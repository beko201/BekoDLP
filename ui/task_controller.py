"""Qt adapter for the existing, toolkit-independent task manager."""
from queue import Empty
import re
import threading

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from task_manager import TaskOptions
from utils import log_error, unexpected_traceback
from utils import DownloadCancelled


def task_status(task):
    """Present engine states as concise task-table labels."""
    status = task.get("status", "")
    if status.startswith("Completed"):
        return "Completed with warnings" if task.get("warnings") else "Completed"
    if status.startswith("Error"):
        return "Failed (see details)"
    if status == "Cancelled":
        return "Cancelled"
    if status.startswith("Cancel"):
        return "Cancelling…"
    if status == "Paused":
        return "Paused"
    if status == "Waiting for video conversion slot":
        return "Waiting for conversion"
    if status == "Waiting for download connection slot":
        return "Waiting for connection"
    if "Converting selected codecs" in status:
        return "Converting"
    if "Copying streams" in status:
        return "Remuxing"
    if status.startswith(("Queu", "Wait")):
        return "Queued"
    return {
        "downloading": "Downloading",
        "processing": "Processing",
        "trimming": "Trimming",
        "verification": "Verifying compatibility",
        "publishing": "Saving",
    }.get(task.get("_progress_stage"), "Starting…")


def notice_text(message):
    """Summarize engine notifications for the status bar."""
    queued = re.match(r"(\d+) videos added to the queue!", message)
    retried = re.match(r"(\d+) tasks queued for retry\.", message)
    if queued or retried:
        count = (queued or retried)[1]
        text = f"{count} video(s) added to the queue." if queued else f"{count} task(s) queued for retry."
        skipped = re.search(r"\((\d+) skipped", message)
        if skipped:
            text += f" {skipped[1]} skipped: the URL is already queued or active."
        return text
    if "failed" in message and "No" in message:
        return "No failed tasks to retry."
    if message.startswith("No tasks could be retried"):
        return "No tasks could be retried. The URL may already be active, or cleanup is still running."
    if message.endswith(" cancelled."):
        return "Task cancelled."
    if " downloaded (" in message:
        return "Download completed."
    return "Task status updated."


class TaskController(QObject):
    ACTIVE_INTERVAL = 100
    IDLE_INTERVAL = 500
    SHUTDOWN_INTERVAL = 50
    _events_ready = Signal()
    tasks_changed = Signal(object)
    tasks_removed = Signal(object)
    task_changed = Signal(str, dict)
    task_removed = Signal(str)
    thumbnail_available = Signal(str, str)
    summary_changed = Signal(int, int, int)
    actions_changed = Signal()
    pause_changed = Signal(bool)
    notice = Signal(str)
    message = Signal(str, str, str, str)
    shutdown_complete = Signal()
    shutdown_wait_required = Signal()
    encoders_changed = Signal(object)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.manager.use_compact_task_events()
        self.closing = False
        self._waiting_for_choice = False
        self._shutdown_finished = False
        self._last_action_revision = None
        self.timer = QTimer(self)
        self.timer.setInterval(self.IDLE_INTERVAL)
        self.timer.timeout.connect(self.poll)
        self._events_ready.connect(self._wake_for_events, Qt.ConnectionType.QueuedConnection)
        self.manager.events.set_wakeup(self._events_ready.emit)

    def _wake_for_events(self):
        if not self.closing and self.timer.isActive() and self.timer.interval() == self.IDLE_INTERVAL:
            self.timer.setInterval(self.ACTIVE_INTERVAL)
            self.poll()

    def _update_poll_interval(self):
        interval = (self.ACTIVE_INTERVAL if self.manager.has_ui_work() or not self.manager.events.empty()
                    else self.IDLE_INTERVAL)
        if self.timer.interval() != interval:
            self.timer.setInterval(interval)

    def start(self, worker_count):
        self.manager.set_worker_count(worker_count)
        tasks = self.manager.list_tasks()
        self.tasks_changed.emit([(task["id"], task) for task in tasks])
        for task in tasks:
            if self.closing:
                return
            self.task_changed.emit(task["id"], task)
        self._publish_summary()
        self._update_poll_interval()
        self.timer.start()

    def load_encoders(self):
        if self.closing or not callable(getattr(type(self.manager.engine), "available_encoders", None)):
            return
        def probe():
            try:
                encoders = self.manager.engine.available_encoders()
            except DownloadCancelled:
                return
            except Exception as error:
                if self.closing:
                    return
                log_error("Could not inspect FFmpeg encoders", [("error", error)], tb=unexpected_traceback(),
                          event='encoder_inspection_failed', component='qt')
                encoders = set()
            if not self.closing:
                self.encoders_changed.emit(encoders)
        self.manager.start_thread(threading.Thread(target=probe, name="ffmpeg-capabilities", daemon=True))

    def submit_links(self, links, *, expand_playlist, **options):
        if self.closing:
            return False
        # Freeze and validate all options before any resolver can run.
        task_options = TaskOptions(**options)
        accepted = 0
        for link in links:
            if self.manager.submit_link(link, task_options, expand_playlist=expand_playlist):
                accepted += 1
        if accepted:
            self.timer.setInterval(self.ACTIVE_INTERVAL)
            self.notice.emit(f"Resolving {accepted} link(s)…")
        elif links:
            self.notice.emit("These links are already queued, downloading or being resolved.")
        return bool(accepted)

    def set_worker_count(self, count):
        if not self.closing:
            self.manager.set_worker_count(count)

    def toggle_pause(self):
        if not self.closing:
            self.manager.toggle_pause()
            self._update_poll_interval()

    def cancel_tasks(self, tids):
        if not self.closing:
            count = sum(self.manager.cancel_task(tid) for tid in tids)
            self.notice.emit(f"Cancellation requested for {count} task(s).")

    def retry_failed(self, tids):
        if not self.closing:
            self.manager.retry_failed(tids)
            self._update_poll_interval()

    def clear_tasks(self, remove_pending=False):
        if not self.closing:
            removed = self.manager.clear_list(remove_pending=remove_pending)
            self.notice.emit(f"Removed {len(removed)} task(s)." if removed else "No tasks to clear.")

    def poll(self):
        if self.closing:
            self._poll_shutdown()
            return
        changed = {}
        removed = {}
        summary_needed = False
        messages = []
        latest_notice = None
        # Bound each poll so a busy event queue cannot monopolize the Qt event loop.
        for _ in range(100):
            try:
                event = self.manager.events.get_nowait()
            except Empty:
                break
            try:
                if event.kind in ("task_added", "task_updated"):
                    changed[event.tid] = None
                    summary_needed = True
                elif event.kind in ("task_removed", "tasks_removed"):
                    tids = event.data["tids"] if event.kind == "tasks_removed" else (event.tid,)
                    for tid in tids:
                        changed.pop(tid, None)
                        removed[tid] = None
                    summary_needed = True
                elif event.kind in ("progress_changed", "queue_changed"):
                    summary_needed = True
                elif event.kind == "pause_changed":
                    self.pause_changed.emit(event.data["paused"])
                elif event.kind == "playlist_blocked":
                    messages.append(("information", "Playlist link",
                                     "Use 'Add multiple links' to download a playlist or album.", ""))
                    summary_needed = True
                elif event.kind == "error":
                    messages.append(("critical", "Link resolution failed",
                                     "The link could not be resolved. Check the URL and your connection.",
                                     event.data.get("message", "")))
                    summary_needed = True
                elif event.kind == "info":
                    messages.append(("information", "Playlist information",
                                     "The playlist could not be fully expanded. Available videos were queued.",
                                     event.data.get("message", "")))
                elif event.kind == "notice":
                    latest_notice = notice_text(event.data.get("message", ""))
                    summary_needed = True
                elif event.kind == "thumbnail" and self.manager.get_task(event.tid) is not None:
                    self.thumbnail_available.emit(event.tid, event.data.get("url") or "")
            except Exception as error:
                log_error("Qt event dispatch failed", details=[("event", event.kind), ("error", error)],
                          tb=unexpected_traceback(), event='event_dispatch_failed', component='qt')
            finally:
                self.manager.events.task_done()
            if self.closing:
                return
        if removed:
            self.tasks_removed.emit(list(removed))
            for tid in removed:
                if self.closing:
                    return
                self.task_removed.emit(tid)
        tasks = [(task['id'], task) for task in self.manager.get_tasks(changed)] if changed else []
        if tasks:
            self.tasks_changed.emit(tasks)
            for tid, task in tasks:
                if self.closing:
                    return
                self.task_changed.emit(tid, task)
        if summary_needed:
            self._publish_summary()
        if latest_notice and not self.closing:
            self.notice.emit(latest_notice)
        for message in messages:
            if self.closing:
                return
            self.message.emit(*message)
        if not self.closing:
            self._update_poll_interval()

    def _publish_summary(self):
        if self.closing:
            return
        summary = self.manager.summary()
        self.summary_changed.emit(summary.total, summary.pending, summary.progress)
        if not self.closing and summary.action_revision != self._last_action_revision:
            self._last_action_revision = summary.action_revision
            self.actions_changed.emit()

    def begin_shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.manager.events.set_wakeup(None)
        self.timer.setInterval(self.SHUTDOWN_INTERVAL)
        self.manager.close()
        self._poll_shutdown()

    def _poll_shutdown(self):
        if self._shutdown_finished:
            return
        if self.manager.finish_shutdown():
            self._finish_shutdown()
        elif self.manager.shutdown_timed_out and not self._waiting_for_choice:
            self._waiting_for_choice = True
            self.shutdown_wait_required.emit()

    def resolve_shutdown_choice(self, close_now):
        if not self.closing or self._shutdown_finished or not self._waiting_for_choice:
            return
        if close_now:
            self.manager.finish_shutdown(force=True)
            self._finish_shutdown()
        else:
            self.manager.extend_shutdown_wait()
            self._waiting_for_choice = False

    def _finish_shutdown(self):
        self._shutdown_finished = True
        self.timer.stop()
        self.shutdown_complete.emit()
