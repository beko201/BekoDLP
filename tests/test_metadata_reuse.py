from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import download_engine
from task_manager import PlaylistFetcher, TaskManager, TaskOptions
from utils import DownloadCancelled, InsufficientDiskSpace


class MetadataReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.requests = []
        self.expired_barrier = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('X-Media-Test')))
                expired = self.path.startswith('/expired/')
                if expired and owner.expired_barrier:
                    owner.expired_barrier.wait(3)
                payload = b'expired' if expired else self.path.rsplit('/', 1)[-1].encode() * 100
                self.send_response(403 if expired else 200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server_thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.server_thread.start()

        def stop_server():
            self.server.shutdown()
            self.server_thread.join(2)
            self.server.server_close()

        self.addCleanup(stop_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.manager = TaskManager()
        self.options = TaskOptions(folder=str(self.folder), format='MP3')
        self.tid = self.manager.add_urls(['https://example.com/watch'], self.options)[0]
        self.manager.tid_to_workerid[self.tid] = 1
        self.engine = self.manager.engine

    def info(self, expired=False):
        prefix = '/expired' if expired else ''
        return download_engine.YTInfo('https://example.com/watch', {
            'id': 'example', 'title': 'Example', 'duration': 1,
            'webpage_url': 'https://example.com/watch',
            'formats': [
                {'format_id': 'video', 'url': self.base + prefix + '/video', 'ext': 'mp4',
                 'vcodec': 'avc1', 'acodec': 'none', 'height': 360,
                 'http_headers': {'X-Media-Test': 'video-header'}},
                {'format_id': 'audio', 'url': self.base + prefix + '/audio', 'ext': 'webm',
                 'vcodec': 'none', 'acodec': 'opus', 'abr': 128,
                 'http_headers': {'X-Media-Test': 'audio-header'}},
            ],
        })

    def group(self, info):
        return self.engine._download_streams_parallel(
            self.tid, info.url, info.video_only_formats()[0], info.audio_only_formats()[0],
            'video.mp4', 'audio.webm', folder=str(self.folder))

    def test_real_parallel_download_reuses_info_and_preserves_headers(self):
        with patch.object(download_engine.YtDlpProcess, 'extract_info', side_effect=AssertionError('redundant extraction')):
            video, audio = self.group(self.info())
        self.assertEqual(Path(video).read_bytes(), b'video' * 100)
        self.assertEqual(Path(audio).read_bytes(), b'audio' * 100)
        self.assertCountEqual(self.requests, [('/video', 'video-header'), ('/audio', 'audio-header')])

    def test_expired_parallel_urls_share_one_refresh_and_keep_selected_formats(self):
        self.expired_barrier = threading.Barrier(2)
        with patch.object(download_engine.YtDlpProcess, 'extract_info', side_effect=AssertionError('redundant extraction')), \
                patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info()) as refresh:
            video, audio = self.group(self.info(expired=True))
        refresh.assert_called_once()
        self.assertEqual(Path(video).read_bytes(), b'video' * 100)
        self.assertEqual(Path(audio).read_bytes(), b'audio' * 100)
        self.assertEqual(len(self.requests), 4)
        self.assertFalse(self.manager.is_cancelled(self.tid))

    def test_cached_snapshots_isolate_nested_mutations_and_old_output_paths(self):
        info = self.info()
        info._media_info.info['requested_downloads'] = [{'filepath': 'old.mp4'}]
        first, _ = info._media_info.snapshot('video')
        first['formats'][0]['http_headers']['X-Media-Test'] = 'changed'
        first['formats'][0]['url'] = 'changed'
        second, _ = info._media_info.snapshot('video')
        self.assertEqual(second['formats'][0]['http_headers']['X-Media-Test'], 'video-header')
        self.assertEqual(second['formats'][0]['url'], self.base + '/video')
        self.assertNotIn('requested_downloads', second)
        self.assertEqual([fmt['format_id'] for fmt in second['formats']], ['video'])

    def test_submission_reuses_resolved_info_through_download_and_publication(self):
        # Use a new manager: the prepared task above is intentionally not submitted here.
        manager = TaskManager()
        info = self.info()

        def convert(tid, worker_id, source, output):
            Path(output).write_bytes(Path(source).read_bytes())

        with patch('engine_metadata._flat_extract', return_value=(info._media_info.info, None)), \
                patch('engine_metadata.fetch_ytinfo_with_retry') as fetch, \
                patch.object(download_engine.YtDlpProcess, 'extract_info', side_effect=AssertionError('redundant extraction')), \
                patch.object(manager.engine, '_ffmpeg_convert_to_mp3', side_effect=convert):
            PlaylistFetcher(manager, info.url, self.options, False).run()
            manager._process_task(('T1', info.url), 1)
        fetch.assert_not_called()
        task = manager.get_task('T1')
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assertEqual(Path(task['output_path']).read_bytes(), b'audio' * 100)
        self.assertNotIn('formats', task)
        self.assertIsNone(manager.take_resolved_info('T1'))

    def test_selector_fallback_reuses_info_with_real_ytdlp(self):
        def convert(tid, worker_id, source, output):
            Path(output).write_bytes(Path(source).read_bytes())

        with patch.object(download_engine.YtDlpProcess, 'extract_info', side_effect=AssertionError('redundant extraction')), \
                patch.object(self.engine, '_ffmpeg_convert_to_mp3', side_effect=convert):
            self.engine._complete_via_selector(
                self.tid, 1, 'url', 'Example', 'Example', 'MP3', None, False, '', '',
                folder=str(self.folder), media_info=self.info()._media_info)
        task = self.manager.get_task(self.tid)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assertEqual(Path(task['output_path']).read_bytes(), b'audio' * 100)

    def test_persistent_expiry_refreshes_only_once(self):
        ydl = Mock()
        expired = HTTPError(self.base, 403, 'expired', {}, None)
        ydl.process_ie_result.side_effect = expired
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info()) as refresh:
            with self.assertRaises(HTTPError):
                self.engine._download_from_info(
                    ydl, 'url', self.info()._media_info, threading.Event(), format_id='video')
        refresh.assert_called_once()
        self.assertEqual(ydl.process_ie_result.call_count, 2)

    def test_disk_error_and_cancellation_do_not_refresh(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                stop = threading.Event()
                ydl = Mock()

                def fail(*args, **kwargs):
                    if cancel:
                        stop.set()
                        raise HTTPError(self.base, 403, 'expired', {}, None)
                    raise InsufficientDiskSpace('no space')

                ydl.process_ie_result.side_effect = fail
                with patch('engine_metadata.fetch_ytinfo_with_retry') as refresh:
                    with self.assertRaises(DownloadCancelled if cancel else InsufficientDiskSpace):
                        self.engine._download_from_info(
                            ydl, 'url', self.info()._media_info, threading.Event(),
                            format_id='video', stop_event=stop)
                refresh.assert_not_called()

    def test_refresh_never_substitutes_higher_resolution_when_selected_format_disappears(self):
        fresh = deepcopy(self.info()._media_info.info)
        fresh['formats'][0].update(format_id='4k', height=2160)
        ydl = Mock()
        ydl.process_ie_result.side_effect = HTTPError(self.base, 403, 'expired', {}, None)
        with patch('engine_metadata.fetch_ytinfo_with_retry',
                          return_value=download_engine.YTInfo('url', fresh)):
            with self.assertRaisesRegex(RuntimeError, 'selected format was not found'):
                self.engine._download_from_info(
                    ydl, 'url', self.info()._media_info, threading.Event(), format_id='video')
        ydl.process_ie_result.assert_called_once()

    def test_resolved_metadata_is_discarded_on_cancel_clear_and_close(self):
        for operation in ('cancel', 'clear', 'close'):
            with self.subTest(operation=operation):
                manager = TaskManager()
                manager.add_urls(['url'], self.options, resolved_info={'url': self.info()._media_info.info})
                if operation == 'clear':
                    manager.clear_list(remove_pending=True)
                elif operation == 'cancel':
                    manager.cancel_task('T1')
                else:
                    manager.close()
                self.assertIsNone(manager.take_resolved_info('T1'))


if __name__ == '__main__':
    unittest.main()
