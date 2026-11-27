"""Task manager facade composed from queue, progress and lifecycle services."""
import os
import math
import time
import threading
from collections import deque
from dataclasses import dataclass, asdict, field
from queue import Queue, Empty
from typing import Optional
from urllib.parse import parse_qs, urlsplit
import download_engine
from component_state import RuntimeField
from task_runtime import TaskRuntime
from settings import MAX_WORKERS
from media_options import validate_output_options
from resolved_info_cache import ResolvedInfoCache
from task_storage import TaskRecord, CancellationEvent
from task_state import state_of, TERMINAL_STATES
from output_compat import normalize_output, output_aliases
from queue_store import task_state, validate_document, SCHEMA_VERSION as QUEUE_SCHEMA_VERSION
from cookie_options import validate_cookie_options, cookie_kwargs
from subtitle_options import validate_subtitle_options
from network_options import validate_network_options, network_kwargs
from naming_templates import validate_naming_options, uses_playlist_naming
from cover_options import validate_cover_options
from chapter_options import validate_chapter_options
from range_options import validate_range_options
from source_formats import validate_source_options
from download_support import DEFAULT_WORKERS
from errors import DownloadCancelled, PlaylistLinkNotAllowed
from url_utils import normalize_url
from time_utils import validate_trim_range, format_eta
from file_utils import format_mb
from app_logging import log_error, log_warning, unexpected_traceback
from runtime_tools import env_info
from task_lifecycle import TaskLifecycle
from task_progress import TaskProgress
from task_queue import TaskQueue
from task_types import (
    DOWNLOAD_PROGRESS_INTERVAL,
    DOWNLOAD_SPEED_SAMPLES,
    DOWNLOAD_SPEED_WINDOW,
    LINK_RESOLVER_WORKERS,
    PROGRESS_STAGES,
    TaskEvent,
    TaskEventQueue,
    TaskOptions,
    TaskSummary,
)
from task_workers import PlaylistFetcher, Worker, _link_resolution_key


class TaskManager:
    """Manage tasks independently of the GUI, which consumes the event queue."""

    runtime: TaskRuntime

    # Existing callers retain these names; the runtime is their single owner.
    tasks_lock = RuntimeField()
    _worker_slots = RuntimeField()
    tasks = RuntimeField()
    _summary_rows = RuntimeField()
    _summary_counts = RuntimeField()
    _progress_sum = RuntimeField()
    _progress_correction = RuntimeField()
    _action_revision = RuntimeField()
    _queue_revision = RuntimeField()
    _queue_rows = RuntimeField()
    _restart_after_shutdown = RuntimeField()
    _restored_queue_paused = RuntimeField()
    _resolved_info = RuntimeField()
    task_counter = RuntimeField()
    task_queue = RuntimeField()
    events = RuntimeField()
    _compact_task_events = RuntimeField()
    workers = RuntimeField()
    retiring_workers = RuntimeField()
    worker_counter = RuntimeField()
    worker_count = RuntimeField()
    background_threads = RuntimeField()
    link_queue = RuntimeField()
    link_workers = RuntimeField()
    _pending_link_keys = RuntimeField()
    paused = RuntimeField()
    active_urls = RuntimeField()
    _active_url_owners = RuntimeField()
    cancel_events = RuntimeField()
    tid_to_workerid = RuntimeField()
    stream_progress = RuntimeField()
    download_speed_history = RuntimeField()
    _download_progress_locks = RuntimeField()
    _closing = RuntimeField()
    _shutdown_event = RuntimeField()
    shutdown_grace_seconds = RuntimeField()
    shutdown_force_seconds = RuntimeField()
    _shutdown_deadline = RuntimeField()
    _shutdown_clock = RuntimeField()
    _shutdown_phase = RuntimeField()
    _shutdown_force_thread = RuntimeField()
    _shutdown_abandoned = RuntimeField()
    engine = RuntimeField()

    def __init__(self, engine_factory=None, *, shutdown_grace_seconds=5, shutdown_force_seconds=2):
        self.runtime = TaskRuntime(shutdown_grace_seconds=shutdown_grace_seconds,
                                   shutdown_force_seconds=shutdown_force_seconds)
        self.queue_controller = TaskQueue(self.runtime, self)
        self.progress_tracker = TaskProgress(self.runtime, self)
        self.lifecycle = TaskLifecycle(self.runtime, self)
        self.engine = (engine_factory or download_engine.DownloadEngine)(self)

    def emit(self, kind, tid=None, **data):
        if not self._closing:
            self.events.put(TaskEvent(kind, tid, data))

    def use_compact_task_events(self):
        """GUI reads the latest state in batches; headless snapshot API stays intact."""
        with self.tasks_lock:
            self._compact_task_events = True

    def _emit_task(self, kind, tid, task):
        # Caller holds tasks_lock, so legacy snapshots remain atomic.
        if self._compact_task_events:
            self.emit(kind, tid)
        else:
            self.emit(kind, tid, task=self._snapshot(task))

    @staticmethod
    def _snapshot(task):
        result = task.snapshot() if isinstance(task, TaskRecord) else dict(task)
        result = normalize_output(result)
        result.update(output_aliases(result))
        if "warnings" in result:
            result["warnings"] = list(result["warnings"])
        return result

    def get_task(self, tid):
        with self.tasks_lock:
            task = self.tasks.get(tid)
            return self._snapshot(task) if task is not None else None

    def list_tasks(self):
        with self.tasks_lock:
            return [self._snapshot(task) for task in self.tasks.values()]

    def completion_snapshot(self):
        with self.tasks_lock:
            return ({tid: dict(state=state_of(task), status=task.get('status'), output_path=task.get('output_path'))
                     for tid, task in self.tasks.items()},
                    bool(self._pending_link_keys or self.tid_to_workerid), self.paused, self._closing)

    def get_tasks(self, tids):
        """Read a changed batch under one lock, omitting tasks already removed."""
        with self.tasks_lock:
            return [self._snapshot(self.tasks[tid]) for tid in tids if tid in self.tasks]

    def summary(self):
        """Return queue aggregates without copying or scanning task snapshots."""
        with self.tasks_lock:
            total = len(self._summary_rows)
            progress = round((self._progress_sum + self._progress_correction) / total) if total else 0
            return TaskSummary(total, *self._summary_counts, progress, self._action_revision)

    def has_ui_work(self):
        """Constant-time activity hint; sleeping worker threads are not work."""
        with self.tasks_lock:
            # The small, bounded pending cache is also swept while the UI is
            # idle, so expiry does not require another download to begin.
            self._resolved_info.prune()
            return bool(self._pending_link_keys or (not self.paused and
                        (self.tid_to_workerid or self._summary_counts[0])))

    def _has_update_work_locked(self):
        # A completed/error row can still have a worker cleaning up. Pausing
        # does not make unfinished tasks safe to interrupt for an update.
        return bool(self._pending_link_keys or self.tid_to_workerid or any(
            state_of(task) not in TERMINAL_STATES
            for task in self.tasks.values()))

    def has_update_work(self):
        """Update-only check; do not confuse queued counts with unfinished work."""
        with self.tasks_lock:
            return self._has_update_work_locked()

    def activate_tool_update(self, activate):
        """Prevent tasks/resolvers entering while an idle tool switch is committed."""
        with self.tasks_lock:
            if self._closing or self._has_update_work_locked():
                return False
            activate()
            return True

    def task_control(self, tid):
        with self.tasks_lock:
            return self.cancel_events.get(tid, self._shutdown_event), self.tid_to_workerid.get(tid)

    def update_metadata(self, tid, *, duration):
        with self.tasks_lock:
            if tid in self.tasks:
                self.tasks[tid]["duration"] = duration

    def add_warning(self, tid, message):
        with self.tasks_lock:
            if tid in self.tasks:
                warnings = self.tasks[tid].setdefault("warnings", [])
                if message not in warnings:
                    warnings.append(message)
                    self._queue_revision += 1

    def should_check_disk(self, tid, now):
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if task is not None:
                if now - task.get("_disk_check_at", 0) < 1:
                    return False
                task["_disk_check_at"] = now
        return True

    def update_task(self, tid, **changes):
        """Update display fields; pass state=TaskState(...) for lifecycle changes.

        Historical status-only calls are adapted by _task_update.
        """
        self._task_update(tid, **changes)

    def fail_task(self, tid, message):
        self._task_error(tid, message)

    def notify(self, message):
        self._notify(message)

    def is_cancelled(self, tid):
        return self._is_cancelled(tid)

    def report_progress(self, tid, stream_key, downloaded, total, *, force=False):
        self._on_download_progress(tid, stream_key, downloaded, total, force=force)

    @property
    def queue_revision(self):
        return self.queue_controller.queue_revision

    def queue_snapshot(self):
        return self.queue_controller.queue_snapshot()

    def restore_queue(self, document):
        return self.queue_controller.restore_queue(document)

    def submit_link(self, url, options, expand_playlist=False):
        return self.queue_controller.submit_link(url, options, expand_playlist)

    def _resolve_links(self):
        return self.queue_controller._resolve_links()

    def _discard_pending_links(self):
        return self.queue_controller._discard_pending_links()

    def take_resolved_info(self, tid):
        return self.queue_controller.take_resolved_info(tid)

    def add_urls(self, urls, options, *, resolved_info=None, notify=True, entry_context=None):
        return self.queue_controller.add_urls(urls, options, resolved_info=resolved_info, notify=notify, entry_context=entry_context)

    def clear_list(self, remove_pending=False):
        return self.queue_controller.clear_list(remove_pending)

    def retry_failed(self, selected=()):
        return self.queue_controller.retry_failed(selected)

    def _add_summary_progress(self, value):
        return self.progress_tracker._add_summary_progress(value)

    def _sync_summary_task(self, tid):
        return self.progress_tracker._sync_summary_task(tid)

    def register_download_streams(self, tid, streams):
        return self.progress_tracker.register_download_streams(tid, streams)

    def report_stage_progress(self, tid, stage, fraction=0, *, status=None, download_metrics=None, state=None):
        return self.progress_tracker.report_stage_progress(tid, stage, fraction, status=status, download_metrics=download_metrics, state=state)

    def begin_task_attempt(self, tid):
        return self.progress_tracker.begin_task_attempt(tid)

    def task_log_fields(self, tid):
        return self.progress_tracker.task_log_fields(tid)

    def set_log_stage(self, tid, stage, *, progress_stage=None):
        return self.progress_tracker.set_log_stage(tid, stage, progress_stage=progress_stage)

    def _task_update(self, tid, title=None, status=None, progress=None, output_path=None, progress_stage=None, download_metrics=None, state=None):
        return self.progress_tracker._task_update(tid, title, status, progress, output_path, progress_stage, download_metrics, state)

    def _task_error(self, tid, errmsg):
        return self.progress_tracker._task_error(tid, errmsg)

    def _update_global_progress(self):
        return self.progress_tracker._update_global_progress()

    def _update_queue_label(self):
        return self.progress_tracker._update_queue_label()

    def _notify(self, text):
        return self.progress_tracker._notify(text)

    def _log_task_error(self, tid, errmsg):
        return self.progress_tracker._log_task_error(tid, errmsg)

    def _on_download_progress(self, tid, stream_key, downloaded, total, *, force=False):
        return self.progress_tracker._on_download_progress(tid, stream_key, downloaded, total, force=force)

    def cancel_task(self, tid):
        return self.lifecycle.cancel_task(tid)

    def start_thread(self, thread):
        return self.lifecycle.start_thread(thread)

    def _release_active_url(self, url, tid):
        return self.lifecycle._release_active_url(url, tid)

    def _wait_for_worker_slot(self, worker):
        return self.lifecycle._wait_for_worker_slot(worker)

    def _process_task(self, task_tuple, worker_id, *, worker=None):
        return self.lifecycle._process_task(task_tuple, worker_id, worker=worker)

    def set_worker_count(self, count):
        return self.lifecycle.set_worker_count(count)

    def toggle_pause(self):
        return self.lifecycle.toggle_pause()

    def close(self, *, require_idle=False):
        return self.lifecycle.close(require_idle=require_idle)

    def is_idle(self):
        return self.lifecycle.is_idle()

    @property
    def shutdown_timed_out(self):
        return self.lifecycle.shutdown_timed_out

    def _begin_forced_shutdown(self):
        return self.lifecycle._begin_forced_shutdown()

    def _terminate_processes_for_shutdown(self):
        return self.lifecycle._terminate_processes_for_shutdown()

    def extend_shutdown_wait(self):
        return self.lifecycle.extend_shutdown_wait()

    def finish_shutdown(self, *, force=False):
        return self.lifecycle.finish_shutdown(force=force)

    def _cancel_task_by_tid(self, tid) -> bool:
        return self.lifecycle._cancel_task_by_tid(tid)

    def _is_cancelled(self, tid) -> bool:
        return self.lifecycle._is_cancelled(tid)

    def _ensure_workers(self):
        return self.lifecycle._ensure_workers()

    def _ensure_workers_locked(self):
        return self.lifecycle._ensure_workers_locked()
