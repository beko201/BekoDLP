"""Encoding admission, live resizing, cancellation and codec thread budgets."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from processing_limits import ProcessingLimits, budget_command, video_encoder
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions
from errors import DownloadCancelled


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for encoding admission")
        time.sleep(.005)


class ProcessingLimitTests(unittest.TestCase):
    def test_fifo_resize_and_reduction_preserve_active_conversions(self):
        limits = ProcessingLimits()
        release = [threading.Event() for _ in range(4)]
        entered = []
        errors = []
        def run(index):
            try:
                with limits.video_slot(lambda: None, lambda: False, lambda: None):
                    entered.append(index)
                    release[index].wait(5)
            except BaseException as error:
                errors.append(error)
        with ThreadPoolExecutor(max_workers=4) as pool:
            try:
                pool.submit(run, 0)
                wait_until(lambda: entered == [0])
                pool.submit(run, 1)
                wait_until(lambda: len(limits.waiters) == 1)
                pool.submit(run, 2)
                wait_until(lambda: len(limits.waiters) == 2)
                limits.configure(2, 2)
                wait_until(lambda: entered == [0, 1])
                limits.configure(1, 2)
                release[0].set()
                wait_until(lambda: limits.active == 1)
                self.assertEqual(entered, [0, 1])
                release[1].set()
                wait_until(lambda: entered == [0, 1, 2])
            finally:
                for event in release:
                    event.set()
        self.assertEqual(errors, [])
        self.assertEqual(limits.active, 0)
        self.assertFalse(limits.waiters)

    def test_cancelled_and_paused_waiters_do_not_take_capacity(self):
        limits = ProcessingLimits()
        paused = threading.Event()
        paused.set()
        cancel = threading.Event()
        waiting = threading.Event()
        def check():
            if cancel.is_set():
                raise DownloadCancelled()
        def run():
            with limits.video_slot(check, paused.is_set, waiting.set):
                self.fail("Paused conversion must not start")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            self.assertTrue(waiting.wait(2))
            self.assertEqual(limits.active, 0)
            cancel.set()
            with self.assertRaises(DownloadCancelled):
                future.result(timeout=2)
        self.assertFalse(limits.waiters)
        self.assertEqual(limits.active, 0)
        paused.clear()
        with self.assertRaisesRegex(RuntimeError, "failed"), \
                limits.video_slot(lambda: None, paused.is_set, lambda: None):
            raise RuntimeError("failed")
        self.assertEqual(limits.active, 0)

    def test_auto_budget_and_invalid_live_or_saved_limits(self):
        limits = ProcessingLimits()
        with patch("processing_limits.os.process_cpu_count", return_value=8, create=True):
            self.assertEqual(limits.thread_budget(), 7)
            limits.configure(2, 0)
            self.assertEqual(limits.thread_budget(), 3)
            limits.configure(2, 6)
            self.assertEqual(limits.thread_budget(), 6)
        for video, threads in ((0, 0), (5, 0), (True, 0), (1, -1), (1, 65), (1, False)):
            with self.subTest(video=video, threads=threads), self.assertRaises(ValueError):
                limits.configure(video, threads)
        data, issues = validate_settings({"video_workers": 0, "ffmpeg_threads": -1})
        self.assertEqual(data["video_workers"], DEFAULTS["video_workers"])
        self.assertEqual(data["ffmpeg_threads"], DEFAULTS["ffmpeg_threads"])
        self.assertIn("video_workers", issues)
        self.assertIn("ffmpeg_threads", issues)

    def test_copy_and_audio_bypass_video_gate_and_encoder_pools_are_bounded(self):
        for args in (("-c:v", "copy", "-c:a", "aac"), ("-vn", "-c", "libmp3lame"),
                     ("-c:a", "libopus")):
            self.assertIsNone(video_encoder(["ffmpeg", *args, "output.mkv"]))
        for codec, option, expected in (("libx265", "-x265-params", "pools=3:frame-threads=1"),
                                        ("libsvtav1", "-svtav1-params", "lp=3")):
            command = ["ffmpeg", "-ss", "1", "-i", "video.mkv", "-i", "audio.mka",
                       "-c:v", codec, "output.mkv"]
            bounded = budget_command(command, 3)
            self.assertEqual(video_encoder(bounded), codec)
            self.assertEqual(bounded[-1], "output.mkv")
            for index, arg in enumerate(bounded):
                if arg == "-i":
                    self.assertEqual(bounded[index - 2:index], ["-threads", "3"])
            self.assertEqual(bounded[bounded.index(option) + 1], expected)
        command = ["ffmpeg", "-i", "input.mp4", "-c:v", "libx265",
                   "-x265-params", "crf=20:pools=99", "output.mp4"]
        bounded = budget_command(command, 2)
        self.assertEqual(bounded[bounded.index("-x265-params") + 1], "crf=20:pools=2:frame-threads=1")

    def test_engine_gate_leaves_audio_free_and_cancelled_waiter_never_launches(self):
        manager = TaskManager()
        self.addCleanup(manager.close)
        tids = manager.add_urls([f"https://example.com/{i}" for i in range(3)], TaskOptions(folder="."))
        engine = manager.engine
        release = threading.Event()
        started = []
        def process(tid, worker, cmd, *args, **kwargs):
            started.append((tid, cmd))
            if tid == tids[0]:
                release.wait(5)
            return 0
        video = [engine.ffmpeg, "-i", "input.mp4", "-c:v", "libx264", "output.mp4"]
        audio = [engine.ffmpeg, "-i", "input.wav", "-vn", "-c:a", "libmp3lame", "output.mp3"]
        with patch.object(engine, "_run_ffmpeg_process", side_effect=process), ThreadPoolExecutor(max_workers=3) as pool:
            try:
                first = pool.submit(engine._run_ffmpeg, tids[0], 1, video)
                wait_until(lambda: len(started) == 1)
                second = pool.submit(engine._run_ffmpeg, tids[1], 2, video)
                wait_until(lambda: len(engine.processing_limits.waiters) == 1)
                self.assertEqual(engine._run_ffmpeg(tids[2], 3, audio), 0)
                manager.cancel_events[tids[1]].set()
                with self.assertRaises(DownloadCancelled):
                    second.result(timeout=2)
                self.assertEqual([tid for tid, _ in started], [tids[0], tids[2]])
                release.set()
                self.assertEqual(first.result(timeout=2), 0)
            finally:
                release.set()
        self.assertEqual(engine.processing_limits.active, 0)

    def test_shutdown_cancels_waiting_conversion_and_launch_failure_releases_slot(self):
        manager = TaskManager()
        engine = manager.engine
        tid = manager.add_urls(["https://example.com/close"], TaskOptions(folder="."))[0]
        command = [engine.ffmpeg, "-i", "input.mp4", "-c:v", "libx264", "output.mp4"]
        with patch.object(engine, "_run_ffmpeg_process", side_effect=OSError("cannot start")):
            with self.assertRaises(OSError):
                engine._run_ffmpeg(tid, 1, command)
        self.assertEqual(engine.processing_limits.active, 0)
        manager.paused = True
        with patch.object(engine, "_run_ffmpeg_process") as launch, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine._run_ffmpeg, tid, 1, command)
            wait_until(lambda: bool(engine.processing_limits.waiters))
            manager.close()
            with self.assertRaises(DownloadCancelled):
                future.result(timeout=2)
            launch.assert_not_called()
        self.assertEqual(engine.processing_limits.active, 0)


if __name__ == "__main__":
    unittest.main()
