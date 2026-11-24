"""Task state transitions, aggregate progress and download event accounting."""
import math
import time
import threading
from collections import deque
from queue_store import task_state
from utils import format_eta, format_mb, log_error, log_warning, unexpected_traceback, env_info
from task_types import (
    DOWNLOAD_PROGRESS_INTERVAL,
    DOWNLOAD_SPEED_SAMPLES,
    DOWNLOAD_SPEED_WINDOW,
    PROGRESS_STAGES,
)


class TaskProgressMixin:
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
        durable = None
        if task is not None:
            state = task_state(task, self._restart_after_shutdown, tid)
            durable = (state, task.get('title'), task.get('output_path') if state == 'completed' else None,
                       task['status'] if state in ('completed', 'failed') else '')
        if self._queue_rows.get(tid) != durable:
            self._queue_revision += 1
            if durable is None:
                self._queue_rows.pop(tid, None)
            else:
                self._queue_rows[tid] = durable
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
            self._queue_revision += 1
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
