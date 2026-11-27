"""Task callbacks and asynchronous shutdown remain isolated between windows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from task_manager import TaskManager, TaskOptions
from task_state import TaskState
from ui.main_window import MainWindow
from qt_support import finish_window_close


class WindowCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def window(self, name):
        folder = self.folder / name
        folder.mkdir()
        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=folder / 'settings.json')
        self.addCleanup(finish_window_close, window)
        window.folder = str(folder)
        window.show()
        return window, manager

    def add(self, window, manager, name):
        tid = manager.add_urls([f'https://example.com/{name}'], TaskOptions(folder=window.folder))[0]
        window.controller.poll()
        window.task_table.view.selectRow(0)
        return tid

    def test_task_buttons_route_to_their_own_window(self):
        first, first_manager = self.window('first')
        second, second_manager = self.window('second')
        first_tid = self.add(first, first_manager, 'first')
        second_tid = self.add(second, second_manager, 'second')
        first.cancel_action.trigger()
        self.assertEqual(first_manager.get_task(first_tid)['state'], TaskState.CANCELLED)
        self.assertEqual(second_manager.get_task(second_tid)['state'], TaskState.QUEUED)
        first.controller.poll()
        first.clear_button.click()
        self.assertIsNone(first_manager.get_task(first_tid))
        self.assertIsNotNone(second_manager.get_task(second_tid))

    def test_closing_one_window_preserves_other_window_actions_and_dialogs(self):
        first, first_manager = self.window('first')
        second, second_manager = self.window('second')
        first.open_bulk_dialog()
        second.open_bulk_dialog()
        finish_window_close(first)
        self.assertTrue(first._allow_close)
        self.assertFalse(first.bulk_dialog.isVisible())
        self.assertFalse(second._closing)
        self.assertFalse(second_manager._closing)
        self.assertTrue(second.bulk_dialog.isVisible())
        self.assertTrue(second.centralWidget().isEnabled())
        tid = self.add(second, second_manager, 'after-first-closed')
        second.cancel_action.trigger()
        self.assertEqual(second_manager.get_task(tid)['state'], TaskState.CANCELLED)

    def test_update_handoff_waits_for_final_queue_save(self):
        window, manager = self.window('updating')
        tid = self.add(window, manager, 'completed')
        manager.update_task(tid, state=TaskState.COMPLETED, status='Completed',
                            output_path=str(self.folder / 'video.mp4'))
        window._update_on_close = True
        observed = []

        def apply():
            observed.append((window.queue_persistence._closed,
                             window.queue_persistence._saved_revision == manager.queue_revision))

        with patch.object(window.updater, 'apply_on_exit', side_effect=apply):
            finish_window_close(window)
        self.assertEqual(observed, [(True, True)])
        self.assertTrue(window._allow_close)
