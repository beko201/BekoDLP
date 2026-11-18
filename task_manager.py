"""Task options, event queues, worker lifecycles and task state management."""
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
from utils import (
    DEFAULT_WORKERS, DownloadCancelled, PlaylistLinkNotAllowed, normalize_url,
    validate_trim_range, format_eta, format_mb, log_error, log_warning,
    unexpected_traceback, env_info,
)

LINK_RESOLVER_WORKERS = 3
DOWNLOAD_PROGRESS_INTERVAL = 0.1
DOWNLOAD_SPEED_WINDOW = 2.0
DOWNLOAD_SPEED_SAMPLES = 32


def _link_resolution_key(url, expand_playlist):
    normalized = normalize_url(url)
    try:
        parsed = urlsplit(url if '://' in url or url.startswith('//') else '//' + url)
        if (expand_playlist and parsed.scheme in ('', 'http', 'https') and parsed.username is None
                and parsed.port in (None, 80, 443) and parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com',
                                                    'music.youtube.com', 'youtu.be', 'www.youtu.be'}):
            playlist = (parse_qs(parsed.query).get('list') or [None])[0]
            if playlist and playlist.isascii() and all(character.isalnum() or character in '_-' for character in playlist):
                return (f'https://www.youtube.com/playlist?list={playlist}', True), None
    except (TypeError, ValueError):
        pass
    mode = False if normalized and normalized.startswith('https://www.youtube.com/watch?v=') else bool(expand_playlist)
    return (normalized, mode), normalized


# Total progress reserves the last percent for successful publication.
PROGRESS_STAGES = {
    "downloading": (0, 70),
    "processing": (70, 90),
    "trimming": (90, 95),
    "verification": (95, 97),
    "publishing": (97, 99),
}


@dataclass(frozen=True)
class TaskOptions:
    folder: str
    format: str = "MP4"
    quality: str = "1080p"
    codec_mode: str = "Original (Automatic)"
    mp3_bitrate: str = "192k"
    trim_start: str = ""
    trim_end: str = ""
    output_mode: str = "legacy"  # Preserve the existing headless API; the UI submits an explicit mode.
    container: str = "auto"
    video_codec: str = "original"
    audio_codec: str = "original"
    audio_bit_depth: str = "original"
    audio_bitrate: str = "192k"
    video_quality: str = "balanced"

    def __post_init__(self):
        validate_trim_range(self.trim_start, self.trim_end)
        validate_output_options(asdict(self))
        object.__setattr__(self, "folder", os.path.abspath(self.folder) if self.folder else "")
        object.__setattr__(self, "mp3_bitrate", self.mp3_bitrate or "192k")


@dataclass(frozen=True)
class TaskEvent:
    kind: str
    tid: Optional[str] = None
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TaskSummary:
    total: int
    pending: int
    failed: int
    retryable: int
    clearable: int
    progress: int
    action_revision: int


class TaskEventQueue(Queue):
    """Coalesce pending state notifications while preserving other events in order."""
    COALESCED_KINDS = frozenset({'task_updated', 'progress_changed', 'queue_changed'})

    def __init__(self):
        super().__init__()  # Unbounded: workers must never wait for the GUI.
        self._pending = {}
        self._wakeup = None

    def set_wakeup(self, callback):
        """Notify an optional consumer on empty-to-nonempty transitions."""
        with self.mutex:
            self._wakeup = callback

    def put(self, event, block=True, timeout=None):
        key = (event.kind, event.tid) if event.kind in self.COALESCED_KINDS else None
        with self.not_full:
            if key is not None and key in self._pending:
                self._pending[key][1] = event
                return
            wakeup = self._wakeup if not self._qsize() else None
            slot = [key, event]
            self._put(slot)
            if key is not None:
                self._pending[key] = slot
            self.unfinished_tasks += 1
            self.not_empty.notify()
        if wakeup is not None:
            wakeup()

    def _get(self):
        # Queue.get() holds the same mutex as put(), so a consumed key can
        # immediately be enqueued again without losing a concurrent update.
        key, event = super()._get()
        if key is not None:
            del self._pending[key]
        return event


class Worker(threading.Thread):
    def __init__(self, manager, queue: Queue, worker_id: int):
        super().__init__(daemon=True)
        self.manager = manager
        self.queue = queue
        self.worker_id = worker_id
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            if not self.manager._wait_for_worker_slot(self):
                break
            try:
                task = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                if self._stop_event.is_set():
                    if not self.manager._shutdown_event.is_set():
                        self.queue.put(task)
                    break
                self.manager._process_task(task, self.worker_id, worker=self)
            except Exception as e:
                self.manager._task_error(task[0], f"Worker {self.worker_id} error: {e}")
            finally:
                self.queue.task_done()


class PlaylistFetcher(threading.Thread):
    """Resolve links using immutable task options without accessing GUI objects."""
    def __init__(self, manager, raw_input_url, options, expand_playlist=True):
        super().__init__(daemon=True)
        self.manager = manager
        self.raw_input_url = raw_input_url
        self.options = options
        self.expand_playlist = expand_playlist

    def run(self):
        try:
            if self.manager._shutdown_event.is_set():
                return
            notes = []
            resolved_info = {}
            added_count = submitted_count = 0
            def accept_batch(urls):
                nonlocal added_count, submitted_count
                if self.manager._shutdown_event.is_set():
                    raise DownloadCancelled()
                added_count += len(self.manager.add_urls(urls, self.options, resolved_info=resolved_info, notify=False))
                submitted_count += len(urls)
            urls = download_engine._extract_urls_from_input(
                self.raw_input_url, self.expand_playlist, notes,
                cancel_event=self.manager._shutdown_event, resolved_info=resolved_info,
                on_batch=accept_batch if self.expand_playlist else None)
            if self.manager._shutdown_event.is_set():
                return
            if urls:
                accept_batch(urls)
            skipped = submitted_count - added_count
            message = f"{added_count} videos added to the queue!"
            if skipped:
                message += f" ({skipped} skipped: the URL is already queued)"
            self.manager.notify(message)
            if notes:
                self.manager.emit("info", title="Playlist could not be read", message="\n".join(notes))
        except DownloadCancelled:
            return
        except PlaylistLinkNotAllowed:
            self.manager.emit("playlist_blocked")
        except Exception as error:
            if self.manager._shutdown_event.is_set():
                return
            log_error("ERROR link resolution", details=[("error", error), ("link", self.raw_input_url),
                                                       ("version", env_info())], tb=unexpected_traceback(),
                      event='link_resolution_failed', component='task_manager')
            self.manager.emit("error", title="Error", message=f"Could not resolve the URL: {error}")


class TaskManager:
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
        result = dict(task)
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

    def _add_summary_progress(self, value):
        # Compensate additions/removals so repeated fractional updates do not
        # gradually move the displayed average across a rounding boundary.
        combined = self._progress_sum + value
        if abs(self._progress_sum) >= abs(value):
            self._progress_correction += (self._progress_sum - combined) + value
        else:
            self._progress_correction += (value - combined) + self._progress_sum
        self._progress_sum = combined

    def _sync_summary_task(self, tid):
        """Update one contribution; caller holds tasks_lock, including worker cleanup."""
        task = self.tasks.get(tid)
        previous = self._summary_rows.get(tid)
        current = None
        if task is not None:
            status = task["status"]
            failed = status.startswith("Error")
            retryable = failed and tid not in self.tid_to_workerid
            clearable = status.startswith(("Completed", "Error", "Queu", "Wait")) or status == "Cancelled"
            actions = (not status.startswith(("Completed", "Error", "Cancel")), failed, retryable,
                       status.startswith("Completed") and bool(task.get("output_path")), clearable)
            progress = float(task.get("progress", 0))
            current = (status.startswith(("Queu", "Wait")), failed, retryable, clearable,
                       progress if math.isfinite(progress) else 0, actions)
        if previous == current:
            return
        for index in range(4):
            self._summary_counts[index] += int(current[index]) if current is not None else 0
            self._summary_counts[index] -= int(previous[index]) if previous is not None else 0
        if previous is not None:
            self._add_summary_progress(-previous[4])
        if current is not None:
            self._add_summary_progress(current[4])
            self._summary_rows[tid] = current
        else:
            self._summary_rows.pop(tid, None)
        if previous is None or current is None or previous[5] != current[5]:
            self._action_revision += 1
        if not self._summary_rows:
            self._progress_sum = self._progress_correction = 0.0

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

    def register_download_streams(self, tid, streams):
        """Register both streams before either parallel download can finish."""
        with self.tasks_lock:
            per_stream = self.stream_progress.setdefault(tid, {})
            for key, total in streams.items():
                per_stream.setdefault(key, (0, max(0, total or 0)))

    def report_stage_progress(self, tid, stage, fraction=0, *, status=None, download_metrics=None):
        start, end = PROGRESS_STAGES[stage]
        fraction = float(fraction)
        if not math.isfinite(fraction):
            fraction = 0
        fraction = max(0, min(1, fraction))
        self._task_update(tid, status=status, progress=start + (end - start) * fraction,
                          progress_stage=stage, download_metrics=download_metrics)
        self.set_log_stage(tid, {'downloading': 'download', 'processing': 'convert',
                                 'trimming': 'trim', 'verification': 'verify',
                                 'publishing': 'publish'}[stage], progress_stage=stage)

    def begin_task_attempt(self, tid):
        """Count actual executions, retaining the count when a task is requeued."""
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if task is None:
                return None
            task['attempt'] = task.get('attempt', 0) + 1
            task['_log_stage'] = 'resolve'
            return task['attempt']

    def task_log_fields(self, tid):
        with self.tasks_lock:
            task = self.tasks.get(tid) or {}
            return {'task': tid, 'attempt': task.get('attempt', 0),
                    'stage': task.get('_log_stage', 'resolve')}

    def set_log_stage(self, tid, stage, *, progress_stage=None):
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if task is None or task.get('_log_stage') == stage:
                return
            if progress_stage is not None and task.get('_progress_stage') != progress_stage:
                return
            task['_log_stage'] = stage
            fields = {'task': tid, 'attempt': task.get('attempt', 0), 'stage': stage}
        log_error('Task stage changed', level='DEBUG', event='task_stage_changed',
                  component='task_manager', **fields)

    def cancel_task(self, tid):
        return self._cancel_task_by_tid(tid)

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

    def add_urls(self, urls, options, *, resolved_info=None, notify=True):
        if self._closing:
            return []
        added, skipped = [], 0
        option_fields = asdict(options)
        for url in urls:
            norm = normalize_url(url)
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
                task = {**option_fields, "id": tid, "url": norm,
                        "title": "(resolving title...)", "status": "Queued",
                        "progress": 0, "output_path": None, "attempt": 0}
                self.tasks[tid] = task
                self._sync_summary_task(tid)
                if info is not None:
                    self._resolved_info.put(tid, info, info_size)
                self.active_urls.add(norm)
                self._active_url_owners[norm] = tid
                self.cancel_events[tid] = threading.Event()
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
                if slot < self.worker_count and len(self.tid_to_workerid) < self.worker_count:
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
        if not self._closing:
            self.paused = not self.paused
            self.emit("pause_changed", paused=self.paused)
        return self.paused

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

    def _task_update(self, tid, title=None, status=None, progress=None, output_path=None,
                     progress_stage=None, download_metrics=None):
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if task is None:
                return
            if progress_stage is not None:
                if task["status"].startswith(("Completed", "Error", "Cancel")):
                    return
                current_stage = task.get("_progress_stage", "downloading")
                if PROGRESS_STAGES[progress_stage][0] < PROGRESS_STAGES[current_stage][0]:
                    return  # Ignore delayed callbacks from an earlier stage.
                task["_progress_stage"] = progress_stage
            if title is not None:
                task["title"] = title
            if status is not None:
                if status.startswith("Completed") and task.get("warnings"):
                    status += " — Warning: " + "; ".join(task["warnings"])
                task["status"] = status
            if progress is not None:
                complete = task["status"].startswith("Completed") and bool(output_path or task.get("output_path"))
                ceiling = 100 if complete else 99
                value = float(progress)
                if not math.isfinite(value):
                    value = task.get("progress", 0)
                task["progress"] = max(task.get("progress", 0), max(0, min(ceiling, value)))
            if output_path is not None:
                task["output_path"] = output_path
            if download_metrics is not None:
                task.update(download_metrics)
            if (task.get("_progress_stage", "downloading") != "downloading"
                    or not task["status"].startswith("Downloading")):
                task["speed_bps"] = None
                task["eta_seconds"] = None
            self._sync_summary_task(tid)
            self._emit_task("task_updated", tid, task)
        if progress is not None:
            self._update_global_progress()

    def _task_error(self, tid, errmsg):
        self._log_task_error(tid, errmsg)
        self._task_update(tid, status=f"Error: {errmsg}")
        self._update_global_progress()
        self._update_queue_label()

    def _update_global_progress(self):
        self.emit("progress_changed")

    def _update_queue_label(self):
        self.emit("queue_changed")

    def _notify(self, text):
        self.emit("notice", message=text)

    def close(self, *, require_idle=False):
        """Begin shutdown; updates may atomically refuse to interrupt any work."""
        with self.tasks_lock:
            if self._closing:
                return not require_idle
            if require_idle and self._has_update_work_locked():
                return False
            self._closing = True
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
                ev = threading.Event()
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

    def _log_task_error(self, tid, errmsg):
        """Log task errors with URL, options, worker and runtime version context."""
        with self.tasks_lock:
            t = dict(self.tasks.get(tid) or {})
            wid = self.tid_to_workerid.get(tid)
        title = t.get("title")
        if title == "(resolving title...)":
            title = None
        trim = ""
        if t.get("trim_start") or t.get("trim_end"):
            trim = f"{t.get('trim_start') or '0:00'} - {t.get('trim_end') or 'end'}"
        options_text = ""
        if t.get("format"):
            options_text = f"{t.get('format')} | quality: {t.get('quality')} | codec: {t.get('codec_mode')}"
        log_error(f"ERROR {tid}", details=[
            ("error", errmsg), ("link", t.get("url")), ("title", title), ("settings", options_text),
            ("bitrate", t.get("mp3_bitrate")), ("folder", t.get("folder")),
            ("output mode", t.get("output_mode")), ("container", t.get("container")),
            ("video codec", t.get("video_codec")), ("audio codec", t.get("audio_codec")),
            ("audio bit depth", t.get("audio_bit_depth")),
            ("conversion bitrate", t.get("audio_bitrate")), ("conversion quality", t.get("video_quality")),
            ("trim", trim), ("worker", wid), ("version", env_info()),
        ], tb=unexpected_traceback(), event='task_failed', component='task_manager',
            task=tid, attempt=t.get('attempt', 0), stage=t.get('_log_stage', 'resolve'))

    def _on_download_progress(self, tid, stream_key, downloaded, total, *, force=False):
        try:
            with self.tasks_lock:
                task = self.tasks.get(tid)
                if (task is None or task["status"].startswith(("Completed", "Error", "Cancel"))
                        or task.get("_progress_stage", "downloading") != "downloading"):
                    return
                lock = self._download_progress_locks.get(tid)
                if lock is None:
                    lock = self._download_progress_locks[tid] = threading.Lock()
            # Serialize publication for this task without blocking other tasks
            # behind its speed calculation or stage notification.
            with lock:
                ts = time.monotonic()
                with self.tasks_lock:
                    task = self.tasks.get(tid)
                    if (task is None or task["status"].startswith(("Completed", "Error", "Cancel"))
                            or task.get("_progress_stage", "downloading") != "downloading"):
                        return
                    per_stream = self.stream_progress.setdefault(tid, {})
                    previous = per_stream.get(stream_key)
                    downloaded, total = max(0, int(downloaded or 0)), max(0, int(total or 0))
                    restarted = previous is not None and downloaded < previous[0]
                    finished = total > 0 and downloaded >= total and (
                        previous is None or previous[1] <= 0 or previous[0] < previous[1])
                    per_stream[stream_key] = (downloaded, total)
                    downloaded_sum = sum(d for d, t in per_stream.values())
                    total_sum = sum(t for d, t in per_stream.values())
                    known_totals = all(t > 0 for d, t in per_stream.values())
                    task["downloaded_bytes"] = downloaded_sum
                    task["total_bytes"] = total_sum
                    last_report = task.get("_download_report_at")
                    if (not force and not restarted and not finished
                            and task["status"].startswith("Downloading") and last_report is not None
                            and ts - last_report < DOWNLOAD_PROGRESS_INTERVAL):
                        return
                    task["_download_report_at"] = ts
                    percent_of_download = (min(100, downloaded_sum / total_sum * 100)
                                           if known_totals and total_sum else 0)
                    hist = self.download_speed_history.get(tid)
                    if hist is None:
                        hist = self.download_speed_history[tid] = deque(maxlen=DOWNLOAD_SPEED_SAMPLES)
                    if restarted or hist and downloaded_sum < hist[-1][1]:
                        hist.clear()
                    hist.append((ts, downloaded_sum))
                    while hist and ts - hist[0][0] > DOWNLOAD_SPEED_WINDOW:
                        hist.popleft()
                    speed_bps = 0.0
                    eta_seconds = None
                    if len(hist) >= 2:
                        dt = hist[-1][0] - hist[0][0]
                        db = max(0, hist[-1][1] - hist[0][1])
                        speed_bps = db / dt if dt > 0 else 0
                        if known_totals and speed_bps > 0:
                            eta_seconds = max(0, total_sum - downloaded_sum) / speed_bps

                    status = (
                        "Downloading - "
                        f"{format_mb(downloaded_sum)}/{format_mb(total_sum)} - "
                        f"{speed_bps * 8 / 1e6:.2f} Mbps - Time left: {format_eta(eta_seconds)}"
                    )
                self.report_stage_progress(tid, "downloading", percent_of_download / 100, status=status,
                                           download_metrics={"speed_bps": speed_bps, "eta_seconds": eta_seconds})
        except Exception as error:
            log_warning("Could not update download progress", error, [("task", tid)], once_key=("_on_download_progress", tid))

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
                self.cancel_events[tid] = threading.Event()
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

