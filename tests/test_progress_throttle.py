"""Producer-side throttling keeps counters current and control events immediate."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import task_manager
from task_manager import DOWNLOAD_SPEED_SAMPLES, TaskManager, TaskOptions


class ProgressThrottleTests(unittest.TestCase):
    def setUp(self):
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.tid = self.manager.add_urls(["https://example.com/video"], TaskOptions(folder="."))[0]
        self.addCleanup(self.manager.close)
        self.now = 0.0

    def clock(self):
        return self.now

    def test_burst_keeps_every_byte_but_limits_calculation_and_publication(self):
        with patch('task_progress.time', SimpleNamespace(monotonic=self.clock)), \
                patch.object(self.manager, "report_stage_progress", wraps=self.manager.report_stage_progress) as report:
            for index in range(10000):
                self.now = index / 10000
                self.manager.report_progress(self.tid, "video", index + 1, 20000)
            self.assertLessEqual(report.call_count, 11)
            task = self.manager.get_task(self.tid)
            self.assertEqual(task["downloaded_bytes"], 10000)
            self.assertEqual(task["total_bytes"], 20000)
            self.assertEqual(self.manager.stream_progress[self.tid]["video"], (10000, 20000))
            self.assertIsInstance(self.manager.download_speed_history[self.tid], deque)
            self.assertLessEqual(len(self.manager.download_speed_history[self.tid]), 11)
            self.assertGreater(task["speed_bps"], 0)
            self.assertGreater(task["eta_seconds"], 0)

    def test_final_and_forced_reports_do_not_wait_for_the_interval(self):
        with patch('task_progress.time', SimpleNamespace(monotonic=self.clock)), \
                patch.object(self.manager, "report_stage_progress", wraps=self.manager.report_stage_progress) as report:
            self.manager.report_progress(self.tid, "video", 1, 100)
            self.now = .001
            self.manager.report_progress(self.tid, "video", 100, 100)
            self.assertEqual(report.call_count, 2)
            self.assertEqual(self.manager.get_task(self.tid)["progress"], 70)
            self.manager.report_progress(self.tid, "video", 100, 100, force=True)
            self.assertEqual(report.call_count, 3)
            self.assertLess(self.manager.get_task(self.tid)["progress"], 100)

    def test_stream_restart_resets_speed_even_when_peer_keeps_aggregate_bytes_higher(self):
        self.manager.register_download_streams(self.tid, {"video": 10000, "audio": 10000})
        with patch('task_progress.time', SimpleNamespace(monotonic=self.clock)):
            self.manager.report_progress(self.tid, "video", 1000, 10000)
            self.now = .2
            self.manager.report_progress(self.tid, "audio", 5000, 10000)
            self.now = .21
            self.manager.report_progress(self.tid, "audio", 8000, 10000)
            self.now = .22
            self.manager.report_progress(self.tid, "video", 500, 10000)
        task = self.manager.get_task(self.tid)
        self.assertEqual(task["downloaded_bytes"], 8500)
        self.assertEqual(task["speed_bps"], 0)
        self.assertIsNone(task["eta_seconds"])
        self.assertEqual(list(self.manager.download_speed_history[self.tid]), [(.22, 8500)])

    def test_speed_history_is_bounded_and_old_samples_expire(self):
        with patch('task_progress.time', SimpleNamespace(monotonic=self.clock)):
            for index in range(200):
                self.now = index / 1000
                self.manager.report_progress(self.tid, "video", index, 10000, force=True)
            self.assertEqual(len(self.manager.download_speed_history[self.tid]), DOWNLOAD_SPEED_SAMPLES)
            self.now = 10
            self.manager.report_progress(self.tid, "video", 201, 10000)
        self.assertEqual(len(self.manager.download_speed_history[self.tid]), 1)
        self.assertEqual(self.manager.get_task(self.tid)["speed_bps"], 0)

    def test_pause_and_processing_clear_metrics_and_resume_is_immediate(self):
        with patch('task_progress.time', SimpleNamespace(monotonic=self.clock)):
            self.manager.report_progress(self.tid, "video", 1, 100)
            self.manager.update_task(self.tid, status="Paused")
            self.assertIsNone(self.manager.get_task(self.tid)["speed_bps"])
            self.now = .01
            self.manager.report_progress(self.tid, "video", 2, 100)
            self.assertTrue(self.manager.get_task(self.tid)["status"].startswith("Downloading"))
            self.manager.report_stage_progress(self.tid, "processing", .5)
            self.assertIsNone(self.manager.get_task(self.tid)["speed_bps"])
            self.assertIsNone(self.manager.get_task(self.tid)["eta_seconds"])
            before = self.manager.get_task(self.tid)
            self.manager.report_progress(self.tid, "video", 100, 100, force=True)
            self.assertEqual(self.manager.get_task(self.tid), before)

    def test_retry_resets_throttle_and_byte_counters(self):
        with patch('task_progress.time', SimpleNamespace(monotonic=self.clock)):
            self.manager.report_progress(self.tid, "video", 50, 100)
            self.manager.engine.process_task.side_effect = lambda task, wid: self.manager.update_task(
                self.tid, status="Error: offline")
            self.manager._process_task((self.tid, self.manager.get_task(self.tid)["url"]), 1)
            self.assertNotIn(self.tid, self.manager._download_progress_locks)
            self.manager.retry_failed([self.tid])
            self.assertEqual(self.manager.get_task(self.tid)["downloaded_bytes"], 0)
            self.now = .001
            self.manager.report_progress(self.tid, "video", 1, 100)
            task = self.manager.get_task(self.tid)
            self.assertAlmostEqual(task["progress"], .7)
            self.assertEqual(task["speed_bps"], 0)
            self.assertEqual(task["downloaded_bytes"], 1)

    def test_other_tasks_can_publish_while_one_task_publication_is_blocked(self):
        other = self.manager.add_urls(["https://example.com/other"], TaskOptions(folder="."))[0]
        entered, release = threading.Event(), threading.Event()
        original = self.manager.report_stage_progress
        def delayed(tid, *args, **kwargs):
            if tid == self.tid:
                entered.set()
                release.wait(3)
            return original(tid, *args, **kwargs)
        with patch.object(self.manager, "report_stage_progress", side_effect=delayed), \
                ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(self.manager.report_progress, self.tid, "video", 1, 100)
                self.assertTrue(entered.wait(2))
                second = pool.submit(self.manager.report_progress, other, "video", 2, 100)
                second.result(timeout=1)
                self.assertEqual(self.manager.get_task(other)["downloaded_bytes"], 2)
                release.set()
                first.result(timeout=2)
            finally:
                release.set()

    def test_terminal_and_removed_tasks_cannot_recreate_progress_buffers(self):
        self.manager.update_task(self.tid, status="Completed", progress=100, output_path="output.mp4")
        before = self.manager.get_task(self.tid)
        self.manager.report_progress(self.tid, "video", 50, 100, force=True)
        self.assertEqual(self.manager.get_task(self.tid), before)
        self.assertFalse(self.manager.stream_progress)
        self.assertFalse(self.manager.download_speed_history)
        self.assertFalse(self.manager._download_progress_locks)
        self.manager.clear_list()
        self.manager.report_progress(self.tid, "video", 100, 100, force=True)
        self.assertFalse(self.manager._download_progress_locks)


if __name__ == "__main__":
    unittest.main()
