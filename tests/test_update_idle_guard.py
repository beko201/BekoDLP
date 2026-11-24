"""Updating must never enter cancellation shutdown while task work exists."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close


class UpdateIdleGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        with patch.object(self.manager, 'set_worker_count'):
            self.window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        self.stage = Mock()
        self.window.updater.pending_install = self.stage

    def tearDown(self):
        self.window._update_on_close = False
        for box in list(self.window._message_boxes):
            box.done(QMessageBox.StandardButton.Ok)
        finish_window_close(self.window)
        self.window.controller.poll()
        for _ in range(5):
            self.app.processEvents()
        self.temp.cleanup()

    def add_task(self):
        return self.manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.folder)))[0]

    def assert_install_blocked(self, tid=None):
        event = self.manager.cancel_events.get(tid) if tid else None
        self.window.updater._install_requested()
        self.assertFalse(self.window._closing)
        self.assertFalse(self.window._update_on_close)
        self.assertFalse(self.manager._closing)
        self.assertFalse(self.manager._shutdown_event.is_set())
        self.assertFalse(self.window.preferences._closed)
        self.assertIs(self.window.updater.pending_install, self.stage)
        self.stage.discard.assert_not_called()
        if event:
            self.assertFalse(event.is_set())

    def test_real_active_download_and_conversion_block_with_zero_queued_count(self):
        for status in ('Downloading', 'Converting selected codecs'):
            with self.subTest(status=status):
                tid = self.add_task()
                started, release = threading.Event(), threading.Event()
                def process(task_tuple, worker_id):
                    self.manager._task_update(tid, status=status)
                    started.set()
                    release.wait(5)
                    self.manager._task_update(tid, status='Completed')
                self.manager.engine.process_task.side_effect = process
                worker = threading.Thread(target=self.manager._process_task,
                                          args=((tid, 'https://example.com/video'), 1))
                worker.start()
                try:
                    self.assertTrue(started.wait(2))
                    self.assertEqual(self.manager.summary().pending, 0)
                    self.assert_install_blocked(tid)
                finally:
                    release.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())

    def test_paused_queue_pending_resolver_and_terminal_worker_cleanup_block(self):
        tid = self.add_task()
        self.manager.paused = True
        self.assert_install_blocked(tid)
        self.manager._task_update(tid, status='Completed')
        with self.manager.tasks_lock:
            self.manager._pending_link_keys.add('resolving-link')
        self.assertEqual(self.manager.summary().pending, 0)
        self.assert_install_blocked(tid)
        with self.manager.tasks_lock:
            self.manager._pending_link_keys.clear()
            self.manager.tid_to_workerid[tid] = 1
        self.assert_install_blocked(tid)
        with self.manager.tasks_lock:
            self.manager.tid_to_workerid.clear()

    def test_close_event_rechecks_even_when_caller_bypasses_updater_guard(self):
        tid = self.add_task()
        self.manager._task_update(tid, status='Downloading')
        self.assertEqual(self.manager.summary().pending, 0)
        self.window._update_on_close = True
        self.window.close()
        self.assertFalse(self.window._closing)
        self.assertFalse(self.manager._shutdown_event.is_set())
        self.assertFalse(self.window._update_on_close)
        self.assertFalse(self.window.preferences._closed)

    def test_work_arriving_during_preference_flush_is_atomically_refused(self):
        original_flush = self.window.preferences.flush
        def flush_then_enqueue():
            result = original_flush()
            self.add_task()
            return result
        with patch.object(self.window.preferences, 'flush', side_effect=flush_then_enqueue):
            self.assert_install_blocked()
        self.assertEqual(self.manager.summary().pending, 1)
        # An aborted update must leave preference saving usable.
        self.window.preferences.schedule({'workers': 2})
        self.assertTrue(self.window.preferences.flush())

    def test_idle_update_reserves_shutdown_and_rejects_late_submissions(self):
        tid = self.add_task()
        self.manager._task_update(tid, status='Completed')
        self.assertFalse(self.manager.has_update_work())
        self.assertTrue(self.manager.close(require_idle=True))
        self.assertEqual(self.manager.add_urls(['https://example.com/late'], TaskOptions(folder=str(self.folder))), [])
        self.assertIsNone(self.manager.submit_link('https://example.com/late', TaskOptions(folder=str(self.folder))))

    def test_preference_save_failure_defers_update_without_closing_manager(self):
        with patch.object(self.window.preferences, 'flush', return_value=False):
            self.assert_install_blocked()
        self.assertFalse(self.window._preferences_closed)

    def test_idle_install_starts_helper_only_after_clean_shutdown(self):
        tid = self.add_task()
        self.manager._task_update(tid, status='Completed')
        def verify_saved_queue(stage):
            persistence = self.window.queue_persistence
            self.assertTrue(persistence._closed)
            self.assertEqual(persistence._saved_revision, self.manager.queue_revision)
        with patch('ui.updates.launch_update', side_effect=verify_saved_queue) as launch:
            self.window.updater._install_requested()
            self.assertTrue(self.manager._closing)
            finish_window_close(self.window)
            launch.assert_called_once_with(self.stage)
        self.assertTrue(self.window._allow_close)
        self.assertIsNone(self.window.updater.pending_install)
        self.stage.discard.assert_not_called()
