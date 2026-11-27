"""Task queue persistence, admission, link resolution and retries."""
import threading
from dataclasses import asdict, field
from queue import Empty
from task_runtime import TaskRuntime
from component_ports import TaskOperations
from task_storage import TaskRecord, CancellationEvent
from task_state import TaskState, state_of, TERMINAL_STATES
from queue_store import task_state, validate_document, SCHEMA_VERSION as QUEUE_SCHEMA_VERSION
from naming_templates import validate_naming_options
from url_utils import normalize_url
from task_types import LINK_RESOLVER_WORKERS, TaskOptions
from output_compat import LEGACY_OUTPUT_KEYS, canonical_document
from task_workers import PlaylistFetcher, _link_resolution_key


class TaskQueue:
    def __init__(self, state: TaskRuntime, operations: TaskOperations):
        self.state = state
        self.operations = operations

    @property
    def queue_revision(self):
        with self.state.tasks_lock:
            return self.state._queue_revision

    def queue_snapshot(self):
        """Persist source URLs and task choices, never expiring media metadata or process state."""
        with self.state.tasks_lock:
            # Copy only private mutable state under the lock. Expanding shared
            # options into the on-disk schema happens after releasing workers.
            tasks = [task.frozen_copy() if isinstance(task, TaskRecord) else self.operations._snapshot(task)
                     for task in self.state.tasks.values()]
            restarting = self.state._restart_after_shutdown.copy()
            revision = self.state._queue_revision
        rows = []
        for task in tasks:
            state = task_state(task, restarting)
            rows.append(dict(id=task['id'], url=task['url'], title=task['title'], state=state,
                status=task['status'] if state in ('completed', 'failed') else '',
                options={name: task.get(name, field.default) for name, field in TaskOptions.__dataclass_fields__.items() if name not in LEGACY_OUTPUT_KEYS},
                output_path=task.get('output_path') if state == 'completed' else None,
                warnings=list(task.get('warnings', [])), attempt=task.get('attempt', 0)))
        return revision, dict(schema_version=QUEUE_SCHEMA_VERSION, tasks=rows)

    def restore_queue(self, document):
        """Restore before workers start. Pending work waits for an explicit Resume."""
        document = validate_document(document)
        with self.state.tasks_lock:
            if self.state.tasks or self.state.workers or self.state._closing:
                raise ValueError('Restore the queue before starting tasks or workers.')
            pending = False
            shared_options = {}
            for row in document['tasks']:
                tid, state = row['id'], row['state']
                status = {'pending': 'Queued', 'completed': 'Completed',
                          'failed': 'Error: Previous download failed', 'cancelled': 'Cancelled'}[state]
                if state in ('completed', 'failed') and row['status']:
                    status = row['status']
                option_key = tuple(row['options'].items())
                options = shared_options.setdefault(option_key, row['options'])
                task = TaskRecord(options, id=tid, url=row['url'], title=row['title'],
                        state=TaskState.QUEUED if state == 'pending' else TaskState(state),
                        status=status, progress=100 if state == 'completed' else 0,
                        output_path=row['output_path'], warnings=row['warnings'], attempt=row['attempt'])
                self.state.tasks[tid] = task
                self.state.task_counter = max(self.state.task_counter, int(tid[1:]))
                self.state.cancel_events[tid] = CancellationEvent()
                if state == 'cancelled':
                    self.state.cancel_events[tid].set()
                if state == 'pending':
                    pending = True
                    self.state.active_urls.add(row['url'])
                    self.state._active_url_owners[row['url']] = tid
                    self.state.task_queue.put((tid, row['url']))
                self.operations._sync_summary_task(tid)
                self.operations._emit_task('task_added', tid, task)
            if pending:
                self.state.paused = True
                self.state._restored_queue_paused = True
                self.operations.emit('pause_changed', paused=True)
        if document['tasks']:
            self.operations._update_queue_label()
        return len(document['tasks'])

    def submit_link(self, url, options, expand_playlist=False):
        """Queue link resolution unless shutdown has started; resolver concurrency is limited."""
        with self.state.tasks_lock:
            if self.state._closing:
                return None
            key, active_url = _link_resolution_key(url, expand_playlist)
            if key in self.state._pending_link_keys or active_url in self.state.active_urls:
                return False
            if len(self.state.link_workers) < LINK_RESOLVER_WORKERS:
                worker = threading.Thread(target=self.operations._resolve_links,
                                          name=f"link-resolver-{len(self.state.link_workers) + 1}",
                                          daemon=True)
                worker.start()
                self.state.link_workers.append(worker)
                self.state.background_threads.add(worker)
            self.state._pending_link_keys.add(key)
            self.state.link_queue.put((url, options, expand_playlist))
        return True

    def _resolve_links(self):
        while not self.state._shutdown_event.is_set():
            try:
                url, options, expand_playlist = self.state.link_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                key, active_url = _link_resolution_key(url, expand_playlist)
                with self.state.tasks_lock:
                    already_active = active_url in self.state.active_urls
                if not self.state._shutdown_event.is_set() and not already_active:
                    PlaylistFetcher(self.operations, url, options, expand_playlist).run()
            finally:
                with self.state.tasks_lock:
                    self.state._pending_link_keys.discard(_link_resolution_key(url, expand_playlist)[0])
                self.state.link_queue.task_done()

    def _discard_pending_links(self):
        while True:
            try:
                url, options, expand_playlist = self.state.link_queue.get_nowait()
            except Empty:
                return
            with self.state.tasks_lock:
                self.state._pending_link_keys.discard(_link_resolution_key(url, expand_playlist)[0])
            self.state.link_queue.task_done()

    def take_resolved_info(self, tid):
        with self.state.tasks_lock:
            self.state._resolved_info.prune()
            return self.state._resolved_info.pop(tid, None)

    def add_urls(self, urls, options, *, resolved_info=None, notify=True, entry_context=None):
        if self.state._closing:
            return []
        added, skipped = [], 0
        option_fields = canonical_document(asdict(options))
        for url in urls:
            norm = normalize_url(url)
            context = (entry_context or {}).get(norm, {})
            naming_context = {key: context[key] for key in ('playlist_title', 'playlist_index') if key in context}
            if naming_context:
                validate_naming_options({**option_fields, **naming_context})
            info = resolved_info.get(norm) if resolved_info else None
            info_size = self.state._resolved_info.prepare(info) if info is not None else None
            with self.state.tasks_lock:
                if self.state._closing:
                    break
                if norm in self.state.active_urls:
                    skipped += 1
                    continue
                self.state.task_counter += 1
                tid = f"T{self.state.task_counter}"
                task = TaskRecord(option_fields, **naming_context, id=tid, url=norm,
                        title="(resolving title...)", state=TaskState.QUEUED, status="Queued",
                        progress=0, output_path=None, attempt=0)
                self.state.tasks[tid] = task
                self.operations._sync_summary_task(tid)
                if info is not None:
                    self.state._resolved_info.put(tid, info, info_size)
                self.state.active_urls.add(norm)
                self.state._active_url_owners[norm] = tid
                self.state.cancel_events[tid] = CancellationEvent()
                self.operations._emit_task("task_added", tid, task)
                self.state.task_queue.put((tid, norm))
                added.append(tid)
        self.operations._update_queue_label()
        message = f"{len(added)} videos added to the queue!"
        if skipped:
            message += f" ({skipped} skipped: the URL is already queued)"
        if notify:
            self.operations._notify(message)
        return added

    def clear_list(self, remove_pending=False):
        if remove_pending:
            while True:
                try:
                    self.state.task_queue.get_nowait()
                except Empty:
                    break
                self.state.task_queue.task_done()
        removed = []
        with self.state.tasks_lock:
            for tid, task in list(self.state.tasks.items()):
                state = state_of(task)
                finished = state in TERMINAL_STATES
                waiting = remove_pending and state == TaskState.QUEUED
                orphan = remove_pending and state == TaskState.CANCELLING and tid not in self.state.tid_to_workerid
                if tid not in self.state.tid_to_workerid and (finished or waiting or orphan):
                    self.operations._release_active_url(task["url"], tid)
                    del self.state.tasks[tid]
                    self.operations._sync_summary_task(tid)
                    self.state._resolved_info.pop(tid, None)
                    self.state.cancel_events.pop(tid, None)
                    self.state.stream_progress.pop(tid, None)
                    self.state.download_speed_history.pop(tid, None)
                    self.state._download_progress_locks.pop(tid, None)
                    removed.append(tid)
            if removed:
                self.operations.emit("tasks_removed", tids=tuple(removed))
        self.operations._update_global_progress()
        self.operations._update_queue_label()
        return removed

    def retry_failed(self, selected=()):
        """Requeue failed tasks with their original options.

        Retry selected failed tasks, or all failed tasks when no selection is given.
        """
        if self.state._closing:
            return
        sel = selected
        with self.state.tasks_lock:
            scope = list(sel) if sel else list(self.state.tasks.keys())
            failed = [tid for tid in scope
                      if tid in self.state.tasks and state_of(self.state.tasks[tid]) == TaskState.FAILED]
        if not failed:
            self.operations._notify("No selected tasks have failed." if sel else "No failed tasks to retry.")
            return

        retried = skipped = 0
        for tid in failed:
            with self.state.tasks_lock:
                t = self.state.tasks.get(tid)
                # Skip tasks still cleaning up and URLs already owned by another queued or active task.
                if (not t or state_of(t) != TaskState.FAILED or tid in self.state.tid_to_workerid
                        or t["url"] in self.state.active_urls):
                    skipped += 1
                    continue
                url = t["url"]
                self.state.active_urls.add(url)
                self.state._active_url_owners[url] = tid
                self.state.cancel_events[tid] = CancellationEvent()
                self.state.stream_progress.pop(tid, None)
                self.state.download_speed_history.pop(tid, None)
                t["progress"] = 0
                t.pop("_download_report_at", None)
                t["downloaded_bytes"] = t["total_bytes"] = 0
                t.pop("_progress_stage", None)
                t["output_path"] = None
                t.pop("warnings", None)
                self.operations._sync_summary_task(tid)
            self.operations._task_update(tid, state=TaskState.QUEUED, status="Queued", progress=0)
            self.state.task_queue.put((tid, url))
            retried += 1

        self.operations._update_queue_label()
        msg = f"{retried} tasks queued for retry." if retried else "No tasks could be retried."
        if skipped:
            msg += f" ({skipped} skipped: the URL is already queued)"
        self.operations._notify(msg)
