"""Power actions are always mocked: exercise queue eligibility and cancellable deadlines."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from task_manager import TaskManager, TaskOptions
from ui.completion_actions import CompletionActions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from power_actions import run_power_action


class CompletionActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager(engine_factory=lambda m: Mock())
        self.addCleanup(self.manager.close)
        self.now = 0
        self.execute = Mock()
        self.actions = CompletionActions(self.manager, self.execute, clock=lambda: self.now)
        self.addCleanup(self.actions.close)

    def add(self, name='task'):
        return self.manager.add_urls(['https://example.com/' + name], TaskOptions(folder=str(self.folder)))[0]

    def done(self, tid):
        self.manager.update_task(tid, status='Completed', progress=100, output_path=str(self.folder / (tid + '.mp4')))

    def test_empty_and_historical_queues_never_trigger_an_action(self):
        tid = self.add()
        self.done(tid)
        self.actions.arm('folder')
        self.actions.poll()
        self.execute.assert_not_called()
        fresh = self.add('fresh')
        self.done(fresh)
        self.actions.poll()
        self.execute.assert_called_once_with('folder', [str(self.folder / (fresh + '.mp4'))])
        self.actions.poll()
        self.execute.assert_called_once()

    def test_completion_waits_for_workers_and_resolvers_to_finish_cleanup(self):
        self.actions.arm('close')
        tid = self.add()
        self.done(tid)
        self.manager.tid_to_workerid[tid] = 1
        self.actions.poll()
        self.execute.assert_not_called()
        self.manager.tid_to_workerid.clear()
        self.manager._pending_link_keys.add(('pending', True))
        self.actions.poll()
        self.execute.assert_not_called()
        self.manager._pending_link_keys.clear()
        self.actions.poll()
        self.execute.assert_called_once()

    def test_power_countdown_can_be_cancelled(self):
        tid = self.add()
        self.actions.arm('shutdown')
        self.done(tid)
        self.actions.poll()
        self.assertIsNotNone(self.actions.dialog)
        self.execute.assert_not_called()
        self.actions.dialog.reject()
        self.now = 100
        self.actions.poll()
        self.execute.assert_not_called()
        self.assertEqual(self.actions.action, 'none')

    def test_power_deadline_executes_only_once_after_a_final_queue_check(self):
        tid = self.add()
        self.actions.arm('sleep')
        self.done(tid)
        self.actions.poll()
        self.now = 29
        self.actions.poll()
        self.execute.assert_not_called()
        self.now = 30
        self.actions.poll()
        self.actions.poll()
        self.execute.assert_called_once_with('sleep', [str(self.folder / (tid + '.mp4'))])

    def test_new_work_pause_failure_and_clear_cancel_a_pending_power_action(self):
        for change in ('new', 'pause', 'failure', 'clear', 'cancel'):
            with self.subTest(change=change):
                self.actions.reset()
                self.manager.clear_list(remove_pending=True)
                self.manager.paused = False
                tid = self.add(change)
                self.actions.arm('hibernate')
                self.done(tid)
                self.actions.poll()
                if change == 'new':
                    self.add('extra')
                elif change == 'pause':
                    self.manager.paused = True
                elif change == 'failure':
                    self.manager.update_task(tid, status='Error: offline')
                elif change == 'cancel':
                    self.manager.update_task(tid, status='Cancelled')
                else:
                    self.manager.clear_list()
                self.now += 100
                self.actions.poll()
                self.assertEqual(self.actions.action, 'none')
        self.execute.assert_not_called()

    def test_window_selection_stays_in_sync_when_the_action_changes(self):
        with patch.object(self.manager, 'set_worker_count'):
            window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        try:
            for value in ('folder', 'shutdown', 'close', 'none'):
                window.completion_combo.setCurrentIndex(window.completion_combo.findData(value))
                self.assertEqual(window.finish_actions.action, value)
                self.assertEqual(window.completion_combo.currentData(), value)
        finally:
            finish_window_close(window)
            window.controller.poll()
            self.app.processEvents()

    def test_closing_disarms_without_running_the_selected_action(self):
        tid = self.add()
        self.actions.arm('shutdown')
        self.done(tid)
        self.actions.poll()
        self.actions.close()
        self.now = 100
        self.actions.poll()
        self.execute.assert_not_called()

    def test_shutdown_command_never_forces_other_applications_to_close(self):
        with patch('power_actions.subprocess.run') as run:
            run_power_action('shutdown')
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ['/s', '/t', '0'])
        self.assertNotIn('/f', command)

    def test_invalid_power_action_is_rejected_before_any_system_call(self):
        with patch('power_actions.subprocess.run') as run:
            with self.assertRaises(ValueError):
                run_power_action('invalid')
        run.assert_not_called()

    def test_failed_or_timed_out_shutdown_is_reported_as_an_action_error(self):
        import subprocess
        for error in (subprocess.CalledProcessError(1, 'shutdown'), subprocess.TimeoutExpired('shutdown', 10)):
            with self.subTest(error=type(error).__name__), patch('power_actions.subprocess.run', side_effect=error):
                with self.assertRaises(OSError):
                    run_power_action('shutdown')
