from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import download_engine
from task_manager import TaskManager, TaskOptions
from utils import DownloadCancelled


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Download did not reach the expected state')
        time.sleep(0.01)


class DownloadPauseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                payload = self.path.encode() * 100
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        server_thread.start()

        def stop_server():
            server.shutdown()
            server_thread.join(2)
            server.server_close()

        self.addCleanup(stop_server)
        self.manager = TaskManager()
        self.addCleanup(self.manager.close)
        self.tid = self.manager.add_urls(['https://example.com/watch'], TaskOptions(folder=str(self.folder)))[0]
        self.manager.tid_to_workerid[self.tid] = 1
        self.manager.paused = True
        self.engine = self.manager.engine
        self.info = download_engine.YTInfo('https://example.com/watch', {
            'id': 'example', 'title': 'Example', 'duration': 1,
            'webpage_url': 'https://example.com/watch',
            'formats': [
                {'format_id': 'v', 'url': f'http://127.0.0.1:{server.server_port}/video',
                 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'height': 360},
                {'format_id': 'a', 'url': f'http://127.0.0.1:{server.server_port}/audio',
                 'ext': 'webm', 'vcodec': 'none', 'acodec': 'opus', 'abr': 128},
            ],
        })

    def run_async(self, operation):
        results = []

        def run():
            try:
                results.append(operation())
            except BaseException as error:
                results.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def stop():
            self.manager.close()
            thread.join(4)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        wait_until(lambda: self.manager.get_task(self.tid)['status'] == 'Paused')
        self.assertFalse(thread.join(0.15))
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.requests, [])
        return thread, results

    def test_real_parallel_download_sends_no_requests_until_resume(self):
        thread, results = self.run_async(lambda: self.engine._download_streams_parallel(
            self.tid, self.info.url, self.info.video_only_formats()[0], self.info.audio_only_formats()[0],
            'video.mp4', 'audio.webm', folder=str(self.folder)))
        self.manager.toggle_pause()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertNotIsInstance(results[0], BaseException)
        self.assertCountEqual(self.requests, ['/video', '/audio'])
        self.assertEqual(Path(results[0][0]).read_bytes(), b'/video' * 100)
        self.assertEqual(Path(results[0][1]).read_bytes(), b'/audio' * 100)

    def test_selector_fallback_also_waits_before_real_network_request(self):
        def convert(tid, worker_id, source, output):
            Path(output).write_bytes(Path(source).read_bytes())

        with patch.object(self.engine, '_ffmpeg_convert_to_mp3', side_effect=convert):
            thread, results = self.run_async(lambda: self.engine._complete_via_selector(
                self.tid, 1, self.info.url, 'Example', 'Example', 'MP3', None, False, '', '',
                folder=str(self.folder), media_info=self.info._media_info))
            self.manager.toggle_pause()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(self.requests, ['/audio'])
        self.assertTrue(self.manager.get_task(self.tid)['status'].startswith('Completed'))

    def test_cancel_shutdown_and_peer_failure_interrupt_wait_without_network(self):
        for signal in ('cancel', 'shutdown', 'peer'):
            with self.subTest(signal=signal):
                # Reset only task-local state between these network-free attempts.
                self.manager._closing = False
                self.manager._shutdown_event.clear()
                self.manager.cancel_events[self.tid].clear()
                self.manager.paused = True
                self.manager.update_task(self.tid, status='Queued')
                stop_event = threading.Event()
                with patch.object(download_engine, 'YtDlpProcess') as ydl:
                    thread, results = self.run_async(lambda: self.engine._download_format(
                        self.tid, 'video', self.info.url, self.info.video_only_formats()[0],
                        'video.mp4', folder=str(self.folder), stop_event=stop_event))
                    if signal == 'cancel':
                        self.manager.cancel_task(self.tid)
                    elif signal == 'shutdown':
                        self.manager.close()
                    else:
                        stop_event.set()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertIsInstance(results[0], DownloadCancelled)
                    ydl.assert_not_called()
                self.assertEqual(self.requests, [])
                self.assertEqual(list(self.folder.iterdir()), [])
                if signal == 'peer':
                    self.assertFalse(self.manager.is_cancelled(self.tid))

    def test_paused_task_does_not_start_metadata_extraction(self):
        with patch.object(download_engine, 'fetch_ytinfo_with_retry') as fetch:
            thread, results = self.run_async(lambda: self.engine.process_task((self.tid, self.info.url), 1))
            fetch.assert_not_called()
            self.manager.cancel_task(self.tid)
            thread.join(2)
            fetch.assert_not_called()
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(self.manager.get_task(self.tid)['status'], 'Cancelled')


if __name__ == '__main__':
    unittest.main()
