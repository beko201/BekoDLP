"""Exercise the real Qt event loop and the existing task-manager boundary offline."""
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
from unittest.mock import Mock, patch

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import download_engine
import main
from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from ui.task_controller import TaskController
from utils import NO_WINDOW


class ControlledEngine:
    def __init__(self, manager):
        self.manager = manager
        self.received = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.wait_after_cancel = False

    def process_task(self, task_tuple, worker_id):
        tid, _ = task_tuple
        self.received.append(self.manager.get_task(tid))
        self.manager.report_stage_progress(tid, "processing", .5, status="Processing")
        self.started.set()
        while not self.release.wait(.005):
            if self.manager.is_cancelled(tid) and not self.wait_after_cancel:
                break
        if self.manager.is_cancelled(tid):
            self.manager.update_task(tid, status="Cancelled")
        else:
            self.manager.update_task(tid, title="Offline title", status="Completed (MP3)",
                                     progress=100, output_path="offline.mp3")

    def terminate_managed_processes(self):
        pass


class QtIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.managers = []
        self.windows = []

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt operation timed out")

    def make_manager(self, **kwargs):
        manager = TaskManager(engine_factory=ControlledEngine, **kwargs)
        self.managers.append(manager)
        return manager

    def make_window(self, manager=None):
        manager = manager if manager is not None else self.make_manager()
        window = MainWindow(manager, settings_path=self.folder / "settings.json")
        self.windows.append(window)
        window.folder = str(self.folder)
        window.show()
        return window

    def tearDown(self):
        for manager in self.managers:
            manager.engine.release.set()
            manager.close()
        for window in self.windows:
            finish_window_close(window)
        self.wait_until(lambda: all(manager.is_idle() for manager in self.managers))
        for manager in self.managers:
            manager.finish_shutdown()
        for window in self.windows:
            if not window._allow_close:
                window.controller.poll()
        self.app.processEvents()

    def test_single_link_freezes_options_and_renders_worker_updates_on_gui_thread(self):
        window = self.make_window()
        manager = window.controller.manager
        owner = threading.get_ident()
        callback_threads = []
        window.controller.task_changed.connect(lambda *_: callback_threads.append(threading.get_ident()))
        window.format_combo.setCurrentText("Audio")
        window.audio_codec_combo.setCurrentIndex(window.audio_codec_combo.findData("mp3"))
        window.bitrate_combo.setCurrentText("128k")
        window.url_edit.setText("https://example.com/a")
        with patch.object(download_engine, "_extract_urls_from_input", return_value=["https://example.com/a"]) as extract:
            window.enqueue()
            window.bitrate_combo.setCurrentText("320k")
            self.wait_until(manager.engine.started.is_set)
        self.assertFalse(extract.call_args.args[1])
        self.assertEqual(manager.engine.received[0]["mp3_bitrate"], "128k")
        self.assertEqual(manager.engine.received[0]["format"], "MP3")
        self.assertEqual(window.url_edit.text(), "")
        manager.engine.release.set()
        self.wait_until(lambda: window.task_table.model.index(0, 5).data() == "100%")
        self.assertEqual(window.task_table.model.index(0, 4).data(), "Completed")
        self.assertEqual(window.global_progress.value(), 100)
        self.assertTrue(callback_threads)
        self.assertEqual(set(callback_threads), {owner})

    def test_invalid_trim_and_missing_folder_do_not_start_resolution_or_clear_input(self):
        window = self.make_window()
        window.url_edit.setText("https://example.com/a")
        window.trim_start_edit.setText("2:00")
        window.trim_end_edit.setText("1:00")
        with patch.object(window.controller.manager, "submit_link") as submit, \
                patch.object(window, "_show_message") as message:
            window.enqueue()
            self.assertEqual(message.call_args.args[1], "Invalid trim range")
            window.folder = ""
            window.enqueue()
            self.assertEqual(message.call_args.args[1], "Download folder")
            submit.assert_not_called()
        self.assertEqual(window.url_edit.text(), "https://example.com/a")

    def test_bulk_submission_expands_playlists_and_retains_invalid_input(self):
        window = self.make_window()
        window.open_bulk_dialog()
        dialog = window.bulk_dialog
        dialog.links_edit.setPlainText(" https://example.com/a \n\nhttps://example.com/b")
        with patch.object(window.controller.manager, "submit_link", return_value=True) as submit:
            window.trim_end_edit.setText("invalid")
            with patch.object(window, "_show_message"):
                dialog.submit_button.click()
            submit.assert_not_called()
            self.assertEqual(len(dialog.links()), 2)
            window.trim_end_edit.clear()
            dialog.submit_button.click()
        self.assertEqual(submit.call_count, 2)
        self.assertTrue(all(call.kwargs["expand_playlist"] for call in submit.call_args_list))
        self.assertIs(submit.call_args_list[0].args[1], submit.call_args_list[1].args[1])
        self.assertEqual(dialog.links(), [])
        self.assertFalse(dialog.isVisible())

    def test_coalescing_queue_accounting_and_removed_tasks_never_reappear(self):
        manager = self.make_manager()
        controller = TaskController(manager)
        changed, removed, summaries = [], [], []
        controller.task_changed.connect(lambda tid, task: changed.append((tid, task)))
        controller.task_removed.connect(removed.append)
        controller.summary_changed.connect(lambda *args: summaries.append(args))
        tid = manager.add_urls(["https://example.com/a"], TaskOptions(folder=str(self.folder)))[0]
        for step in range(5000):
            manager.report_stage_progress(tid, "processing", step / 5000)
        controller.poll()
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(manager.events.unfinished_tasks, 0)
        manager.update_task(tid, status="Error: offline failure")
        manager.clear_list()
        controller.poll()
        self.assertEqual(len(changed), 1)
        self.assertEqual(removed, [tid])
        self.assertEqual(manager.events.unfinished_tasks, 0)

    def test_poll_has_bounded_batches_and_preserves_error_diagnostics(self):
        manager = self.make_manager()
        controller = TaskController(manager)
        messages = []
        controller.message.connect(lambda *args: messages.append(args))
        for _ in range(105):
            manager.emit("error", message="Could not resolve the URL: offline failure")
        controller.poll()
        self.assertEqual(len(messages), 100)
        self.assertEqual(manager.events.qsize(), 5)
        self.assertEqual(messages[0][1], "Link resolution failed")
        self.assertIn("offline failure", messages[0][3])
        controller.poll()
        self.assertEqual(len(messages), 105)
        self.assertEqual(manager.events.unfinished_tasks, 0)

    def test_idle_timer_wakes_immediately_for_background_tasks_and_errors(self):
        manager = self.make_manager()
        controller = TaskController(manager)
        self.addCleanup(lambda: controller.timer.stop())
        controller.start(1)
        self.assertEqual(controller.timer.interval(), 500)
        changed, messages = [], []
        controller.task_changed.connect(lambda tid, task: changed.append(tid))
        controller.message.connect(lambda *args: messages.append(args))
        thread = threading.Thread(target=lambda: manager.add_urls(
            ['https://example.com/wake'], TaskOptions(folder=str(self.folder))))
        thread.start()
        thread.join(2)
        self.wait_until(lambda: bool(changed), timeout=.4)
        self.assertEqual(controller.timer.interval(), 100)
        manager.engine.release.set()
        self.wait_until(lambda: not manager.tid_to_workerid)
        controller.poll()
        self.assertEqual(controller.timer.interval(), 500)
        manager.emit('error', message='Offline test failure')
        self.wait_until(lambda: bool(messages), timeout=.4)
        self.assertIn('Offline test failure', messages[0][3])
        controller.begin_shutdown()
        self.assertTrue(controller._shutdown_finished or controller.timer.interval() <= 100)

    def test_paused_queue_uses_idle_interval_and_resume_reactivates_polling(self):
        manager = self.make_manager()
        controller = TaskController(manager)
        self.addCleanup(lambda: controller.timer.stop())
        manager.toggle_pause()
        manager.add_urls(['https://example.com/paused'], TaskOptions(folder=str(self.folder)))
        controller.start(1)
        # The controlled engine can publish its initial state concurrently.
        # Pending events legitimately keep the timer active until consumed.
        def paused_idle():
            controller.poll()
            return controller.timer.interval() == 500
        self.wait_until(paused_idle)
        controller.toggle_pause()
        self.assertEqual(controller.timer.interval(), 100)

    def test_pause_cancel_retry_and_clear_use_manager_without_touching_active_tasks(self):
        window = self.make_window()
        manager = window.controller.manager
        tid = manager.add_urls(["https://example.com/a"], TaskOptions(folder=str(self.folder)))[0]
        self.wait_until(manager.engine.started.is_set)
        window.controller.poll()
        window.pause_button.click()
        self.wait_until(lambda: window.pause_button.text() == "Resume")
        self.assertTrue(manager.paused)
        window.clear_button.click()
        self.assertIsNotNone(manager.get_task(tid))
        window.task_table.view.selectRow(0)
        window.cancel_action.trigger()
        self.wait_until(lambda: manager.get_task(tid)["status"] == "Cancelled" and not manager.tid_to_workerid)
        window.controller.poll()
        window.clear_button.click()
        window.controller.poll()
        self.assertEqual(window.task_table.model.rowCount(), 0)
        self.assertFalse(window.retry_action.isEnabled())

    def test_closing_keeps_qt_responsive_and_waits_for_worker_cleanup(self):
        window = self.make_window()
        manager = window.controller.manager
        manager.engine.wait_after_cancel = True
        manager.add_urls(["https://example.com/a"], TaskOptions(folder=str(self.folder)))
        self.wait_until(manager.engine.started.is_set)
        ticks = []
        heartbeat = QTimer()
        heartbeat.setInterval(5)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        window.close()
        self.assertTrue(window.isVisible())
        self.assertTrue(window._closing)
        self.wait_until(lambda: len(ticks) >= 4)
        self.assertFalse(window._allow_close)
        manager.engine.release.set()
        self.wait_until(lambda: window._allow_close)
        heartbeat.stop()
        self.assertFalse(window.isVisible())
        self.assertTrue(manager.is_idle())
        self.assertFalse(window.controller.timer.isActive())

    def test_shutdown_timeout_prompts_once_and_only_explicit_wait_extends_deadline(self):
        manager = self.make_manager(shutdown_grace_seconds=2, shutdown_force_seconds=1)
        now = [0.0]
        manager._shutdown_clock = lambda: now[0]
        release = threading.Event()
        self.addCleanup(release.set)
        manager.start_thread(threading.Thread(target=lambda: release.wait(5)))
        controller = TaskController(manager)
        prompts, completed = [], []
        controller.shutdown_wait_required.connect(lambda: prompts.append(1))
        controller.shutdown_complete.connect(lambda: completed.append(1))
        controller.begin_shutdown()
        now[0] = 2
        controller.poll()
        now[0] = 3
        controller.poll()
        controller.poll()
        self.assertEqual(len(prompts), 1)
        controller.resolve_shutdown_choice(False)
        self.assertEqual(manager._shutdown_deadline, 5)
        controller.poll()
        self.assertEqual(len(prompts), 1)
        now[0] = 5
        controller.poll()
        self.assertEqual(len(prompts), 2)
        controller.resolve_shutdown_choice(True)
        self.assertEqual(completed, [1])
        self.assertTrue(manager._shutdown_abandoned)
        release.set()

    def test_startup_dependency_failure_reports_error_before_creating_window(self):
        with patch.object(main, "QApplication", return_value=self.app), \
                patch.object(main, "TaskManager", side_effect=RuntimeError("missing ffmpeg")), \
                patch.object(main, "QMessageBox") as message, \
                patch.object(main, "MainWindow") as window:
            self.assertEqual(main.main(), 1)
        window.assert_not_called()
        message.return_value.exec.assert_called_once()
        details = message.return_value.setDetailedText.call_args.args[0]
        self.assertIn('Traceback (most recent call last)', details)
        self.assertIn('RuntimeError: missing ffmpeg', details)
        message.return_value.setInformativeText.assert_called_once()

    def test_qt_entry_imports_without_tkinter(self):
        code = "import sys; sys.modules['tkinter'] = None; import main; assert 'gui' not in sys.modules"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=15, **NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_interface_startup_failure_closes_manager_and_restores_exception_hooks(self):
        manager = Mock()
        original_hook = sys.excepthook
        original_thread_hook = threading.excepthook
        with patch.object(main, "QApplication", return_value=self.app), \
                patch.object(main, "TaskManager", return_value=manager), \
                patch.object(main, "MainWindow", side_effect=RuntimeError("widget construction failed")), \
                patch.object(main, "QMessageBox") as message, \
                patch.object(main, "log_error") as log:
            self.assertEqual(main.main(), 1)
        manager.close.assert_called_once()
        message.return_value.exec.assert_called_once()
        self.assertIn("widget construction failed", log.call_args.kwargs["tb"])
        self.assertIs(sys.excepthook, original_hook)
        self.assertIs(threading.excepthook, original_thread_hook)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_mp3_conversion_reaches_completed_qt_row_and_preserves_output(self):
        source = self.folder / "source.wav"
        destination = self.folder / "downloads"
        destination.mkdir()
        subprocess.run([shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        manager = TaskManager()
        self.managers.append(manager)
        # Match the fixture teardown API without replacing any engine behavior.
        manager.engine.release = threading.Event()
        window = self.make_window(manager)
        window.folder = str(destination)
        window.format_combo.setCurrentText("Audio")
        window.audio_codec_combo.setCurrentIndex(window.audio_codec_combo.findData("mp3"))
        window.trim_start_edit.setText("0.1")
        window.trim_end_edit.setText("0.6")
        # This offline fixture replaces the full-source downloader, not FFmpeg's
        # network section downloader. Exercise the explicit full-download path.
        window.output_controls.trim_download_combo.setCurrentIndex(
            window.output_controls.trim_download_combo.findData('full'))
        info = download_engine.YTInfo("offline", {"title": "Offline audio", "duration": 1,
            "formats": [{"format_id": "audio", "ext": "wav", "vcodec": "none",
                         "acodec": "pcm_s16le", "filesize": source.stat().st_size}]})

        def local_download(tid, key, url, stream, filename, *, folder):
            target = Path(folder, filename)
            shutil.copyfile(source, target)
            return str(target)

        window.url_edit.setText("https://example.com/offline")
        with patch.object(download_engine, "_extract_urls_from_input", return_value=["https://example.com/offline"]), \
                patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info), \
                patch.object(manager.engine, "_download_format", side_effect=local_download):
            window.enqueue()
            self.wait_until(lambda: manager.get_task("T1") is not None
                            and manager.get_task("T1")["status"].startswith(("Completed", "Error"))
                            and not manager.tid_to_workerid)
        window.controller.poll()
        task = manager.get_task("T1")
        self.assertTrue(task["status"].startswith("Completed"), task["status"])
        output = Path(task["output_path"])
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(list(destination.iterdir()), [output])
        self.assertEqual(window.task_table.model.index(0, 4).data(), "Completed")
        self.assertEqual(window.global_progress.value(), 100)


if __name__ == "__main__":
    unittest.main()
