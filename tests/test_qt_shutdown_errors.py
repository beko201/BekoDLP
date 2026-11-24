"""Qt shutdown deadlines, real managed processes and unexpected failure reporting."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

import download_engine
from download_engine import DownloadEngine
from task_manager import TaskManager, TaskOptions
from ui.exception_reporter import QtExceptionReporter
from ui.main_window import MainWindow
from qt_support import finish_window_close
from utils import DownloadCancelled, NO_WINDOW, run_cmd_capture


class GateEngine:
    def __init__(self, manager):
        self.manager = manager
        self.started = threading.Event()
        self.release = threading.Event()
        self.force_release = threading.Event()
        self.block_force = False
        self.workspace = None

    def process_task(self, task_tuple, worker_id):
        tid, _ = task_tuple
        folder = self.manager.get_task(tid)["folder"]
        self.workspace = Path(tempfile.mkdtemp(prefix=".yd-test-", dir=folder))
        partial = self.workspace / "partial.part"
        partial.write_bytes(b"in-progress data")
        self.manager.update_task(tid, status="Processing")
        self.started.set()
        self.release.wait(10)
        partial.unlink()
        self.workspace.rmdir()
        self.manager.update_task(tid, status="Cancelled")

    def terminate_managed_processes(self):
        if self.block_force:
            self.force_release.wait(10)


class CaptureEngine(DownloadEngine):
    def __init__(self, manager):
        super().__init__(manager)
        self.started = threading.Event()
        self.process = None

    def process_task(self, task_tuple, worker_id):
        tid, _ = task_tuple
        def register(proc):
            self.process = proc
            self._register_process(proc)
            self.started.set()
        try:
            run_cmd_capture([sys.executable, "-c", "import time; time.sleep(30)"],
                            on_start=register, on_finish=self._unregister_process)
        except subprocess.CalledProcessError:
            self.manager.update_task(tid, status="Cancelled")


class QtShutdownErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.managers = []
        self.windows = []

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt shutdown operation timed out")

    def make_window(self, factory=GateEngine, grace=.04, force=.04):
        manager = TaskManager(engine_factory=factory, shutdown_grace_seconds=grace,
                              shutdown_force_seconds=force)
        self.managers.append(manager)
        window = MainWindow(manager, settings_path=self.folder / "settings.json")
        self.windows.append(window)
        window.show()
        return window, manager

    def start_task(self, manager):
        tid = manager.add_urls(["https://example.com/task"], TaskOptions(folder=str(self.folder)))[0]
        self.wait_until(manager.engine.started.is_set)
        return tid

    def shutdown_box(self, window):
        return next((box for box in window._message_boxes
                     if box.windowTitle() == "Some tasks have not stopped"), None)

    def tearDown(self):
        for manager in self.managers:
            for name in ("release", "force_release"):
                event = getattr(manager.engine, name, None)
                if event is not None:
                    event.set()
            manager.close()
            manager.engine.terminate_managed_processes()
        for window in self.windows:
            finish_window_close(window)
        self.wait_until(lambda: all(manager.is_idle() for manager in self.managers))
        for window in self.windows:
            window.controller.poll()
        self.app.processEvents()

    def test_timeout_wait_dismissal_and_force_close_keep_owned_partial_files_until_worker_exits(self):
        window, manager = self.make_window(grace=.08, force=.03)
        self.start_task(manager)
        existing = self.folder / "completed.mp4"
        existing.write_bytes(b"completed")
        ticks = []
        heartbeat = QTimer(window)
        heartbeat.setInterval(5)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        completed = []
        window.controller.shutdown_complete.connect(lambda: completed.append(1))
        window.close()
        self.wait_until(lambda: self.shutdown_box(window) is not None)
        self.assertGreater(len(ticks), 2)
        self.shutdown_box(window).reject()  # Dismissing the prompt must mean wait.
        self.assertFalse(manager._shutdown_abandoned)
        self.assertTrue(window.isVisible())
        self.wait_until(lambda: self.shutdown_box(window) is not None)
        self.shutdown_box(window).done(QMessageBox.StandardButton.Yes)
        self.wait_until(lambda: not window.isVisible())
        self.assertTrue(manager._shutdown_abandoned)
        self.assertFalse(manager.is_idle())
        self.assertEqual((manager.engine.workspace / "partial.part").read_bytes(), b"in-progress data")
        self.assertEqual(existing.read_bytes(), b"completed")
        window.controller.poll()
        window.controller.resolve_shutdown_choice(True)
        self.assertEqual(completed, [1])

    def test_cleanup_finishing_while_prompt_is_open_closes_without_extending_deadline(self):
        window, manager = self.make_window()
        self.start_task(manager)
        window.close()
        self.wait_until(lambda: self.shutdown_box(window) is not None)
        deadline = manager._shutdown_deadline
        manager.engine.release.set()
        self.wait_until(lambda: not window.isVisible())
        self.assertFalse(window._message_boxes)
        self.assertEqual(manager._shutdown_deadline, deadline)
        self.assertFalse(manager._shutdown_abandoned)

    def test_stuck_termination_helper_cannot_block_the_qt_shutdown_choice(self):
        window, manager = self.make_window()
        manager.engine.block_force = True
        self.start_task(manager)
        window.close()
        self.wait_until(lambda: self.shutdown_box(window) is not None)
        self.assertTrue(manager._shutdown_force_thread.is_alive())
        self.shutdown_box(window).done(QMessageBox.StandardButton.Yes)
        self.wait_until(lambda: not window.isVisible())
        self.assertTrue(manager._shutdown_force_thread.is_alive())

    def test_real_managed_capture_is_killed_after_grace_without_killing_other_processes(self):
        window, manager = self.make_window(CaptureEngine, grace=.03, force=1)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **NO_WINDOW)
        try:
            self.start_task(manager)
            window.close()
            self.wait_until(lambda: not window.isVisible())
            self.assertTrue(manager.engine._force_stopping)
            self.assertIsNotNone(manager.engine.process.poll())
            self.assertFalse(manager.engine.managed_processes)
            self.assertIsNone(other.poll())
            self.assertTrue(manager.is_idle())
        finally:
            other.kill()
            other.wait(timeout=3)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_paused_ffmpeg_is_cancelled_and_workspace_is_cleaned_through_qt(self):
        source = self.folder / "source.wav"
        target = self.folder / "downloads"
        target.mkdir()
        protected = target / "existing.mp3"
        protected.write_bytes(b"keep")
        subprocess.run([shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=15", str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        window, manager = self.make_window(DownloadEngine, grace=2, force=1)
        processes = []
        register = manager.engine._register_process
        def record(proc):
            processes.append(proc)
            register(proc)
        def download(tid, key, url, stream, filename, *, folder):
            result = Path(folder, filename)
            shutil.copyfile(source, result)
            return str(result)
        def convert(tid, wid, input_path, output_path):
            manager.engine._run_ffmpeg(tid, wid, [manager.engine.ffmpeg, "-y", "-re", "-i",
                                               input_path, "-vn", "-ab", "128k", output_path], duration=15)
        info = download_engine.YTInfo("offline", {"title": "Paused conversion", "duration": 15,
            "formats": [{"format_id": "audio", "ext": "wav", "vcodec": "none",
                         "acodec": "pcm_s16le", "filesize": source.stat().st_size}]})
        with patch.object(manager.engine, "_register_process", side_effect=record), \
                patch.object(manager.engine, "_download_format", side_effect=download), \
                patch.object(manager.engine, "_ffmpeg_convert_to_mp3", side_effect=convert), \
                patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info):
            tid = manager.add_urls(["https://example.com/audio"], TaskOptions(folder=str(target), format="MP3"))[0]
            self.wait_until(lambda: manager.engine.current_process_pids)
            window.pause_button.click()
            self.wait_until(lambda: manager.get_task(tid)["status"] == "Paused")
            self.assertTrue(any(p.poll() is None for p in processes))
            window.close()
            self.wait_until(lambda: not window.isVisible())
        self.assertEqual(manager.get_task(tid)["status"], "Cancelled")
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertFalse(manager.engine.managed_processes or manager.engine.current_process_pids)
        self.assertEqual(list(target.iterdir()), [protected])
        self.assertEqual(protected.read_bytes(), b"keep")

    def test_error_burst_opens_one_dialog_and_close_discards_remaining_notifications(self):
        window, manager = self.make_window()
        for i in range(10):
            manager.emit("error", message=f"offline error {i}")
        window.controller.poll()
        self.assertEqual(len(window._message_boxes), 1)
        self.assertEqual(len(window._pending_messages), 9)
        next(iter(window._message_boxes)).done(QMessageBox.StandardButton.Ok)
        self.wait_until(lambda: len(window._message_boxes) == 1)
        self.assertEqual(len(window._pending_messages), 8)
        window.open_bulk_dialog()
        window.close()
        self.wait_until(lambda: not window.isVisible())
        self.assertFalse(window._message_boxes or window._pending_messages)
        self.assertFalse(window.bulk_dialog.isVisible())

    def test_callback_and_background_exceptions_are_logged_and_displayed_on_gui_thread(self):
        window, _ = self.make_window()
        reporter = QtExceptionReporter()
        reporter.message.connect(window._show_message, Qt.ConnectionType.QueuedConnection)
        original_sys, original_thread = sys.excepthook, threading.excepthook
        reporter.install()
        try:
            with patch("ui.exception_reporter.log_error") as log:
                button = QPushButton(window)
                def fail():
                    raise RuntimeError("callback failure")
                button.clicked.connect(fail)
                button.click()  # PySide invokes sys.excepthook for a failing slot.
                self.wait_until(lambda: len(window._message_boxes) == 1)
                self.assertIn("callback failure", next(iter(window._message_boxes)).detailedText())
                next(iter(window._message_boxes)).done(QMessageBox.StandardButton.Ok)
                owner = threading.get_ident()
                delivery_threads = []
                def receive(*_):
                    delivery_threads.append(threading.get_ident())
                reporter.message.connect(receive, Qt.ConnectionType.QueuedConnection)
                def fail_thread():
                    raise RuntimeError("background failure")
                thread = threading.Thread(target=fail_thread)
                thread.start()
                self.wait_until(lambda: not thread.is_alive() and len(window._message_boxes) == 1)
                self.assertIn("background failure", next(iter(window._message_boxes)).detailedText())
                self.assertEqual(delivery_threads, [owner])
                self.assertEqual(log.call_count, 2)
                try:
                    raise DownloadCancelled()
                except DownloadCancelled:
                    reporter._handle_sys(*sys.exc_info())
                self.assertEqual(log.call_count, 2)
        finally:
            reporter.restore()
        self.assertIs(sys.excepthook, original_sys)
        self.assertIs(threading.excepthook, original_thread)

    def test_close_during_task_dispatch_stops_further_ui_updates(self):
        window, manager = self.make_window()
        # Park workers before adding tasks so polling sees a reproducible batch.
        manager.set_worker_count(1)
        self.start_task(manager)
        manager.add_urls(["https://example.com/next"], TaskOptions(folder=str(self.folder)))
        seen = []
        def close_on_first(tid, task):
            seen.append(tid)
            window.close()
        window.controller.task_changed.connect(close_on_first)
        window.controller.poll()
        self.assertEqual(len(seen), 1)
        self.assertTrue(window._closing)


if __name__ == "__main__":
    unittest.main()
