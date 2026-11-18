import threading
import unittest
from unittest.mock import Mock

from task_manager import TaskManager, TaskOptions


class CancelRaceTests(unittest.TestCase):
    def make_manager(self, active=True):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        tid = manager.add_urls(['https://example.com/video'], TaskOptions(folder='.'))[0]
        if active:
            manager.tid_to_workerid[tid] = 1
            manager.update_task(tid, status='Downloading', progress=45)
        return manager, tid

    def test_worker_terminal_update_cannot_be_overwritten_by_cancel(self):
        for terminal in ('Completed (MP4)', 'Error: network', 'Cancelled'):
            with self.subTest(terminal=terminal):
                manager, tid = self.make_manager()
                completed = threading.Event()
                original_update = manager._task_update
                original_set = manager.cancel_events[tid].set

                def finish():
                    try:
                        manager.update_task(tid, status=terminal, progress=100,
                                            output_path='result.mp4')
                    finally:
                        completed.set()

                worker = threading.Thread(target=finish)

                def signal_cancel():
                    original_set()
                    worker.start()

                def delayed_cancel_update(task_id, **changes):
                    if changes.get('status') == 'Cancelling...':
                        # Reproduce the original gap between releasing the lock
                        # and posting the cancellation status.
                        self.assertTrue(completed.wait(3))
                    return original_update(task_id, **changes)

                manager.cancel_events[tid].set = signal_cancel
                manager._task_update = delayed_cancel_update
                self.assertTrue(manager.cancel_task(tid))
                worker.join(3)
                self.assertFalse(worker.is_alive())
                task = manager.get_task(tid)
                self.assertEqual(task['status'], terminal)
                if terminal.startswith('Completed'):
                    self.assertEqual(task['progress'], 100)
                    self.assertEqual(task['output_path'], 'result.mp4')
                updates = []
                while not manager.events.empty():
                    event = manager.events.get_nowait()
                    if event.kind == 'task_updated' and event.tid == tid:
                        updates.append(event.data['task'])
                self.assertEqual(updates[-1]['status'], terminal)

    def test_terminal_task_rejects_cancel_without_setting_event(self):
        for terminal in ('Completed (MP4)', 'Error: network', 'Cancelled'):
            with self.subTest(terminal=terminal):
                manager, tid = self.make_manager()
                manager.update_task(tid, status=terminal)
                before = manager.get_task(tid)
                self.assertFalse(manager.cancel_task(tid))
                self.assertEqual(manager.get_task(tid), before)
                self.assertFalse(manager.cancel_events[tid].is_set())

    def test_active_and_queued_cancellation_keep_progress_and_release_queued_url(self):
        for active in (False, True):
            with self.subTest(active=active):
                manager, tid = self.make_manager(active)
                self.assertTrue(manager.cancel_task(tid))
                task = manager.get_task(tid)
                self.assertTrue(manager.is_cancelled(tid))
                self.assertEqual(task['status'], 'Cancelling...' if active else 'Cancelled')
                self.assertEqual(task['progress'], 45 if active else 0)
                self.assertEqual(task['url'] in manager.active_urls, active)
                self.assertFalse(manager.cancel_task(tid))


if __name__ == '__main__':
    unittest.main()
