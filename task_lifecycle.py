"""Worker capacity, pause/cancellation and bounded shutdown."""
import threading
from queue import Empty
from settings import MAX_WORKERS
from task_storage import CancellationEvent
from queue_store import task_state
from utils import DEFAULT_WORKERS, log_warning
from task_workers import Worker


class TaskLifecycleMixin:
    def cancel_task(self, tid):
        return self._cancel_task_by_tid(tid)

    def start_thread(self, thread):
        with self.tasks_lock:
            if self._closing:
                return None
            self.background_threads = {t for t in self.background_threads if t.is_alive()}
            self.background_threads.add(thread)
            thread.daemon = True
            thread.start()
        return thread

    def _release_active_url(self, url, tid):
        """Release the URL only if owned by this task; the caller must hold tasks_lock."""
        if self._active_url_owners.get(url) == tid:
            self._active_url_owners.pop(url)
            self.active_urls.discard(url)

    def _wait_for_worker_slot(self, worker):
        with self._worker_slots:
            while not (self._closing or self._shutdown_event.is_set() or worker._stop_event.is_set()):
                slot = self.workers.index(worker) if worker in self.workers else 0
                if ((not self.paused or not self._restored_queue_paused)
                        and slot < self.worker_count and len(self.tid_to_workerid) < self.worker_count):
                    return True
                self._worker_slots.wait(timeout=0.2)
        return False

    def _process_task(self, task_tuple, worker_id, *, worker=None):
        tid, url = task_tuple
        with self._worker_slots:
            # Keep at most the selected number of tasks active. Extra pool
            # threads stay reusable instead of being retired and recreated.
            while worker is not None:
                if self._closing or self._shutdown_event.is_set() or worker._stop_event.is_set():
                    if not self._closing:
                        self.task_queue.put(task_tuple)
                    return
                if tid not in self.tasks:
                    return
                slot = self.workers.index(worker) if worker in self.workers else 0
                if slot >= self.worker_count:
                    # The count may have changed after Queue.get(). Do not
                    # strand a pending task in a worker that is now parked.
                    self.task_queue.put(task_tuple)
                    return
                if len(self.tid_to_workerid) < self.worker_count:
                    break
                self._worker_slots.wait(timeout=0.2)
            if tid not in self.tasks:
                return
            self.tid_to_workerid[tid] = worker_id
            self._sync_summary_task(tid)
        try:
            self.engine.process_task(task_tuple, worker_id)
        finally:
            with self._worker_slots:
                task = self.tasks.get(tid)
                if task:
                    self._release_active_url(task["url"], tid)
                self.tid_to_workerid.pop(tid, None)
                self._sync_summary_task(tid)
                self.cancel_events.pop(tid, None)
                self.stream_progress.pop(tid, None)
                self.download_speed_history.pop(tid, None)
                self._download_progress_locks.pop(tid, None)
                self._resolved_info.pop(tid, None)
                self._worker_slots.notify_all()
            self._update_queue_label()

    def set_worker_count(self, count):
        if type(count) is not int or not 1 <= count <= MAX_WORKERS:
            raise ValueError(f"Worker count must be an integer between 1 and {MAX_WORKERS}")
        with self._worker_slots:
            if self._closing:
                return
            self.worker_count = count
            self._ensure_workers_locked()
            self._worker_slots.notify_all()

    def toggle_pause(self):
        with self._worker_slots:
            if not self._closing:
                self.paused = not self.paused
                if not self.paused:
                    self._restored_queue_paused = False
                self.emit("pause_changed", paused=self.paused)
                self._worker_slots.notify_all()
        return self.paused

    def close(self, *, require_idle=False):
        """Begin shutdown; updates may atomically refuse to interrupt any work."""
        with self.tasks_lock:
            if self._closing:
                return not require_idle
            if require_idle and self._has_update_work_locked():
                return False
            self._closing = True
            self._restart_after_shutdown = {tid for tid, task in self.tasks.items()
                                            if task_state(task) == 'pending'}
            self._queue_revision += 1
            self._resolved_info.clear()
            self._shutdown_phase = "grace"
            self._shutdown_deadline = self._shutdown_clock() + self.shutdown_grace_seconds
            self._shutdown_event.set()
            self._worker_slots.notify_all()
            self.paused = False
            for event in self.cancel_events.values():
                event.set()
            workers = self.workers + self.retiring_workers
        self._discard_pending_links()
        for worker in workers:
            worker.stop()
        return True

    def is_idle(self):
        with self.tasks_lock:
            threads = self.workers + self.retiring_workers + list(self.background_threads)
        return not any(thread.is_alive() for thread in threads)

    @property
    def shutdown_timed_out(self):
        return self._shutdown_phase == "timed_out"

    def _begin_forced_shutdown(self):
        self._shutdown_phase = "forcing"
        self._shutdown_deadline = self._shutdown_clock() + self.shutdown_force_seconds
        # Process termination must not block the GUI thread.
        self._shutdown_force_thread = threading.Thread(
            target=self._terminate_processes_for_shutdown, name="shutdown-processes", daemon=True)
        self._shutdown_force_thread.start()

    def _terminate_processes_for_shutdown(self):
        try:
            self.engine.terminate_managed_processes()
        except Exception as error:
            log_warning("Could not terminate subprocesses during shutdown", error)

    def extend_shutdown_wait(self):
        """Only an explicit user choice grants another bounded wait."""
        if self.shutdown_timed_out:
            self._shutdown_phase = "forcing"
            self._shutdown_deadline = self._shutdown_clock() + self.shutdown_grace_seconds

    def finish_shutdown(self, *, force=False):
        if not self._closing:
            return False
        if self._shutdown_abandoned:
            return True
        force_running = (self._shutdown_force_thread is not None
                         and self._shutdown_force_thread.is_alive())
        if force:
            # Do not delete workspaces or close files owned by live threads.
            # Workers are daemons; closing the UI lets the application exit.
            if self._shutdown_phase == "grace":
                self._begin_forced_shutdown()
            self._shutdown_abandoned = True
            log_warning("Shutdown timed out; exiting while preserving files owned by remaining tasks",
                        RuntimeError("The user chose to close without waiting for remaining cleanup"),
                        details=[("task folders", sorted({t.get("folder", "") for t in self.list_tasks()}))])
        elif not self.is_idle() or force_running:
            if self._shutdown_clock() >= self._shutdown_deadline:
                if self._shutdown_phase == "grace":
                    self._begin_forced_shutdown()
                else:
                    self._shutdown_phase = "timed_out"
            return False
        for queue in (self.task_queue, self.link_queue, self.events):
            while True:
                try:
                    queue.get_nowait()
                except Empty:
                    break
                if queue is not self.events:
                    queue.task_done()
        return True

    def _cancel_task_by_tid(self, tid) -> bool:
        """Request cancellation unless the task is missing or already terminal or cancelling.

        Return True when cancellation is requested, otherwise False.
        """
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if not task:
                return False
            status = task["status"]
            if status.startswith("Completed") or status.startswith("Error") or status.startswith("Cancel"):
                return False
            ev = self.cancel_events.get(tid)
            if ev is None:
                ev = CancellationEvent()
                self.cancel_events[tid] = ev
            ev.set()
            worker_id = self.tid_to_workerid.get(tid)
            # Finish queued cancellations immediately, even when all workers are busy or paused.
            queued = worker_id is None and (status == "Queued" or status.startswith("Queued"))
            if queued:
                self._release_active_url(task["url"], tid)  # Allow the same URL to be submitted again.
                self._resolved_info.pop(tid, None)
            # Keep the status check, cancellation signal and transition atomic.
            # A worker may finish immediately after this lock is released;
            # cancellation must never overwrite that terminal update later.
            task["status"] = "Cancelled" if queued else "Cancelling..."
            task["speed_bps"] = None
            task["eta_seconds"] = None
            self._sync_summary_task(tid)
            self._emit_task("task_updated", tid, task)
        if queued:
            self._update_global_progress()
        return True

    def _is_cancelled(self, tid) -> bool:
        ev = self.cancel_events.get(tid)
        return self._shutdown_event.is_set() or (ev is not None and ev.is_set())

    def _ensure_workers(self):
        with self._worker_slots:
            self._ensure_workers_locked()
            self._worker_slots.notify_all()

    def _ensure_workers_locked(self):
        if self._closing:
            return
        try:
            desired = max(1, int(self.worker_count or DEFAULT_WORKERS))
        except (ValueError, TypeError) as error:
            log_warning("Could not read the worker count; using the default", error,
                        once_key="worker_count")
            desired = DEFAULT_WORKERS
        # The pool grows only as needed, up to MAX_WORKERS. Reducing the count
        # parks extra threads at task admission; increasing it reuses them.
        while len(self.workers) < desired:
            self.worker_counter += 1
            wid = self.worker_counter
            w = Worker(self, self.task_queue, wid)
            w.start()
            self.workers.append(w)
