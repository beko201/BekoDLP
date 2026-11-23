"""Download capacity, cancellation and real native HLS fragment concurrency."""
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import download_engine
from network_limits import NetworkLimits
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions
from utils import DownloadCancelled, NO_WINDOW


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Download capacity wait timed out")
        time.sleep(.005)


class NetworkLimitTests(unittest.TestCase):
    def test_budget_adapts_fragment_parallelism_and_reserves_multiple_streams(self):
        limits = NetworkLimits()
        limits.configure(4, 8)
        with limits.session(lambda: None, lambda: None, streams=2) as options:
            self.assertEqual(options["concurrent_fragment_downloads"], 3)
            self.assertEqual(limits.active_connections, 8)
        limits.configure(4, 2)
        with limits.session(lambda: None, lambda: None, streams=2) as options:
            self.assertEqual(options["concurrent_fragment_downloads"], 1)
            self.assertEqual(limits.active_connections, 2)
        limits.configure(4, 8)
        with limits.session(lambda: None, lambda: None, fragmented=False) as options:
            self.assertEqual(options["concurrent_fragment_downloads"], 1)
            self.assertEqual(limits.active_connections, 1)
        self.assertEqual(limits.active_connections, 0)

    def test_lower_budget_waits_for_active_sessions_and_releases_after_failure(self):
        limits = NetworkLimits()
        limits.configure(4, 4)
        release = threading.Event()
        entered = threading.Event()
        waiting = threading.Event()
        def first():
            with limits.session(lambda: None, lambda: None):
                entered.set()
                release.wait(3)
        def second():
            with limits.session(lambda: None, waiting.set) as options:
                self.assertEqual(options["concurrent_fragment_downloads"], 1)
                raise RuntimeError("download failed")
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                one = pool.submit(first)
                self.assertTrue(entered.wait(2))
                two = pool.submit(second)
                self.assertTrue(waiting.wait(2))
                limits.configure(4, 2)
                self.assertEqual(limits.active_connections, 4)
                release.set()
                one.result(timeout=2)
                with self.assertRaises(RuntimeError):
                    two.result(timeout=2)
            finally:
                release.set()
        self.assertEqual(limits.active_connections, 0)
        self.assertFalse(limits.waiters)

    def test_cancelled_waiter_never_reserves_capacity(self):
        limits = NetworkLimits()
        limits.configure(1, 2)
        cancel, waiting = threading.Event(), threading.Event()
        def check():
            if cancel.is_set():
                raise DownloadCancelled()
        def wait():
            with limits.session(check, waiting.set):
                self.fail("Cancelled download must not start")
        with limits.session(lambda: None, lambda: None, streams=2), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(wait)
            self.assertTrue(waiting.wait(2))
            cancel.set()
            with self.assertRaises(DownloadCancelled):
                future.result(timeout=2)
            self.assertEqual(limits.active_connections, 2)
        self.assertEqual(limits.active_connections, 0)

    def test_pause_peer_failure_and_shutdown_interrupt_engine_capacity_wait(self):
        for reason in ("peer", "shutdown"):
            with self.subTest(reason=reason):
                manager = TaskManager(engine_factory=lambda manager: Mock())
                engine = download_engine.DownloadEngine(manager, ffmpeg="ffmpeg", ffprobe="ffprobe")
                manager.engine = engine
                stop = threading.Event()
                manager.paused = True
                def enter():
                    with engine.media_download_session("T1", stop_event=stop):
                        self.fail("Paused session must not start")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(enter)
                    wait_until(lambda: bool(engine.network_limits.waiters))
                    self.assertEqual(engine.network_limits.active_connections, 0)
                    if reason == "peer":
                        stop.set()
                    else:
                        manager.close()
                    with self.assertRaises(DownloadCancelled):
                        future.result(timeout=2)
                manager.close()
                self.assertFalse(engine.network_limits.waiters)

    def test_invalid_live_and_saved_values_are_rejected(self):
        limits = NetworkLimits()
        for fragments, connections in ((0, 8), (5, 8), (True, 8), (1, 1), (1, 33), (1, False)):
            with self.subTest(fragments=fragments, connections=connections), self.assertRaises(ValueError):
                limits.configure(fragments, connections)
        data, issues = validate_settings({"fragment_downloads": 99, "network_connections": -1})
        self.assertEqual(data["fragment_downloads"], DEFAULTS["fragment_downloads"])
        self.assertEqual(data["network_connections"], DEFAULTS["network_connections"])
        self.assertIn("fragment_downloads", issues)
        self.assertIn("network_connections", issues)

    def test_cancelled_requests_do_not_launch_a_downloader_process(self):
        cancel = threading.Event()
        def check():
            if cancel.is_set():
                raise DownloadCancelled()
        with download_engine.YtDlpProcess({'quiet': True}, check=check) as ydl, \
                patch('ytdlp_process.subprocess.Popen') as process:
            cancel.set()
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(ydl.extract_info, 'http://example.com/fragment') for _ in range(40)]
                for future in futures:
                    with self.assertRaises(DownloadCancelled):
                        future.result(timeout=2)
            process.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class NativeFragmentTests(unittest.TestCase):
    def test_real_hls_downloads_parallelize_within_shared_media_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            manifest = source / "media.m3u8"
            subprocess.run([shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i",
                            "testsrc2=s=128x96:r=10:d=4", "-c:v", "libx264", "-threads", "2",
                            "-g", "5", "-sc_threshold", "0", "-f", "hls", "-hls_time", "0.5",
                            "-hls_playlist_type", "vod", str(manifest)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
            state = dict(active=0, peak=0)
            lock = threading.Lock()
            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(source), **kwargs)
                def do_GET(self):
                    with lock:
                        state["active"] += 1
                        state["peak"] = max(state["peak"], state["active"])
                    try:
                        if self.path.endswith(".ts"):
                            time.sleep(.04)
                        super().do_GET()
                    finally:
                        with lock:
                            state["active"] -= 1
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
            thread.start()
            manager = TaskManager()
            engine = manager.engine
            engine.configure_network(fragment_downloads=3, network_connections=4)
            url = f"http://127.0.0.1:{server.server_port}/media.m3u8"
            info = download_engine.YTInfo(url, {"id": "local", "title": "Local HLS", "duration": 4,
                "webpage_url": url, "formats": [{"format_id": "hls", "url": url, "ext": "mp4",
                    "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none", "height": 96}]})
            tids = manager.add_urls(["https://example.com/one", "https://example.com/two"],
                                    TaskOptions(folder=str(output)))
            for index, tid in enumerate(tids, 1):
                manager.tid_to_workerid[tid] = index
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(engine._download_format, tid, "video", url, info.formats[0],
                                           f"video-{index}.mp4", folder=str(output))
                               for index, tid in enumerate(tids, 1)]
                    paths = [future.result(timeout=15) for future in futures]
                self.assertGreaterEqual(state["peak"], 2)
                self.assertLessEqual(state["peak"], 4)
                self.assertEqual(engine.network_limits.active_connections, 0)
                self.assertFalse(engine.network_limits.waiters)
                for path in paths:
                    probe = engine._ffprobe_media(path)
                    self.assertTrue(any(stream["codec_type"] == "video" for stream in probe["streams"]))
            finally:
                manager.close()
                server.shutdown()
                thread.join(2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
