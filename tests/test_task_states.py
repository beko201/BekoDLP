"""Lifecycle decisions use structured state even when display text disagrees."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from queue_store import QueueStore, task_state
from engine_runtime import EngineRuntime
from engine_transfers import MediaTransfers
from task_manager import TaskManager, TaskOptions
from task_state import TaskState, state_of, STOPPED_STATES
from ui.completion_actions import CompletionActions
from ui.main_window import MainWindow
from ui.task_controller import task_status
from qt_support import finish_window_close


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(self.manager.close)

    def add(self, name='video'):
        return self.manager.add_urls([f'https://example.com/{name}'],
                                     TaskOptions(folder=str(self.folder)))[0]

    def test_explicit_state_overrides_conflicting_display_text(self):
        for state in TaskState:
            with self.subTest(state=state):
                tid = self.add(state.value)
                self.manager.update_task(tid, state=state, status='Completed (display text only)')
                task = self.manager.get_task(tid)
                self.assertEqual(state_of(task), state)
                expected = {TaskState.COMPLETED: 'completed', TaskState.FAILED: 'failed',
                            TaskState.CANCELLED: 'cancelled', TaskState.CANCELLING: 'cancelled'}.get(state, 'pending')
                self.assertEqual(task_state(task), expected)
                self.assertEqual(self.manager.cancel_task(tid), state not in STOPPED_STATES)

    def test_localized_completed_task_ignores_delayed_progress_and_cancellation(self):
        tid = self.add()
        self.manager.update_task(tid, state=TaskState.COMPLETED, status='Başarıyla kaydedildi',
                                 output_path=str(self.folder / 'video.mp4'), progress=100)
        self.manager.report_stage_progress(tid, 'downloading', .5, status='Downloading late callback')
        self.manager.report_progress(tid, 'video', 50, 100, force=True)
        task = self.manager.get_task(tid)
        self.assertEqual(task['state'], TaskState.COMPLETED)
        self.assertEqual(task['status'], 'Başarıyla kaydedildi')
        self.assertEqual(task['progress'], 100)
        self.assertFalse(self.manager.cancel_task(tid))
        self.assertFalse(self.manager.has_update_work())
        self.assertEqual(self.manager.clear_list(), [tid])

    def test_completed_looking_active_task_is_not_idle_or_clearable(self):
        tid = self.add()
        self.manager.update_task(tid, state=TaskState.RUNNING, status='Completed soon', progress=100)
        self.assertTrue(self.manager.has_update_work())
        self.assertEqual(self.manager.get_task(tid)['progress'], 99)
        self.assertEqual(self.manager.clear_list(), [])
        self.assertEqual(self.manager.summary().clearable, 0)

    def test_localized_queued_and_waiting_states_keep_summary_and_clear_rules(self):
        queued, waiting = self.add('queued'), self.add('waiting')
        self.manager.update_task(queued, state=TaskState.QUEUED, status='Sırada')
        self.manager.update_task(waiting, state=TaskState.WAITING, status='Kaynak bekleniyor')
        self.assertEqual(self.manager.summary().pending, 2)
        self.assertEqual(self.manager.clear_list(remove_pending=True), [queued])
        self.assertEqual(self.manager.get_task(waiting)['state'], TaskState.WAITING)
        self.assertTrue(self.manager.cancel_task(waiting))
        self.assertEqual(self.manager.get_task(waiting)['state'], TaskState.CANCELLING)

    def test_failed_task_retries_with_localized_error_message(self):
        tid = self.add()
        self.manager.update_task(tid, state=TaskState.FAILED, status='Bağlantı kurulamadı')
        # Worker cleanup releases the URL before retry becomes possible.
        with self.manager.tasks_lock:
            self.manager._release_active_url(self.manager.tasks[tid]['url'], tid)
        self.assertEqual(self.manager.summary().retryable, 1)
        self.manager.retry_failed([tid])
        self.assertEqual(self.manager.get_task(tid)['state'], TaskState.QUEUED)
        self.assertEqual(self.manager.summary().failed, 0)

    def test_queue_roundtrip_uses_saved_state_and_preserves_display_details(self):
        expected = (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.RUNNING)
        tids = []
        for state in expected:
            tid = self.add(state.value)
            tids.append(tid)
            self.manager.update_task(tid, state=state, status='Completed is only a label',
                                     output_path='video.mp4' if state == TaskState.COMPLETED else None)
        store = QueueStore(self.folder / 'queue.json')
        store.save(self.manager.queue_snapshot()[1])
        document, warnings = store.load()
        restored = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(restored.close)
        restored.restore_queue(document)
        self.assertEqual(warnings, [])
        for tid, original in zip(tids, expected):
            state = TaskState.QUEUED if original == TaskState.RUNNING else original
            self.assertEqual(restored.get_task(tid)['state'], state)
        self.assertEqual(restored.get_task(tids[1])['status'], 'Completed is only a label')
        self.assertTrue(restored.paused)

    def test_legacy_status_only_api_still_adapts_existing_callers(self):
        tid = self.add()
        self.manager.update_task(tid, status='Error: offline')
        self.assertEqual(self.manager.get_task(tid)['state'], TaskState.FAILED)
        self.assertEqual(state_of({'status': 'Completed (MP4)'}), TaskState.COMPLETED)

    def test_invalid_state_does_not_partially_update_task(self):
        tid = self.add()
        before = self.manager.get_task(tid)
        with self.assertRaises(ValueError):
            self.manager.update_task(tid, state='invalid', status='Completed')
        self.assertEqual(self.manager.get_task(tid), before)

    def test_download_pause_restores_state_even_with_conflicting_display_text(self):
        tid = self.add()
        self.manager.update_task(tid, state=TaskState.DOWNLOADING, status='Completed is only a label')
        runtime = EngineRuntime(ffmpeg=None, ffprobe=None)
        runtime.processing_limits = Mock()
        engine = MediaTransfers(runtime, self.manager, Mock())
        with patch('engine_transfers.YtDlpProcess') as process:
            engine.ytdlp_process({}, tid)
            tick = process.call_args.kwargs['on_tick']
            self.manager.paused = True
            tick()
            self.assertEqual(self.manager.get_task(tid)['state'], TaskState.PAUSED)
            self.manager.update_task(tid, state=TaskState.PAUSED, status='Duraklatıldı')
            self.manager.paused = False
            tick()
        self.assertEqual(self.manager.get_task(tid)['state'], TaskState.DOWNLOADING)
        self.assertEqual(self.manager.get_task(tid)['status'], 'Completed is only a label')


class QtTaskStateTests(unittest.TestCase):
    setUp = TaskStateTests.setUp
    add = TaskStateTests.add
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_table_snapshot_and_window_actions_use_state(self):
        with patch.object(self.manager, 'set_worker_count'):
            window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        self.addCleanup(finish_window_close, window)
        window.show()
        tid = self.add()
        self.manager.update_task(tid, state=TaskState.COMPLETED, status='İşlem bitti',
                                 output_path='video.mp4', progress=100)
        window.controller.poll()
        window.task_table.view.selectRow(0)
        window._refresh_actions()
        self.assertTrue(window.show_file_action.isEnabled())
        self.assertFalse(window.cancel_action.isEnabled())
        snapshot = window.task_table.model.task_for_id(tid)
        self.assertEqual(state_of(snapshot), TaskState.COMPLETED)
        self.assertEqual(task_status(snapshot), 'Completed')
        self.manager.update_task(tid, state=TaskState.FAILED, status='Completed is only display text')
        window.controller.poll()
        window._refresh_actions()
        self.assertTrue(window.retry_action.isEnabled())
        self.assertTrue(window.diagnostics_action.isEnabled())
        self.assertFalse(window.show_file_action.isEnabled())

    def test_completion_action_uses_state_instead_of_display_text(self):
        execute = Mock()
        actions = CompletionActions(self.manager, execute, countdown_seconds=0)
        self.addCleanup(actions.close)
        tid = self.add()
        actions.arm('folder')
        self.manager.update_task(tid, state=TaskState.RUNNING, status='Completed')
        actions.poll()
        execute.assert_not_called()
        self.manager.update_task(tid, state=TaskState.COMPLETED, status='Kaydedildi', output_path='video.mp4')
        actions.poll()
        execute.assert_called_once_with('folder', ['video.mp4'])

    def test_power_countdown_survives_a_display_only_change(self):
        execute = Mock()
        now = [0]
        actions = CompletionActions(self.manager, execute, countdown_seconds=30, clock=lambda: now[0])
        self.addCleanup(actions.close)
        tid = self.add()
        actions.arm('shutdown')
        self.manager.update_task(tid, state=TaskState.COMPLETED, status='Kaydedildi', output_path='video.mp4')
        actions.poll()
        self.assertIsNotNone(actions.deadline)
        self.manager.update_task(tid, state=TaskState.COMPLETED, status='İndirme tamamlandı')
        now[0] = 30
        actions.poll()
        execute.assert_called_once_with('shutdown', ['video.mp4'])

    def test_failure_or_cancellation_cannot_trigger_a_power_action(self):
        for state in (TaskState.FAILED, TaskState.CANCELLING, TaskState.CANCELLED):
            with self.subTest(state=state):
                execute = Mock()
                actions = CompletionActions(self.manager, execute, countdown_seconds=0)
                self.addCleanup(actions.close)
                tid = self.add(state.value)
                actions.arm('shutdown')
                self.manager.update_task(tid, state=state, status='Completed')
                actions.poll()
                self.assertEqual(actions.action, 'none')
                execute.assert_not_called()
