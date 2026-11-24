"""Proxy routing, rate budgets, immutable choices and real local CLI traffic."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import asdict
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from PySide6.QtWidgets import QApplication, QMessageBox

import download_engine
from network_options import NETWORK_DEFAULTS, network_selection, validate_network_options
from queue_store import QueueStore
from task_manager import PlaylistFetcher, TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from utils import _log_text
from ytdlp_process import YtDlpProcess


class NetworkOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager()
        self.addCleanup(self.manager.close)
        self.choice = dict(proxy_mode='custom', proxy_url='socks5://127.0.0.1:1080', download_rate='1048576')

    def test_proxy_modes_and_limit_are_separate_cli_arguments(self):
        for mode, expected in (('system', None), ('direct', ''), ('custom', self.choice['proxy_url'])):
            args = YtDlpProcess(dict(self.choice, proxy_mode=mode))._arguments()
            if expected is None:
                self.assertNotIn('--proxy', args)
            else:
                self.assertEqual(args[args.index('--proxy') + 1], expected)
            self.assertEqual(args[args.index('--limit-rate') + 1], '1048576')
        self.assertNotIn('--limit-rate', YtDlpProcess()._arguments())

    def test_invalid_proxy_and_rate_are_rejected(self):
        for url in ('127.0.0.1:8080', 'http://host', 'file:///private', 'http://host:99999',
                    'http://host:80/path', 'socks5://host:80\n--exec evil'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_network_options(dict(self.choice, proxy_url=url))
        for rate in ('-1', '0', 'nan', '5M', '1\n2', '999999999999999'):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                TaskOptions(folder=str(self.folder), download_rate=rate)

    def test_queue_roundtrip_and_legacy_defaults(self):
        tid = self.manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.folder), **self.choice))[0]
        store = QueueStore(self.folder / 'queue.json')
        store.save(self.manager.queue_snapshot()[1])
        restored = TaskManager(engine_factory=lambda m: Mock())
        self.addCleanup(restored.close)
        document = store.load()[0]
        restored.restore_queue(document)
        self.assertEqual(network_selection(restored.get_task(tid)), self.choice)
        for key in NETWORK_DEFAULTS:
            document['tasks'][0]['options'].pop(key)
        legacy = TaskManager(engine_factory=lambda m: Mock())
        self.addCleanup(legacy.close)
        legacy.restore_queue(document)
        self.assertEqual(network_selection(legacy.get_task(tid)), NETWORK_DEFAULTS)

    def test_resolver_and_expired_metadata_refresh_keep_the_proxy(self):
        options = TaskOptions(folder=str(self.folder), **self.choice)
        with patch.object(download_engine, '_extract_urls_from_input', return_value=['https://example.com/video']) as resolve:
            PlaylistFetcher(self.manager, 'https://example.com/video', options).run()
        self.assertEqual(resolve.call_args.kwargs['network_options'], self.choice)
        info = download_engine.YTInfo('https://example.com/video', {'id': 'test'}, network_options=self.choice)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info) as fetch:
            info._media_info.refresh(0, threading.Event(), None)
        self.assertEqual(fetch.call_args.kwargs['network_options'], self.choice)

    def test_parallel_streams_share_the_limit_and_disable_fragment_multiplication(self):
        tid = self.manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.folder), **self.choice))[0]
        info = download_engine.YTInfo('https://example.com/video', {'id': 'test', 'formats': [
            {'format_id': 'v', 'url': 'https://example.com/v', 'vcodec': 'avc1', 'acodec': 'none', 'height': 360},
            {'format_id': 'a', 'url': 'https://example.com/a', 'vcodec': 'none', 'acodec': 'aac'}]})
        captured = []
        def download(tid, key, url, stream, filename, *, folder, stop_event=None):
            process = self.manager.engine.ytdlp_process({'concurrent_fragment_downloads': 4}, tid)
            captured.append(process.options)
            path = Path(folder, filename)
            path.write_bytes(b'test')
            return str(path)
        with patch.object(self.manager.engine, '_download_format', side_effect=download):
            self.manager.engine._download_streams_parallel(tid, info.url, info.video_only_formats()[0],
                info.audio_only_formats()[0], 'video.mp4', 'audio.m4a', folder=str(self.folder))
        self.assertEqual([c['download_rate'] for c in captured], ['524288', '524288'])
        self.assertTrue(all(c['concurrent_fragment_downloads'] == 1 for c in captured))
        self.assertEqual(self.manager.engine.ytdlp_process({}, tid).options['download_rate'], '1048576')

    def test_ui_validates_and_freezes_network_choices(self):
        manager = TaskManager(engine_factory=lambda m: Mock())
        self.addCleanup(manager.close)
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=self.folder / 'settings.json')
        try:
            controls = window.output_controls.network
            controls.restore(self.choice)
            self.assertEqual(controls.values(), self.choice)
            window.folder = str(self.folder)
            window.url_edit.setText('https://example.com/video')
            with patch.object(manager, 'submit_link', return_value=True) as submit:
                window.enqueue()
            frozen = submit.call_args.args[1]
            controls.proxy_combo.setCurrentIndex(controls.proxy_combo.findData('direct'))
            self.assertEqual(network_selection(asdict(frozen)), self.choice)
            controls.proxy_combo.setCurrentIndex(controls.proxy_combo.findData('custom'))
            controls.proxy_edit.setText('invalid')
            window.url_edit.setText('https://example.com/second')
            with patch.object(manager, 'submit_link') as submit, patch.object(window, '_show_message') as show:
                window.enqueue()
            submit.assert_not_called()
            self.assertEqual(show.call_args.args[1], 'Network settings')
        finally:
            for box in list(window._message_boxes):
                box.done(QMessageBox.StandardButton.Ok)
            finish_window_close(window)
            window.controller.poll()
            self.app.processEvents()

    def test_proxy_credentials_are_redacted_in_http_and_socks_logs(self):
        for scheme in ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h'):
            self.assertNotIn('private-pass', _log_text(f'{scheme}://user:private-pass@localhost:8080'))

    def test_real_cli_routes_metadata_and_rate_limited_media_through_a_local_proxy(self):
        self._assert_local_proxy_transfer(authenticated=False)

    def test_real_cli_routes_authenticated_metadata_and_media_without_credentials_in_cmdline(self):
        self._assert_local_proxy_transfer(authenticated=True)

    def test_real_cli_proxy_config_preserves_unicode_and_quoted_credentials(self):
        self._assert_local_proxy_transfer(authenticated=True, password="páss'\"\\@word")

    def _assert_local_proxy_transfer(self, *, authenticated, password='private-pass'):
        paths = []
        authorization = []
        command_lines = []
        expected_auth = {'Basic ' + base64.b64encode(('audit-user:' + password).encode(encoding)).decode('ascii')
                         for encoding in ('utf-8', 'latin-1')}
        payload = b'x' * (256 * 1024)
        class Proxy(BaseHTTPRequestHandler):
            def respond(self, body):
                paths.append(self.path)
                authorization.append(self.headers.get('Proxy-Authorization'))
                if authenticated and self.headers.get('Proxy-Authorization') not in expected_auth:
                    self.send_response(407)
                    self.send_header('Proxy-Authenticate', 'Basic realm="audit"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                watch = urlsplit(self.path).path == '/watch.html'
                content = b'<title>Proxy media</title><video src="http://media.example.invalid/media.mp4"></video>' if watch else payload
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if watch else 'video/mp4')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                if body:
                    self.wfile.write(content)
            def do_GET(self):
                self.respond(True)
            def do_HEAD(self):
                self.respond(False)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        credentials = 'audit-user:' + password + '@' if authenticated else ''
        selected = dict(proxy_mode='custom', proxy_url=f'http://{credentials}127.0.0.1:{server.server_port}', download_rate='131072')
        url = 'http://media.example.invalid/watch.html'
        try:
            import ytdlp_process
            original_spawn = ytdlp_process.spawn_owned_process
            def spawn(args, **kwargs):
                command_lines.append(list(args))
                return original_spawn(args, **kwargs)
            with patch('ytdlp_process.spawn_owned_process', side_effect=spawn):
                info = download_engine.fetch_ytinfo_with_retry(url, max_retries=1, network_options=selected)
                tid = self.manager.add_urls([url], TaskOptions(folder=str(self.folder), **selected))[0]
                self.manager.tid_to_workerid[tid] = 1
                started = time.monotonic()
                output = self.manager.engine._download_format(tid, 'video', url, info.formats[0], 'limited.mp4', folder=str(self.folder))
            self.assertGreaterEqual(time.monotonic() - started, 1.5)
            self.assertEqual(Path(output).read_bytes(), payload)
            self.assertTrue(any('/watch.html' in p for p in paths))
            self.assertTrue(any('/media.mp4' in p for p in paths))
            self.assertTrue(all(p.startswith('http://media.example.invalid/') for p in paths))
            if authenticated:
                self.assertTrue(all(value in expected_auth for value in authorization))
                self.assertGreaterEqual(len(command_lines), 2)
                self.assertNotIn(password, str(command_lines))
                self.assertNotIn('audit-user', str(command_lines))
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
