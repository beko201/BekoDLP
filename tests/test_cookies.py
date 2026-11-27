"""Cookie references, temporary jars and authenticated local CLI requests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from cookie_options import (COOKIE_DEFAULTS, cookie_selection, validate_cookie_options,
                            cookie_error_guidance, BROWSER_COOKIE_COPY_HELP)
from ui.i18n import language, set_language, tr
import download_engine
from queue_store import QueueStore
from settings import validate_settings
from task_manager import PlaylistFetcher, TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from errors import DownloadCancelled
from ytdlp_process import YtDlpProcess, YtDlpProcessError


class CookieTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.jar = self.folder / 'my cookies.txt'
        self.original = (b'# Netscape HTTP Cookie File\n'
            b'127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tfixture-session-secret\n')
        self.jar.write_bytes(self.original)
        self.selection = dict(COOKIE_DEFAULTS, cookie_mode='file', cookie_file=str(self.jar))
        self.managers, self.windows = [], []

    def manager(self, *, real=False):
        manager = TaskManager() if real else TaskManager(engine_factory=lambda manager: Mock())
        self.managers.append(manager)
        return manager

    def tearDown(self):
        for window in self.windows:
            for box in list(window._message_boxes):
                box.done(QMessageBox.StandardButton.Ok)
            finish_window_close(window)
        for manager in self.managers:
            manager.close()
        for window in self.windows:
            window.controller.poll()
        for _ in range(3):
            self.app.processEvents()

    def window(self, path=None):
        manager = self.manager()
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=path or self.folder / 'settings.json')
        self.windows.append(window)
        return window, manager

    def test_browser_and_profile_are_one_cli_argument_without_reading_a_browser(self):
        selection = dict(COOKIE_DEFAULTS, cookie_mode='browser', cookie_browser='edge',
                         cookie_profile=r'C:\Browser Profiles\Profile 2')
        with YtDlpProcess(selection)._cookie_arguments() as args:
            self.assertEqual(args, ['--cookies-from-browser', r'edge:C:\Browser Profiles\Profile 2'])
        with YtDlpProcess()._cookie_arguments() as args:
            self.assertEqual(args, [])

    def test_cookie_copy_error_has_specific_help_and_unrelated_errors_do_not(self):
        message = 'yt-dlp failed: ERROR: Could not copy Chrome cookie database.'
        self.assertEqual(cookie_error_guidance(message), BROWSER_COOKIE_COPY_HELP)
        self.assertEqual(cookie_error_guidance('HTTP Error 403: Forbidden'), '')
        self.assertEqual(cookie_error_guidance('Permission denied: output.mp4'), '')

    def test_cookie_copy_help_is_localized_without_changing_cookie_settings(self):
        previous = language()
        self.addCleanup(set_language, previous)
        window, manager = self.window()
        selection = dict(COOKIE_DEFAULTS, cookie_mode='browser', cookie_browser='chrome')
        window.output_controls.cookies.restore(selection)
        original = cookie_selection(window.output_controls.values())
        set_language('tr')
        error = 'yt-dlp failed: ERROR: Could not copy Chrome cookie database.'
        window._show_message('critical', 'Link resolution failed', 'Could not resolve the URL.', error)
        box = next(iter(window._message_boxes))
        self.assertEqual(box.text(), tr(BROWSER_COOKIE_COPY_HELP))
        self.assertIn('arka plan işlemleri', box.text())
        self.assertEqual(box.detailedText(), error)
        self.assertEqual(cookie_selection(window.output_controls.values()), original)

    def test_each_process_uses_a_distinct_copy_and_never_changes_the_source(self):
        with YtDlpProcess(self.selection)._cookie_arguments() as first, \
                YtDlpProcess(self.selection)._cookie_arguments() as second:
            first_path, second_path = Path(first[1]), Path(second[1])
            self.assertNotEqual(first_path, second_path)
            self.assertNotEqual(first_path, self.jar)
            self.assertEqual(first_path.read_bytes(), self.original)
            first_path.write_bytes(b'changed by downloader')
            self.assertEqual(second_path.read_bytes(), self.original)
        self.assertFalse(first_path.exists())
        self.assertFalse(second_path.exists())
        self.assertEqual(self.jar.read_bytes(), self.original)

    def test_cancelled_process_removes_its_cookie_copy(self):
        paths = []
        def cancel(args, consume):
            paths.append(Path(args[args.index('--cookies') + 1]))
            self.assertTrue(paths[-1].exists())
            raise DownloadCancelled()
        adapter = YtDlpProcess(self.selection)
        with patch.object(adapter, '_run_process', side_effect=cancel):
            with self.assertRaises(DownloadCancelled):
                adapter.extract_info('https://example.com/video')
        self.assertFalse(paths[0].exists())
        self.assertEqual(self.jar.read_bytes(), self.original)

    def test_bad_cookie_file_is_reported_without_exposing_its_contents(self):
        self.jar.write_bytes(b'# Netscape HTTP Cookie File\ninvalid\tprivate-cookie-value\n')
        with self.assertRaises(YtDlpProcessError) as caught:
            with YtDlpProcess(self.selection)._cookie_arguments():
                self.fail('Malformed cookie file accepted')
        self.assertIn('Netscape cookies.txt', str(caught.exception))
        self.assertNotIn('private-cookie-value', str(caught.exception))

    def test_invalid_cookie_configuration_is_rejected(self):
        for values in (dict(cookie_mode='unknown'), dict(cookie_mode='file'),
                       dict(cookie_browser='edge --exec bad'), dict(cookie_profile='bad\nprofile')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                TaskOptions(folder=str(self.folder), **values)
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            validate_cookie_options(dict(self.selection, cookie_file=str(self.folder / 'missing.txt')), check_file=True)

    def test_preferences_and_old_queue_files_use_safe_defaults(self):
        defaults, issues = validate_settings({})
        self.assertFalse(issues)
        self.assertEqual(cookie_selection(defaults), COOKIE_DEFAULTS)
        invalid, issues = validate_settings({'cookie_mode': 'file', 'cookie_file': ''})
        self.assertTrue(issues)
        self.assertEqual(invalid['cookie_mode'], 'none')
        manager = self.manager()
        manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.folder)))
        document = manager.queue_snapshot()[1]
        for name in COOKIE_DEFAULTS:
            document['tasks'][0]['options'].pop(name)
        restored = self.manager()
        restored.restore_queue(document)
        self.assertEqual(cookie_selection(restored.get_task('T1')), COOKIE_DEFAULTS)

    def test_queue_saves_source_references_and_not_cookie_contents(self):
        manager = self.manager()
        manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.folder), **self.selection))
        path = self.folder / 'queue.json'
        store = QueueStore(path)
        store.save(manager.queue_snapshot()[1])
        self.assertNotIn('fixture-session-secret', path.read_text(encoding='utf-8'))
        restored = self.manager()
        restored.restore_queue(store.load()[0])
        self.assertEqual(cookie_selection(restored.get_task('T1')), self.selection)

    def test_resolver_receives_the_frozen_task_cookie_source(self):
        manager = self.manager()
        options = TaskOptions(folder=str(self.folder), **self.selection)
        with patch.object(download_engine, '_extract_urls_from_input', return_value=['https://example.com/video']) as resolve:
            PlaylistFetcher(manager, 'https://example.com/list', options, True).run()
        self.assertEqual(resolve.call_args.kwargs['cookie_options'], self.selection)
        self.assertEqual(cookie_selection(manager.get_task('T1')), self.selection)

    def test_expired_url_refresh_keeps_the_original_cookie_source(self):
        info = download_engine.YTInfo('https://example.com/video', {'id': 'test'}, self.selection)
        refreshed = download_engine.YTInfo('https://example.com/video', {'id': 'new'}, self.selection)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=refreshed) as fetch:
            info._media_info.refresh(0, threading.Event(), None)
        self.assertEqual(fetch.call_args.kwargs['cookie_options'], self.selection)

    def test_settings_restore_and_new_tasks_capture_cookie_source(self):
        window, manager = self.window()
        controls = window.output_controls.cookies
        controls.restore(self.selection)
        controls.changed.emit()
        self.assertTrue(window.preferences.flush())
        window.folder = str(self.folder)
        window.url_edit.setText('https://example.com/video')
        with patch.object(manager, 'submit_link', return_value=True) as submit:
            window.enqueue()
        frozen = submit.call_args.args[1]
        controls.mode_combo.setCurrentIndex(controls.mode_combo.findData('none'))
        self.assertEqual(cookie_selection(asdict(frozen)), self.selection)
        self.assertEqual(controls.values()['cookie_mode'], 'none')
        saved = json.loads((self.folder / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(cookie_selection(saved), self.selection)
        second, _ = self.window()
        self.assertEqual(second.output_controls.cookies.values(), self.selection)

    def test_missing_file_does_not_start_resolution_or_clear_the_url(self):
        window, manager = self.window()
        window.folder = str(self.folder)
        controls = window.output_controls.cookies
        controls.restore(dict(self.selection, cookie_file=str(self.folder / 'missing.txt')))
        window.url_edit.setText('https://example.com/video')
        with patch.object(manager, 'submit_link') as submit, patch.object(window, '_show_message') as show:
            window.enqueue()
        submit.assert_not_called()
        self.assertEqual(show.call_args.args[1], 'Cookie settings')
        self.assertEqual(window.url_edit.text(), 'https://example.com/video')

    def test_real_cli_uses_cookies_for_metadata_and_parallel_downloads(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cookie = self.headers.get('Cookie', '')
                requests.append((self.path, cookie))
                if 'session=fixture-session-secret' not in cookie:
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == '/media.mp4' and 'server_update=new-value' not in cookie:
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path.startswith('/watch'):
                    payload = b'<title>Authenticated media</title><video src="/media.mp4"></video>'
                    mime = 'text/html'
                else:
                    payload, mime = b'authenticated media' * 100, 'video/mp4'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Set-Cookie', 'server_update=new-value; Path=/')
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(YtDlpProcessError):
                download_engine.fetch_ytinfo_with_retry(base + '/watch.html', max_retries=1)
            requests.clear()
            info, _ = download_engine._flat_extract(base + '/watch.html', cookie_options=self.selection)
            self.assertTrue(info['id'])
            fetched = download_engine.fetch_ytinfo_with_retry(base + '/watch.html', max_retries=1,
                                                             cookie_options=self.selection)
            self.assertEqual(fetched._media_info.cookie_options, self.selection)
            manager = self.manager(real=True)
            tid = manager.add_urls([base + '/watch.html'], TaskOptions(folder=str(self.folder), **self.selection))[0]
            manager.tid_to_workerid[tid] = 1
            single = manager.engine._download_format(tid, 'single', fetched.url, fetched.formats[0],
                                                     'authenticated-single.mp4', folder=str(self.folder))
            self.assertEqual(Path(single).read_bytes(), b'authenticated media' * 100)
            sources = download_engine.YTInfo(base + '/watch.html', {'id': 'streams', 'title': 'Streams',
                'formats': [
                    {'format_id': 'video', 'url': base + '/video.mp4', 'ext': 'mp4',
                     'vcodec': 'avc1', 'acodec': 'none', 'height': 360},
                    {'format_id': 'audio', 'url': base + '/audio.m4a', 'ext': 'm4a',
                     'vcodec': 'none', 'acodec': 'aac', 'abr': 128}]}, self.selection)
            video, audio = manager.engine._download_streams_parallel(tid, sources.url,
                sources.video_only_formats()[0], sources.audio_only_formats()[0],
                'output-video.mp4', 'output-audio.m4a', folder=str(self.folder))
            self.assertEqual(Path(video).read_bytes(), b'authenticated media' * 100)
            self.assertEqual(Path(audio).read_bytes(), b'authenticated media' * 100)
            self.assertTrue(requests)
            self.assertTrue(all('session=fixture-session-secret' in cookie for _, cookie in requests))
            self.assertEqual(self.jar.read_bytes(), self.original)
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
