"""Task queue persistence, admission, link resolution and retries."""
import threading
from dataclasses import asdict, field
from queue import Empty
from task_storage import TaskRecord, CancellationEvent
from queue_store import task_state, validate_document, SCHEMA_VERSION as QUEUE_SCHEMA_VERSION
from naming_templates import validate_naming_options
from utils import normalize_url
from task_types import LINK_RESOLVER_WORKERS, TaskOptions
from task_workers import PlaylistFetcher, _link_resolution_key


class TaskQueueMixin:
    @property
    def queue_revision(self):
        with self.tasks_lock:
            return self._queue_revision

    def queue_snapshot(self):
        """Persist source URLs and task choices, never expiring media metadata or process state."""
        with self.tasks_lock:
            # Copy only private mutable state under the lock. Expanding shared
            # options into the on-disk schema happens after releasing workers.
            tasks = [task.frozen_copy() if isinstance(task, TaskRecord) else self._snapshot(task)
                     for task in self.tasks.values()]
            restarting = self._restart_after_shutdown.copy()
            revision = self._queue_revision
        rows = []
        for task in tasks:
            state = task_state(task, restarting)
            rows.append(dict(id=task['id'], url=task['url'], title=task['title'], state=state,
                status=task['status'] if state in ('completed', 'failed') else '',
                options={name: task.get(name, field.default) for name, field in TaskOptions.__dataclass_fields__.items()},
                output_path=task.get('output_path') if state == 'completed' else None,
                warnings=list(task.get('warnings', [])), attempt=task.get('attempt', 0)))
        return revision, dict(schema_version=QUEUE_SCHEMA_VERSION, tasks=rows)

    def restore_queue(self, document):
        """Restore before workers start. Pending work waits for an explicit Resume."""
        document = validate_document(document)
        with self.tasks_lock:
            if self.tasks or self.workers or self._closing:
                raise ValueError('Restore the queue before starting tasks or workers.')
            pending = False
            shared_options = {}
            for row in document['tasks']:
                tid, state = row['id'], row['state']
                status = {'pending': 'Queued', 'completed': 'Completed',
                          'failed': 'Error: Previous download failed', 'cancelled': 'Cancelled'}[state]
                if state in ('completed', 'failed') and row['status'].startswith(('Completed' if state == 'completed' else 'Error')):
                    status = row['status']
                option_key = tuple(row['options'].items())
                options = shared_options.setdefault(option_key, row['options'])
                task = TaskRecord(options, id=tid, url=row['url'], title=row['title'],
                        status=status, progress=100 if state == 'completed' else 0,
                        output_path=row['output_path'], warnings=row['warnings'], attempt=row['attempt'])
                self.tasks[tid] = task
                self.task_counter = max(self.task_counter, int(tid[1:]))
                self.cancel_events[tid] = CancellationEvent()
                if state == 'cancelled':
                    self.cancel_events[tid].set()
                if state == 'pending':
                    pending = True
                    self.active_urls.add(row['url'])
                    self._active_url_owners[row['url']] = tid
                    self.task_queue.put((tid, row['url']))
                self._sync_summary_task(tid)
                self._emit_task('task_added', tid, task)
            if pending:
                self.paused = True
                self._restored_queue_paused = True
                self.emit('pause_changed', paused=True)
        if document['tasks']:
            self._update_queue_label()
        return len(document['tasks'])

    def submit_link(self, url, options, expand_playlist=False):
        """Queue link resolution unless shutdown has started; resolver concurrency is limited."""
        with self.tasks_lock:
            if self._closing:
                return None
            key, active_url = _link_resolution_key(url, expand_playlist)
            if key in self._pending_link_keys or active_url in self.active_urls:
                return False
            if len(self.link_workers) < LINK_RESOLVER_WORKERS:
                worker = threading.Thread(target=self._resolve_links,
                                          name=f"link-resolver-{len(self.link_workers) + 1}",
                                          daemon=True)
                worker.start()
                self.link_workers.append(worker)
                self.background_threads.add(worker)
            self._pending_link_keys.add(key)
            self.link_queue.put((url, options, expand_playlist))
        return True

    def _resolve_links(self):
        while not self._shutdown_event.is_set():
            try:
                url, options, expand_playlist = self.link_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                key, active_url = _link_resolution_key(url, expand_playlist)
                with self.tasks_lock:
                    already_active = active_url in self.active_urls
                if not self._shutdown_event.is_set() and not already_active:
                    PlaylistFetcher(self, url, options, expand_playlist).run()
            finally:
                with self.tasks_lock:
                    self._pending_link_keys.discard(_link_resolution_key(url, expand_playlist)[0])
                self.link_queue.task_done()

    def _discard_pending_links(self):
        while True:
            try:
                url, options, expand_playlist = self.link_queue.get_nowait()
            except Empty:
                return
            with self.tasks_lock:
                self._pending_link_keys.discard(_link_resolution_key(url, expand_playlist)[0])
            self.link_queue.task_done()

    def take_resolved_info(self, tid):
        with self.tasks_lock:
            self._resolved_info.prune()
            return self._resolved_info.pop(tid, None)

    def add_urls(self, urls, options, *, resolved_info=None, notify=True, entry_context=None):
        if self._closing:
            return []
        added, skipped = [], 0
        option_fields = asdict(options)
        for url in urls:
            norm = normalize_url(url)
            context = (entry_context or {}).get(norm, {})
            naming_context = {key: context[key] for key in ('playlist_title', 'playlist_index') if key in context}
            if naming_context:
                validate_naming_options({**option_fields, **naming_context})
            info = resolved_info.get(norm) if resolved_info else None
            info_size = self._resolved_info.prepare(info) if info is not None else None
            with self.tasks_lock:
                if self._closing:
                    break
                if norm in self.active_urls:
                    skipped += 1
                    continue
                self.task_counter += 1
                tid = f"T{self.task_counter}"
                task = TaskRecord(option_fields, **naming_context, id=tid, url=norm,
                        title="(resolving title...)", status="Queued",
                        progress=0, output_path=None, attempt=0)
                self.tasks[tid] = task
                self._sync_summary_task(tid)
                if info is not None:
                    self._resolved_info.put(tid, info, info_size)
                self.active_urls.add(norm)
                self._active_url_owners[norm] = tid
                self.cancel_events[tid] = CancellationEvent()
                self._emit_task("task_added", tid, task)
                self.task_queue.put((tid, norm))
                added.append(tid)
        self._update_queue_label()
        message = f"{len(added)} videos added to the queue!"
        if skipped:
            message += f" ({skipped} skipped: the URL is already queued)"
        if notify:
            self._notify(message)
        return added

    def clear_list(self, remove_pending=False):
        if remove_pending:
            while True:
                try:
                    self.task_queue.get_nowait()
                except Empty:
                    break
                self.task_queue.task_done()
        removed = []
        with self.tasks_lock:
            for tid, task in list(self.tasks.items()):
                status = task["status"]
                finished = status.startswith(("Completed", "Error")) or status == "Cancelled"
                waiting = remove_pending and (status == "Queued" or status.startswith("Queued"))
                orphan = remove_pending and status == "Cancelling..." and tid not in self.tid_to_workerid
                if tid not in self.tid_to_workerid and (finished or waiting or orphan):
                    self._release_active_url(task["url"], tid)
                    del self.tasks[tid]
                    self._sync_summary_task(tid)
                    self._resolved_info.pop(tid, None)
                    self.cancel_events.pop(tid, None)
                    self.stream_progress.pop(tid, None)
                    self.download_speed_history.pop(tid, None)
                    self._download_progress_locks.pop(tid, None)
                    removed.append(tid)
            if removed:
                self.emit("tasks_removed", tids=tuple(removed))
        self._update_global_progress()
        self._update_queue_label()
        return removed

    def retry_failed(self, selected=()):
        """Requeue failed tasks with their original options.

        Retry selected failed tasks, or all failed tasks when no selection is given.
        """
        if self._closing:
            return
        sel = selected
        with self.tasks_lock:
            scope = list(sel) if sel else list(self.tasks.keys())
            failed = [tid for tid in scope
                      if tid in self.tasks and self.tasks[tid]["status"].startswith("Error")]
        if not failed:
            self._notify("No selected tasks have failed." if sel else "No failed tasks to retry.")
            return

        retried = skipped = 0
        for tid in failed:
            with self.tasks_lock:
                t = self.tasks.get(tid)
                # Skip tasks still cleaning up and URLs already owned by another queued or active task.
                if (not t or not t["status"].startswith("Error") or tid in self.tid_to_workerid
                        or t["url"] in self.active_urls):
                    skipped += 1
                    continue
                url = t["url"]
                self.active_urls.add(url)
                self._active_url_owners[url] = tid
                self.cancel_events[tid] = CancellationEvent()
                self.stream_progress.pop(tid, None)
                self.download_speed_history.pop(tid, None)
                t["progress"] = 0
                t.pop("_download_report_at", None)
                t["downloaded_bytes"] = t["total_bytes"] = 0
                t.pop("_progress_stage", None)
                t["output_path"] = None
                t.pop("warnings", None)
                self._sync_summary_task(tid)
            self._task_update(tid, status="Queued", progress=0)
            self.task_queue.put((tid, url))
            retried += 1

        self._update_queue_label()
        msg = f"{retried} tasks queued for retry." if retried else "No tasks could be retried."
        if skipped:
            msg += f" ({skipped} skipped: the URL is already queued)"
        self._notify(msg)
