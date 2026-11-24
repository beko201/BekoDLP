"""Public task manager facade; components share its existing locks and state."""
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
from settings import MAX_WORKERS
from media_options import validate_output_options
from resolved_info_cache import ResolvedInfoCache
from task_storage import TaskRecord, CancellationEvent
from queue_store import task_state, validate_document, SCHEMA_VERSION as QUEUE_SCHEMA_VERSION
from cookie_options import validate_cookie_options, cookie_kwargs
from subtitle_options import validate_subtitle_options
from network_options import validate_network_options, network_kwargs
from naming_templates import validate_naming_options, uses_playlist_naming
from cover_options import validate_cover_options
from chapter_options import validate_chapter_options
from range_options import validate_range_options
from source_formats import validate_source_options
from utils import (
    DEFAULT_WORKERS,
    DownloadCancelled,
    PlaylistLinkNotAllowed,
    normalize_url,
    validate_trim_range,
    format_eta,
    format_mb,
    log_error,
    log_warning,
    unexpected_traceback,
    env_info,
)
from task_lifecycle import TaskLifecycleMixin
from task_progress import TaskProgressMixin
from task_queue import TaskQueueMixin
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


class TaskManager(TaskQueueMixin, TaskProgressMixin, TaskLifecycleMixin):
    """Manage tasks independently of the GUI, which consumes the event queue."""

    def __init__(self, engine_factory=None, *, shutdown_grace_seconds=5, shutdown_force_seconds=2):
        self.tasks_lock = threading.Lock()
        self._worker_slots = threading.Condition(self.tasks_lock)
        self.tasks = {}
        self._summary_rows = {}
        self._summary_counts = [0, 0, 0, 0]
        self._progress_sum = 0.0
        self._progress_correction = 0.0
        self._action_revision = 0
        self._queue_revision = 0
        self._queue_rows = {}
        self._restart_after_shutdown = set()
        self._restored_queue_paused = False
        self._resolved_info = ResolvedInfoCache()
        self.task_counter = 0
        self.task_queue = Queue()
        self.events = TaskEventQueue()
        self._compact_task_events = False
        self.workers = []
        self.retiring_workers = []
        self.worker_counter = 0
        self.worker_count = DEFAULT_WORKERS
        self.background_threads = set()
        self.link_queue = Queue()
        self.link_workers = []
        self._pending_link_keys = set()
        self.paused = False
        self.active_urls = set()
        self._active_url_owners = {}
        self.cancel_events = {}
        self.tid_to_workerid = {}
        self.stream_progress = {}
        self.download_speed_history = {}
        self._download_progress_locks = {}
        self._closing = False
        self._shutdown_event = threading.Event()
        self.shutdown_grace_seconds = float(shutdown_grace_seconds)
        self.shutdown_force_seconds = float(shutdown_force_seconds)
        if any(not math.isfinite(value) or value < 0 for value in (
                self.shutdown_grace_seconds, self.shutdown_force_seconds)):
            raise ValueError("Shutdown wait durations must be finite and non-negative")
        self._shutdown_deadline = None
        self._shutdown_clock = time.monotonic
        self._shutdown_phase = None
        self._shutdown_force_thread = None
        self._shutdown_abandoned = False
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
            return ({tid: {key: task.get(key) for key in ('status', 'output_path')}
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
            not task['status'].startswith(('Completed', 'Error', 'Cancelled'))
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
        self._task_update(tid, **changes)

    def fail_task(self, tid, message):
        self._task_error(tid, message)

    def notify(self, message):
        self._notify(message)

    def is_cancelled(self, tid):
        return self._is_cancelled(tid)

    def report_progress(self, tid, stream_key, downloaded, total, *, force=False):
        self._on_download_progress(tid, stream_key, downloaded, total, force=force)
