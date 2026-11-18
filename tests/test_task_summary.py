"""Incremental queue totals across state transitions and concurrent progress."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import math
import random
import unittest
from unittest.mock import Mock, patch

from task_manager import TaskManager, TaskOptions


class TaskSummaryTests(unittest.TestCase):
    def setUp(self):
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(self.manager.close)

    def add(self, count):
        start = self.manager.task_counter
        return self.manager.add_urls([f"https://example.com/{start + i}" for i in range(count)],
                                     TaskOptions(folder="."))

    def assert_summary_matches_tasks(self):
        tasks = self.manager.list_tasks()
        active = self.manager.tid_to_workerid
        summary = self.manager.summary()
        self.assertEqual(summary.total, len(tasks))
        self.assertEqual(summary.pending, sum(t["status"].startswith(("Queu", "Wait")) for t in tasks))
        self.assertEqual(summary.failed, sum(t["status"].startswith("Error") for t in tasks))
        self.assertEqual(summary.retryable,
                         sum(t["status"].startswith("Error") and t["id"] not in active for t in tasks))
        self.assertEqual(summary.clearable,
                         sum(t["status"].startswith(("Completed", "Error", "Queu", "Wait"))
                             or t["status"] == "Cancelled" for t in tasks))
        expected = round(math.fsum(t.get("progress", 0) for t in tasks) / len(tasks)) if tasks else 0
        self.assertEqual(summary.progress, expected)

    def test_lifecycle_cancellation_retry_and_clear_keep_totals_in_sync(self):
        failed, complete, cancelled, waiting, pending = self.add(5)
        self.assert_summary_matches_tasks()
        self.manager.update_task(complete, status="Completed", output_path="output.mp4", progress=100)
        self.manager.cancel_task(cancelled)
        self.manager.update_task(waiting, status="Waiting for video conversion slot", progress=70)
        self.manager.engine.process_task.side_effect = lambda task, wid: self.manager.update_task(
            task[0], status="Error: offline", progress=45)
        self.manager._process_task((failed, self.manager.get_task(failed)["url"]), 1)
        self.assertEqual(self.manager.summary().retryable, 1)
        self.assert_summary_matches_tasks()
        self.manager.retry_failed([failed])
        self.assertEqual(self.manager.summary().failed, 0)
        self.assertEqual(self.manager.get_task(failed)["progress"], 0)
        self.assert_summary_matches_tasks()
        removed = self.manager.clear_list()
        self.assertEqual(set(removed), {complete, cancelled})
        self.assert_summary_matches_tasks()
        self.manager.clear_list(remove_pending=True)
        self.assertEqual(self.manager.summary().total, 1)  # The conversion waiter is not a queued download.
        self.manager.cancel_task(waiting)
        self.manager.clear_list(remove_pending=True)
        self.assertEqual(self.manager.summary().total, 0)
        self.assertEqual(self.manager.summary().progress, 0)
        self.assert_summary_matches_tasks()
        self.add(1)
        self.assert_summary_matches_tasks()

    def test_random_updates_and_removals_match_a_full_scan(self):
        rng = random.Random(517)
        tids = self.add(80)
        statuses = ("Queued", "Downloading", "Paused", "Waiting for conversion", "Error: offline",
                    "Completed (MP4)", "Cancelled")
        for index in range(1500):
            tid = rng.choice(tids)
            status = rng.choice(statuses)
            self.manager.update_task(tid, status=status, progress=rng.random() * 100,
                                     output_path="output.mp4" if status.startswith("Completed") else None)
            if index % 100 == 0:
                self.assert_summary_matches_tasks()
        self.manager.clear_list(remove_pending=True)
        self.assert_summary_matches_tasks()

    def test_parallel_progress_and_duplicate_updates_never_double_count(self):
        tids = self.add(8)
        def update(tid):
            for step in range(300):
                self.manager.update_task(tid, status="Downloading", progress=step / 3)
                self.manager.update_task(tid, progress=step / 3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(update, tids))
        self.assert_summary_matches_tasks()
        self.assertEqual(self.manager.summary().progress, 99)
        self.assertEqual(self.manager.summary().pending, 0)

    def test_progress_and_title_changes_do_not_invalidate_actions(self):
        tid = self.add(1)[0]
        self.manager.update_task(tid, status="Downloading", progress=.1)
        revision = self.manager.summary().action_revision
        for index in range(50):
            self.manager.update_task(tid, status=f"Downloading - {index} MB", progress=index / 2,
                                     title=f"Title {index}")
        self.assertEqual(self.manager.summary().action_revision, revision)
        self.manager.update_task(tid, status="Completed", progress=100, output_path="output.mp4")
        self.assertGreater(self.manager.summary().action_revision, revision)

    def test_summary_is_immutable_and_does_not_iterate_or_copy_tasks(self):
        self.add(100)
        snapshot = self.manager.summary()
        with self.assertRaises(FrozenInstanceError):
            snapshot.total = 0
        class NoIteration(dict):
            def values(self):
                raise AssertionError("summary must not scan tasks")
            def items(self):
                raise AssertionError("summary must not scan tasks")
            def __iter__(self):
                raise AssertionError("summary must not scan tasks")
        original = self.manager.tasks
        self.manager.tasks = NoIteration(original)
        try:
            with patch.object(self.manager, "_snapshot", side_effect=AssertionError("must not copy")):
                self.assertEqual(self.manager.summary(), snapshot)
        finally:
            self.manager.tasks = original


if __name__ == "__main__":
    unittest.main()
