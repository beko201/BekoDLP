"""Worker capacity, pause/cancellation and bounded shutdown."""
import threading
from queue import Empty
from task_runtime import TaskRuntime
from component_ports import TaskOperations
from settings import MAX_WORKERS
from task_storage import CancellationEvent
from task_state import TaskState, state_of, STOPPED_STATES
from queue_store import task_state
from download_support import DEFAULT_WORKERS
from app_logging import log_warning
from task_workers import Worker


class TaskLifecycle:
    def __init__(self, state: TaskRuntime, operations: TaskOperations):
        self.state = state
        self.operations = operations

    def cancel_task(self, tid):
        return self.operations._cancel_task_by_tid(tid)

    def start_thread(self, thread):
        with self.state.tasks_lock:
            if self.state._closing:
                return None
            self.state.background_threads = {t for t in self.state.background_threads if t.is_alive()}
            self.state.background_threads.add(thread)
            thread.daemon = True
            thread.start()
        return thread

    def _release_active_url(self, url, tid):
        """Release the URL only if owned by this task; the caller must hold tasks_lock."""
        if self.state._active_url_owners.get(url) == tid:
            self.state._active_url_owners.pop(url)
            self.state.active_urls.discard(url)

    def _wait_for_worker_slot(self, worker):
        with self.state._worker_slots:
            while not (self.state._closing or self.state._shutdown_event.is_set() or worker._stop_event.is_set()):
                slot = self.state.workers.index(worker) if worker in self.state.workers else 0
                if ((not self.state.paused or not self.state._restored_queue_paused)
                        and slot < self.state.worker_count and len(self.state.tid_to_workerid) < self.state.worker_count):
                    return True
                self.state._worker_slots.wait(timeout=0.2)
        return False

    def _process_task(self, task_tuple, worker_id, *, worker=None):
        tid, url = task_tuple
        with self.state._worker_slots:
            # Keep at most the selected number of tasks active. Extra pool
            # threads stay reusable instead of being retired and recreated.
            while worker is not None:
                if self.state._closing or self.state._shutdown_event.is_set() or worker._stop_event.is_set():
                    if not self.state._closing:
                        self.state.task_queue.put(task_tuple)
                    return
                if tid not in self.state.tasks:
                    return
                slot = self.state.workers.index(worker) if worker in self.state.workers else 0
                if slot >= self.state.worker_count:
                    # The count may have changed after Queue.get(). Do not
                    # strand a pending task in a worker that is now parked.
                    self.state.task_queue.put(task_tuple)
                    return
                if len(self.state.tid_to_workerid) < self.state.worker_count:
                    break
                self.state._worker_slots.wait(timeout=0.2)
            if tid not in self.state.tasks:
                return
            self.state.tid_to_workerid[tid] = worker_id
            self.operations._sync_summary_task(tid)
        try:
            self.state.engine.process_task(task_tuple, worker_id)
        finally:
            with self.state._worker_slots:
                task = self.state.tasks.get(tid)
                if task:
                    self.operations._release_active_url(task["url"], tid)
                self.state.tid_to_workerid.pop(tid, None)
                self.operations._sync_summary_task(tid)
                self.state.cancel_events.pop(tid, None)
                self.state.stream_progress.pop(tid, None)
                self.state.download_speed_history.pop(tid, None)
                self.state._download_progress_locks.pop(tid, None)
                self.state._resolved_info.pop(tid, None)
                self.state._worker_slots.notify_all()
            self.operations._update_queue_label()

    def set_worker_count(self, count):
        if type(count) is not int or not 1 <= count <= MAX_WORKERS:
            raise ValueError(f"Worker count must be an integer between 1 and {MAX_WORKERS}")
        with self.state._worker_slots:
            if self.state._closing:
                return
            self.state.worker_count = count
            self.operations._ensure_workers_locked()
            self.state._worker_slots.notify_all()

    def toggle_pause(self):
        with self.state._worker_slots:
            if not self.state._closing:
                self.state.paused = not self.state.paused
                if not self.state.paused:
                    self.state._restored_queue_paused = False
                self.operations.emit("pause_changed", paused=self.state.paused)
                self.state._worker_slots.notify_all()
        return self.state.paused

    def close(self, *, require_idle=False):
        """Begin shutdown; updates may atomically refuse to interrupt any work."""
        with self.state.tasks_lock:
            if self.state._closing:
                return not require_idle
            if require_idle and self.operations._has_update_work_locked():
                return False
            self.state._closing = True
            self.state._restart_after_shutdown = {tid for tid, task in self.state.tasks.items()
                                            if task_state(task) == 'pending'}
            self.state._queue_revision += 1
            self.state._resolved_info.clear()
            self.state._shutdown_phase = "grace"
            self.state._shutdown_deadline = self.state._shutdown_clock() + self.state.shutdown_grace_seconds
            self.state._shutdown_event.set()
            self.state._worker_slots.notify_all()
            self.state.paused = False
            for event in self.state.cancel_events.values():
                event.set()
            workers = self.state.workers + self.state.retiring_workers
        self.operations._discard_pending_links()
        for worker in workers:
            worker.stop()
        return True

    def is_idle(self):
        with self.state.tasks_lock:
            threads = self.state.workers + self.state.retiring_workers + list(self.state.background_threads)
        return not any(thread.is_alive() for thread in threads)

    @property
    def shutdown_timed_out(self):
        return self.state._shutdown_phase == "timed_out"

    def _begin_forced_shutdown(self):
        self.state._shutdown_phase = "forcing"
        self.state._shutdown_deadline = self.state._shutdown_clock() + self.state.shutdown_force_seconds
        # Process termination must not block the GUI thread.
        self.state._shutdown_force_thread = threading.Thread(
            target=self.operations._terminate_processes_for_shutdown, name="shutdown-processes", daemon=True)
        self.state._shutdown_force_thread.start()

    def _terminate_processes_for_shutdown(self):
        try:
            self.state.engine.terminate_managed_processes()
        except Exception as error:
            log_warning("Could not terminate subprocesses during shutdown", error)

    def extend_shutdown_wait(self):
        """Only an explicit user choice grants another bounded wait."""
        if self.operations.shutdown_timed_out:
            self.state._shutdown_phase = "forcing"
            self.state._shutdown_deadline = self.state._shutdown_clock() + self.state.shutdown_grace_seconds

    def finish_shutdown(self, *, force=False):
        if not self.state._closing:
            return False
        if self.state._shutdown_abandoned:
            return True
        force_running = (self.state._shutdown_force_thread is not None
                         and self.state._shutdown_force_thread.is_alive())
        if force:
            # Do not delete workspaces or close files owned by live threads.
            # Workers are daemons; closing the UI lets the application exit.
            if self.state._shutdown_phase == "grace":
                self.operations._begin_forced_shutdown()
            self.state._shutdown_abandoned = True
            log_warning("Shutdown timed out; exiting while preserving files owned by remaining tasks",
                        RuntimeError("The user chose to close without waiting for remaining cleanup"),
                        details=[("task folders", sorted({t.get("folder", "") for t in self.operations.list_tasks()}))])
        elif not self.operations.is_idle() or force_running:
            if self.state._shutdown_clock() >= self.state._shutdown_deadline:
                if self.state._shutdown_phase == "grace":
                    self.operations._begin_forced_shutdown()
                else:
                    self.state._shutdown_phase = "timed_out"
            return False
        for queue in (self.state.task_queue, self.state.link_queue, self.state.events):
            while True:
                try:
                    queue.get_nowait()
                except Empty:
                    break
                if queue is not self.state.events:
                    queue.task_done()
        return True

    def _cancel_task_by_tid(self, tid) -> bool:
        """Request cancellation unless the task is missing or already terminal or cancelling.

        Return True when cancellation is requested, otherwise False.
        """
        with self.state.tasks_lock:
            task = self.state.tasks.get(tid)
            if not task:
                return False
            state = state_of(task)
            if state in STOPPED_STATES:
                return False
            ev = self.state.cancel_events.get(tid)
            if ev is None:
                ev = CancellationEvent()
                self.state.cancel_events[tid] = ev
            ev.set()
            worker_id = self.state.tid_to_workerid.get(tid)
            # Finish queued cancellations immediately, even when all workers are busy or paused.
            queued = worker_id is None and state == TaskState.QUEUED
            if queued:
                self.operations._release_active_url(task["url"], tid)  # Allow the same URL to be submitted again.
                self.state._resolved_info.pop(tid, None)
            # Keep the status check, cancellation signal and transition atomic.
            # A worker may finish immediately after this lock is released;
            # cancellation must never overwrite that terminal update later.
            task["status"] = "Cancelled" if queued else "Cancelling..."
            task['state'] = TaskState.CANCELLED if queued else TaskState.CANCELLING
            task["speed_bps"] = None
            task["eta_seconds"] = None
            self.operations._sync_summary_task(tid)
            self.operations._emit_task("task_updated", tid, task)
        if queued:
            self.operations._update_global_progress()
        return True

    def _is_cancelled(self, tid) -> bool:
        ev = self.state.cancel_events.get(tid)
        return self.state._shutdown_event.is_set() or (ev is not None and ev.is_set())

    def _ensure_workers(self):
        with self.state._worker_slots:
            self.operations._ensure_workers_locked()
            self.state._worker_slots.notify_all()

    def _ensure_workers_locked(self):
        if self.state._closing:
            return
        try:
            desired = max(1, int(self.state.worker_count or DEFAULT_WORKERS))
        except (ValueError, TypeError) as error:
            log_warning("Could not read the worker count; using the default", error,
                        once_key="worker_count")
            desired = DEFAULT_WORKERS
        # The pool grows only as needed, up to MAX_WORKERS. Reducing the count
        # parks extra threads at task admission; increasing it reuses them.
        while len(self.state.workers) < desired:
            self.state.worker_counter += 1
            wid = self.state.worker_counter
            w = Worker(self.operations, self.state.task_queue, wid)
            w.start()
            self.state.workers.append(w)
