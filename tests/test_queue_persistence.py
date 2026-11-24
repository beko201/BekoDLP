"""Queue round trips, crash recovery and restart behavior without network access."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from queue_store import QueueStore, UnsupportedQueueVersion, validate_document
from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from ui.queue_persistence import QueuePersistence


class QueuePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'queue.json'
        self.managers = []
        self.windows = []

    def manager(self, factory=None):
        manager = TaskManager(engine_factory=factory or (lambda manager: Mock()))
        self.managers.append(manager)
        return manager

    def tearDown(self):
        for window in self.windows:
            for box in list(window._message_boxes):
                box.done(QMessageBox.StandardButton.Ok)
            window.close()
        for manager in self.managers:
            manager.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(not m.is_idle() for m in self.managers):
            self.app.processEvents()
            time.sleep(.01)
        for window in self.windows:
            window.controller.poll()
        deadline = time.monotonic() + 5
        while any(not window._allow_close for window in self.windows) and time.monotonic() < deadline:
            self.app.processEvents()
            for window in self.windows:
                for box in list(window._message_boxes):
                    box.done(QMessageBox.StandardButton.Ok)
            time.sleep(.005)

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def close_persistence(self, persistence):
        persistence.close()
        self.wait(lambda: persistence._closed)

    def add(self, manager, url='https://example.com/video', **choices):
        options = TaskOptions(folder=str(self.folder), **choices)
        return manager.add_urls([url], options)[0]

    def snapshot(self, manager):
        return manager.queue_snapshot()[1]

    def roundtrip(self, manager):
        store = QueueStore(self.path)
        store.save(self.snapshot(manager))
        document, warnings = QueueStore(self.path).load()
        self.assertEqual(warnings, [])
        restored = self.manager()
        restored.restore_queue(document)
        return restored

    def test_roundtrip_keeps_each_tasks_own_options_and_unicode_title(self):
        manager = self.manager()
        video = self.add(manager, quality='720p', trim_start='1:20', trim_end='2:00',
                         output_mode='video', container='mkv', video_codec='hevc', audio_codec='aac')
        audio = self.add(manager, 'https://example.com/audio', output_mode='audio', container='mp3',
                         audio_codec='mp3', audio_bitrate='320k')
        manager.update_task(video, title='Türkçe başlık 🎵', status='Downloading', progress=45)
        manager.add_warning(video, 'Source warning')
        manager.begin_task_attempt(video)
        restored = self.roundtrip(manager)
        for tid in (video, audio):
            before, after = manager.get_task(tid), restored.get_task(tid)
            self.assertEqual({name: after[name] for name in TaskOptions.__dataclass_fields__},
                             {name: before[name] for name in TaskOptions.__dataclass_fields__})
        self.assertEqual(restored.get_task(video)['title'], 'Türkçe başlık 🎵')
        self.assertEqual(restored.get_task(video)['warnings'], ['Source warning'])
        self.assertEqual(restored.get_task(video)['attempt'], 1)
        self.assertEqual(restored.get_task(video)['progress'], 0)
        self.assertTrue(restored.paused)
        self.assertEqual(restored.summary().pending, 2)

    def test_completed_failed_and_cancelled_rows_do_not_restart(self):
        manager = self.manager()
        done = self.add(manager)
        failed = self.add(manager, 'https://example.com/failed')
        cancelled = self.add(manager, 'https://example.com/cancelled')
        output = self.folder / 'saved.mp4'
        output.write_bytes(b'keep this output')
        manager.update_task(done, status='Completed (MP4)', progress=100, output_path=str(output))
        manager.update_task(failed, status='Error: Offline')
        manager.cancel_task(cancelled)
        restored = self.roundtrip(manager)
        self.assertTrue(restored.task_queue.empty())
        self.assertFalse(restored.paused)
        self.assertEqual(restored.get_task(done)['output_path'], str(output))
        self.assertEqual(restored.get_task(done)['progress'], 100)
        self.assertEqual(restored.summary().retryable, 1)
        self.assertTrue(restored.cancel_events[cancelled].is_set())
        restored.retry_failed([failed])
        self.assertEqual(restored.task_queue.get_nowait(), (failed, 'https://example.com/failed'))
        self.assertEqual(output.read_bytes(), b'keep this output')

    def test_shutdown_cancellation_is_restored_as_pending_but_user_cancel_is_not(self):
        manager = self.manager()
        interrupted = self.add(manager)
        cancelled = self.add(manager, 'https://example.com/cancelled')
        late_complete = self.add(manager, 'https://example.com/completed')
        manager.update_task(interrupted, status='Processing', progress=80)
        manager.cancel_task(cancelled)
        manager.close()
        manager.update_task(interrupted, status='Cancelled')
        manager.update_task(late_complete, status='Completed', progress=100, output_path=str(self.folder / 'done.mp4'))
        restored = self.roundtrip(manager)
        self.assertEqual(restored.task_queue.qsize(), 1)
        self.assertEqual(restored.task_queue.get_nowait()[0], interrupted)
        self.assertEqual(restored.get_task(cancelled)['status'], 'Cancelled')
        self.assertTrue(restored.get_task(late_complete)['status'].startswith('Completed'))

    def test_restored_workers_do_nothing_until_resume(self):
        manager = self.manager()
        self.add(manager)
        started = threading.Event()
        class Engine:
            def __init__(self, manager):
                self.manager = manager
            def process_task(self, task, worker):
                started.set()
                self.manager.update_task(task[0], status='Completed', progress=100,
                                         output_path=str(self_outer.folder / 'done.mp4'))
        self_outer = self
        restored = self.manager(Engine)
        restored.restore_queue(self.snapshot(manager))
        restored.set_worker_count(1)
        self.assertFalse(started.wait(.25))
        restored.toggle_pause()
        self.assertTrue(started.wait(2))

    def test_cleared_queue_does_not_reappear_from_backup(self):
        manager = self.manager()
        self.add(manager)
        store = QueueStore(self.path)
        store.save(self.snapshot(manager))
        manager.clear_list(remove_pending=True)
        store.save(self.snapshot(manager))
        self.assertTrue(store.backup.exists())
        document, _ = QueueStore(self.path).load()
        self.assertEqual(document['tasks'], [])

    def test_corrupt_primary_recovers_backup_and_preserves_original(self):
        manager = self.manager()
        tid = self.add(manager)
        store = QueueStore(self.path)
        store.save(self.snapshot(manager))
        manager.update_task(tid, title='New title')
        store.save(self.snapshot(manager))
        self.path.write_bytes(b'{broken queue')
        recovered = QueueStore(self.path)
        document, warnings = recovered.load()
        self.assertTrue(warnings)
        preserved = list(self.folder.glob('queue.json.corrupt-*'))
        self.assertEqual(preserved[0].read_bytes(), b'{broken queue')
        self.assertEqual(document['tasks'][0]['title'], '(resolving title...)')
        recovered.save(document)
        self.assertEqual(QueueStore(self.path).load()[0], document)

    def test_missing_primary_recovers_backup(self):
        manager = self.manager()
        self.add(manager)
        store = QueueStore(self.path)
        store.save(self.snapshot(manager))
        self.path.replace(store.backup)
        document, warnings = QueueStore(self.path).load()
        self.assertEqual(len(document['tasks']), 1)
        self.assertTrue(warnings)

    def test_unknown_schema_is_never_overwritten(self):
        original = b'{"schema_version": 999, "tasks": []}'
        self.path.write_bytes(original)
        store = QueueStore(self.path)
        with self.assertRaises(UnsupportedQueueVersion):
            store.load()
        with self.assertRaises(PermissionError):
            store.save(self.snapshot(self.manager()))
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_atomic_replace_keeps_the_previous_primary(self):
        manager = self.manager()
        tid = self.add(manager)
        store = QueueStore(self.path)
        store.save(self.snapshot(manager))
        original = self.path.read_bytes()
        manager.update_task(tid, title='Changed')
        real_replace = os.replace
        def replace(source, target):
            if Path(target) == self.path:
                raise PermissionError('Primary locked')
            return real_replace(source, target)
        with patch('queue_store.os.replace', side_effect=replace):
            with self.assertRaises(PermissionError):
                store.save(self.snapshot(manager))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_invalid_rows_are_rejected_before_any_task_is_restored(self):
        manager = self.manager()
        self.add(manager)
        valid = self.snapshot(manager)
        for change in ('duplicate', 'bad_options', 'bad_url', 'bad_state'):
            with self.subTest(change=change):
                document = deepcopy(valid)
                if change == 'duplicate':
                    document['tasks'].append(deepcopy(document['tasks'][0]))
                elif change == 'bad_options':
                    document['tasks'][0]['options']['trim_end'] = 'invalid'
                elif change == 'bad_url':
                    document['tasks'][0]['url'] = 'file:///private'
                else:
                    document['tasks'][0]['state'] = 'unknown'
                target = self.manager()
                with self.assertRaises(ValueError):
                    target.restore_queue(document)
                self.assertEqual(target.summary().total, 0)

    def test_progress_ticks_do_not_rewrite_the_queue(self):
        manager = self.manager()
        tid = self.add(manager)
        persistence = QueuePersistence(manager, self.path)
        persistence.load()
        self.addCleanup(self.close_persistence, persistence)
        persistence.flush()
        self.wait(lambda: not persistence.busy)
        manager.update_task(tid, status='Downloading', progress=1)
        with patch.object(persistence.store, 'save', wraps=persistence.store.save) as save:
            for step in range(2, 80):
                manager.update_task(tid, status=f'Downloading - {step} MB', progress=step)
                persistence.flush()
            save.assert_not_called()
            manager.update_task(tid, title='Resolved title')
            persistence.flush()
            self.wait(lambda: not persistence.busy)
            save.assert_called_once()

    def test_snapshot_excludes_media_urls_and_runtime_handles(self):
        manager = self.manager()
        tid = self.add(manager)
        manager.tasks[tid].update(thumbnail_url='https://cdn/expiring', process=object(),
                                  formats=[{'url': 'https://cdn/signed?token=temporary'}])
        document = json.dumps(self.snapshot(manager))
        self.assertNotIn('temporary', document)
        self.assertNotIn('expiring', document)
        self.assertNotIn('process', document)

    def test_snapshot_and_disk_write_run_off_gui_thread_and_keep_timers_running(self):
        manager = self.manager()
        self.add(manager)
        persistence = QueuePersistence(manager, self.path)
        persistence.load()
        persistence.timer.stop()
        self.addCleanup(self.close_persistence, persistence)
        snapshot_started, snapshot_release = threading.Event(), threading.Event()
        save_started, save_release = threading.Event(), threading.Event()
        self.addCleanup(snapshot_release.set)
        self.addCleanup(save_release.set)
        gui_thread = threading.get_ident()
        threads, completed, ticks = [], [], []
        snapshot, save = manager.queue_snapshot, persistence.store.save
        def slow_snapshot():
            threads.append(threading.get_ident())
            snapshot_started.set()
            snapshot_release.wait(3)
            return snapshot()
        def slow_save(document):
            threads.append(threading.get_ident())
            save_started.set()
            save_release.wait(3)
            save(document)
        heartbeat = QTimer()
        heartbeat.setInterval(1)
        heartbeat.timeout.connect(lambda: ticks.append(True))
        heartbeat.start()
        try:
            with patch.object(manager, 'queue_snapshot', side_effect=slow_snapshot), \
                    patch.object(persistence.store, 'save', side_effect=slow_save):
                self.assertIsNone(persistence.flush(on_finished=lambda saved: completed.append((saved, threading.get_ident()))))
                self.wait(snapshot_started.is_set)
                self.wait(lambda: len(ticks) >= 3)
                snapshot_release.set()
                self.wait(save_started.is_set)
                count = len(ticks)
                self.wait(lambda: len(ticks) >= count + 3)
                self.assertEqual(completed, [])
                save_release.set()
                self.wait(lambda: bool(completed))
        finally:
            snapshot_release.set()
            save_release.set()
            heartbeat.stop()
        self.assertTrue(all(thread != gui_thread for thread in threads))
        self.assertEqual(completed, [(True, gui_thread)])

    def test_changes_during_save_coalesce_and_close_waits_for_latest_queue(self):
        manager = self.manager()
        tid = self.add(manager)
        persistence = QueuePersistence(manager, self.path)
        persistence.load()
        persistence.timer.stop()
        self.addCleanup(self.close_persistence, persistence)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        documents = []
        original = persistence.store.save
        def save(document):
            documents.append(document)
            if len(documents) == 1:
                started.set()
                release.wait(3)
            original(document)
        with patch.object(persistence.store, 'save', side_effect=save):
            persistence.flush()
            self.wait(started.is_set)
            for number in range(30):
                manager.update_task(tid, title=f'Title {number}')
                persistence.flush()
            manager.clear_list(remove_pending=True)
            last = self.add(manager, 'https://example.com/latest')
            self.assertIsNone(persistence.close())
            self.assertFalse(persistence._closed)
            self.assertEqual(len(documents), 1)
            release.set()
            self.wait(lambda: persistence._closed)
        self.assertTrue(persistence._close_result)
        self.assertEqual(len(documents), 2)
        saved, warnings = QueueStore(self.path).load()
        self.assertEqual(warnings, [])
        self.assertEqual([row['id'] for row in saved['tasks']], [last])
        self.assertTrue(persistence.store.backup.exists())

    def test_background_save_error_preserves_file_and_later_flush_retries(self):
        manager = self.manager()
        tid = self.add(manager)
        QueueStore(self.path).save(self.snapshot(manager))
        original = self.path.read_bytes()
        persistence = QueuePersistence(manager, self.path)
        persistence.load()
        persistence.timer.stop()
        self.addCleanup(self.close_persistence, persistence)
        manager.update_task(tid, title='Changed')
        results, messages = [], []
        persistence.message.connect(lambda *message: messages.append(message))
        with patch.object(persistence.store, 'save', side_effect=PermissionError('Disk locked')):
            persistence.flush(on_finished=results.append)
            self.wait(lambda: bool(results))
        self.assertEqual(results, [False])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIn('could not be saved', messages[0][2])
        persistence.flush(on_finished=results.append)
        self.wait(lambda: len(results) == 2)
        self.assertEqual(results, [False, True])
        self.assertEqual(QueueStore(self.path).load()[0]['tasks'][0]['title'], 'Changed')

    def test_power_action_waits_for_save_and_new_work_cancels_it(self):
        for add_work in (False, True):
            with self.subTest(add_work=add_work):
                manager = self.manager()
                with patch.object(manager, 'set_worker_count'):
                    window = MainWindow(manager, settings_path=self.folder / f'power-{add_work}.json')
                self.windows.append(window)
                tid = self.add(manager)
                manager.update_task(tid, status='Completed', progress=100, output_path='done.mp4')
                started, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                original = window.queue_persistence.store.save
                def slow_save(document):
                    started.set()
                    release.wait(3)
                    original(document)
                with patch.object(window.queue_persistence.store, 'save', side_effect=slow_save), \
                        patch('power_actions.run_power_action') as power:
                    window._perform_completion_action('sleep', [])
                    self.wait(started.is_set)
                    power.assert_not_called()
                    if add_work:
                        self.add(manager, 'https://example.com/new-power-task')
                    release.set()
                    self.wait(lambda: not window.queue_persistence.busy)
                    if add_work:
                        power.assert_not_called()
                    else:
                        self.wait(lambda: power.called)
                        power.assert_called_once_with('sleep')

    def test_window_close_and_reopen_restore_table_and_resume_button(self):
        path = self.folder / 'settings.json'
        def window():
            manager = self.manager()
            with patch.object(manager, 'set_worker_count'):
                result = MainWindow(manager, settings_path=path)
            self.windows.append(result)
            return result, manager
        first, manager = window()
        tid = self.add(manager, trim_start='2', trim_end='5')
        manager.update_task(tid, title='Saved task', status='Downloading', progress=30)
        first.close()
        first.controller.poll()
        self.wait(lambda: first._allow_close)
        self.assertTrue(first._allow_close)
        second, restored = window()
        self.assertTrue(restored.paused)
        self.assertEqual(second.pause_button.text(), 'Resume')
        self.assertEqual(second.task_table.model.rowCount(), 1)
        self.assertEqual(restored.get_task(tid)['trim_start'], '2')
        self.assertEqual(restored.get_task(tid)['status'], 'Queued')
        self.assertEqual(restored.add_urls(['https://example.com/new'], TaskOptions(folder=str(self.folder))), ['T2'])

    def test_autosave_survives_a_restart_without_a_normal_close(self):
        manager = self.manager()
        persistence = QueuePersistence(manager, self.path)
        persistence.load()
        self.addCleanup(self.close_persistence, persistence)
        tid = self.add(manager)
        manager.update_task(tid, title='In progress', status='Downloading', progress=40)
        QTest.qWait(900)
        self.wait(lambda: not persistence.busy and self.path.exists())
        document, warnings = QueueStore(self.path).load()
        self.assertFalse(warnings)
        restored = self.manager()
        restored.restore_queue(document)
        self.assertEqual(restored.get_task(tid)['title'], 'In progress')
        self.assertEqual(restored.get_task(tid)['status'], 'Queued')
        self.assertTrue(restored.paused)

    def test_close_time_write_failure_is_visible_before_the_window_closes(self):
        manager = self.manager()
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=self.folder / 'settings.json')
        self.windows.append(window)
        window.show()
        self.add(manager)
        with patch.object(window.queue_persistence.store, 'save', side_effect=PermissionError('Disk locked')):
            window.close()
            window.controller.poll()
            self.wait(lambda: window._queue_closed)
            self.assertFalse(window._allow_close)
            warning = next(box for box in window._message_boxes if box.property('_message_key') == 'Queue warning')
            self.assertIn('could not be saved', warning.text())
            warning.done(QMessageBox.StandardButton.Ok)
            self.wait(lambda: window._allow_close)
            self.assertTrue(window._allow_close)
