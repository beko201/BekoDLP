import threading
import time
import unittest

from settings import MAX_WORKERS
from task_manager import TaskManager, TaskOptions


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Workers did not reach the expected state')
        time.sleep(0.01)


class BlockingEngine:
    def __init__(self, manager):
        self.manager = manager
        self.lock = threading.Lock()
        self.started = []
        self.active = {}
        self.releases = {}
        self.release_all = threading.Event()
        self.peak = 0

    def process_task(self, task, worker_id):
        tid, url = task
        release = threading.Event()
        with self.lock:
            self.started.append((tid, worker_id))
            self.active[tid] = worker_id
            self.releases[tid] = release
            self.peak = max(self.peak, len(self.active))
        while not (release.wait(0.01) or self.release_all.is_set()
                   or self.manager.is_cancelled(tid)):
            pass
        with self.lock:
            del self.active[tid]
        self.manager.update_task(tid, status='Completed (MP4)', progress=100,
                                 output_path='result.mp4')


class WorkerLimitTests(unittest.TestCase):
    def make_manager(self):
        manager = TaskManager(engine_factory=BlockingEngine)

        def cleanup():
            manager.close()
            manager.engine.release_all.set()
            wait_until(manager.is_idle)
            self.assertTrue(manager.finish_shutdown())

        self.addCleanup(cleanup)
        return manager

    def enqueue(self, manager, count):
        return manager.add_urls([f'https://example.com/{i}' for i in range(count)],
                                TaskOptions(folder='.'))

    def test_repeated_16_to_1_to_16_never_multiplies_workers_or_active_tasks(self):
        manager = self.make_manager()
        engine = manager.engine
        self.enqueue(manager, 48)
        manager.set_worker_count(MAX_WORKERS)
        wait_until(lambda: len(engine.started) == MAX_WORKERS)
        original_workers = tuple(manager.workers)
        for _ in range(10):
            manager.set_worker_count(1)
            manager.set_worker_count(MAX_WORKERS)
        self.assertEqual(tuple(manager.workers), original_workers)
        self.assertEqual(len(manager.workers) + len(manager.retiring_workers), MAX_WORKERS)
        self.assertEqual(manager.worker_counter, MAX_WORKERS)
        self.assertEqual(len(engine.started), MAX_WORKERS)
        engine.release_all.set()
        wait_until(lambda: manager.task_queue.unfinished_tasks == 0)
        self.assertEqual(len(engine.started), 48)
        self.assertLessEqual(engine.peak, MAX_WORKERS)

    def test_lower_count_waits_for_active_tasks_and_does_not_strand_pending_work(self):
        manager = self.make_manager()
        engine = manager.engine
        self.enqueue(manager, 9)
        manager.set_worker_count(3)
        wait_until(lambda: len(engine.started) == 3)
        manager.set_worker_count(1)
        initial = dict(engine.active)
        for tid, worker_id in initial.items():
            if worker_id != 1:
                engine.releases[tid].set()
        wait_until(lambda: len(manager.tid_to_workerid) == 1)
        self.assertEqual(len(engine.started), 3)
        remaining = next(tid for tid, worker_id in initial.items() if worker_id == 1)
        engine.releases[remaining].set()
        wait_until(lambda: len(engine.started) == 4)
        self.assertEqual(len(engine.active), 1)
        self.assertEqual(engine.started[-1][1], 1)
        engine.release_all.set()
        wait_until(lambda: manager.task_queue.unfinished_tasks == 0)
        self.assertEqual(len(engine.started), 9)
        self.assertTrue(all(wid == 1 for tid, wid in engine.started[3:]))

    def test_concurrent_resize_calls_share_one_bounded_pool(self):
        manager = self.make_manager()
        barrier = threading.Barrier(5)
        errors = []

        def resize():
            try:
                barrier.wait(3)
                for count in (16, 1, 8, 3, 16, 2):
                    manager.set_worker_count(count)
            except BaseException as error:
                errors.append(error)

        callers = [threading.Thread(target=resize) for _ in range(4)]
        for caller in callers:
            caller.start()
        barrier.wait(3)
        for caller in callers:
            caller.join(5)
            self.assertFalse(caller.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(manager.workers), MAX_WORKERS)
        self.assertEqual(len({w.worker_id for w in manager.workers}), MAX_WORKERS)
        self.assertEqual(manager.worker_counter, MAX_WORKERS)

    def test_close_stops_parked_workers_without_starting_pending_tasks(self):
        manager = self.make_manager()
        engine = manager.engine
        self.enqueue(manager, 12)
        manager.set_worker_count(3)
        wait_until(lambda: len(engine.started) == 3)
        manager.set_worker_count(1)
        manager.close()
        engine.release_all.set()
        wait_until(manager.is_idle)
        self.assertEqual(len(engine.started), 3)
        manager.set_worker_count(16)
        self.assertEqual(len(manager.workers), 3)
        self.assertTrue(manager.finish_shutdown())
        self.assertEqual(manager.task_queue.unfinished_tasks, 0)


if __name__ == '__main__':
    unittest.main()
