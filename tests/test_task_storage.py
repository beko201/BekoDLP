"""Shared task settings stay isolated; lazy cancellation wakes real waiters."""
import threading
import unittest
from unittest.mock import Mock

from task_manager import TaskManager, TaskOptions
from task_storage import CancellationEvent


class TaskStorageTests(unittest.TestCase):
    def test_task_changes_never_change_siblings_or_public_snapshots(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        first, second = manager.add_urls(['https://example.com/1', 'https://example.com/2'],
                                         TaskOptions(folder='.', quality='720p'))
        manager.tasks[first]['quality'] = '480p'
        manager.tasks[first].update(playlist_index='3')
        manager.add_warning(first, 'first only')
        self.assertEqual(manager.get_task(second)['quality'], '720p')
        self.assertEqual(manager.get_task(second)['playlist_index'], '')
        self.assertNotIn('warnings', manager.get_task(second))
        snapshot = manager.get_task(first)
        self.assertIsInstance(snapshot, dict)
        snapshot['quality'] = '2160p'
        snapshot['warnings'].clear()
        self.assertEqual(manager.get_task(first)['quality'], '480p')
        self.assertEqual(manager.get_task(first)['warnings'], ['first only'])
        del manager.tasks[first]['quality']
        self.assertNotIn('quality', manager.get_task(first))
        self.assertEqual(manager.get_task(second)['quality'], '720p')

    def test_restored_tasks_keep_independent_options_and_cancellation(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        first, second = manager.add_urls(['https://example.com/1', 'https://example.com/2'], TaskOptions(folder='.'))
        restored = TaskManager(engine_factory=lambda manager: Mock())
        restored.restore_queue(manager.queue_snapshot()[1])
        restored.tasks[first]['quality'] = '480p'
        restored.cancel_task(first)
        self.assertEqual(restored.get_task(second)['quality'], '1080p')
        self.assertFalse(restored.is_cancelled(second))
        self.assertEqual(restored.get_task(first)['status'], 'Cancelled')

    def test_cancellation_before_and_after_wait_and_clear(self):
        event = CancellationEvent()
        self.assertFalse(event.is_set())
        event.set()
        self.assertTrue(event.wait(0))
        event.clear()
        self.assertFalse(event.wait(.001))
        results = []
        workers = [threading.Thread(target=lambda: results.append(event.wait(2))) for _ in range(4)]
        for worker in workers:
            worker.start()
        event.set()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True] * 4)
        event.clear()
        self.assertFalse(event.is_set())
        self.assertFalse(event.wait(0))

    def test_first_wait_racing_with_cancel_never_misses_the_signal(self):
        for _ in range(50):
            event = CancellationEvent()
            barrier = threading.Barrier(2)
            result = []
            def wait():
                barrier.wait()
                result.append(event.wait(1))
            worker = threading.Thread(target=wait)
            worker.start()
            barrier.wait()
            event.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [True])
