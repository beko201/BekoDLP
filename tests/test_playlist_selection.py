"""Playlist preview selection, cancellation, immutable settings and real CLI metadata."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from playlist_selection import PreviewCancellation, load_playlist_preview, playlist_input_url, preview_entry
from queue_store import QueueStore
from resolved_info_cache import ResolvedInfoCache
from task_manager import TaskManager, TaskOptions
from ui.bulk_add_dialog import BulkAddDialog
from ui.main_window import MainWindow
from qt_support import finish_window_close
from ui.playlist_selection import PlaylistModel, PlaylistSelectionDialog
from utils import DownloadCancelled


def entry(number, **kwargs):
    return dict(playlist_index=number, id=f'v{number}', title=f'Video {number}',
                url=f'https://example.com/watch/{number}', duration=number * 60, **kwargs)


class PlaylistSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(self.manager.close)
        self.options = TaskOptions(folder=str(self.folder), output_mode='video', container='mkv',
            cookie_mode='browser', cookie_browser='firefox', proxy_mode='direct', download_rate='1048576')

    def dialog(self):
        dialog = PlaylistSelectionDialog(self.manager, 'https://example.com/playlist', self.options)
        self.addCleanup(dialog.reject)
        return dialog

    def rows(self, *numbers):
        return [preview_entry(entry(number), index + 1, 'https://example.com/playlist') for index, number in enumerate(numbers)]

    def test_bulk_dialog_exposes_selection_only_for_one_link(self):
        dialog = BulkAddDialog()
        self.addCleanup(dialog.reject)
        submitted = Mock()
        dialog.playlist_requested.connect(submitted)
        self.assertFalse(dialog.playlist_button.isEnabled())
        dialog.links_edit.setPlainText('https://example.com/playlist')
        self.assertTrue(dialog.playlist_button.isEnabled())
        dialog.playlist_button.click()
        submitted.assert_called_once_with('https://example.com/playlist')
        dialog.links_edit.setPlainText('https://example.com/one\nhttps://example.com/two')
        self.assertFalse(dialog.playlist_button.isEnabled())
        self.assertTrue(dialog.submit_button.isEnabled())

    def test_range_uses_original_playlist_positions_including_missing_rows(self):
        model = PlaylistModel()
        model.append(self.rows(1, 3, 7, 8))
        model.select(3, 7)
        self.assertEqual(model.selected_urls(), ['https://example.com/watch/3', 'https://example.com/watch/7'])
        model.select(0, 0)
        self.assertFalse(model.checked)
        model.select()
        self.assertEqual(len(model.checked), 4)

    def test_private_deleted_empty_and_nested_collections_are_not_selectable(self):
        model = PlaylistModel()
        raw = [None, dict(title='[Private video]', id='private', ie_key='Youtube'),
               dict(title='[Deleted video]', id='deleted', ie_key='Youtube'),
               dict(_type='playlist', url='https://example.com/nested'), entry(5)]
        model.append([preview_entry(value, index + 1, 'https://www.youtube.com/playlist?list=PLdemo')
                      for index, value in enumerate(raw)])
        self.assertEqual(model.checked, {4})
        for index in range(4):
            self.assertFalse(model.setData(model.index(index, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole))
            self.assertFalse(model.flags(model.index(index, 0)) & Qt.ItemFlag.ItemIsUserCheckable)

    def test_checkboxes_toggle_individual_entries(self):
        model = PlaylistModel()
        model.append(self.rows(1, 2, 3))
        self.assertTrue(model.setData(model.index(1, 0), Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(model.checked, {0, 2})
        self.assertEqual(model.data(model.index(1, 0), Qt.ItemDataRole.CheckStateRole), Qt.CheckState.Unchecked)
        self.assertTrue(model.setData(model.index(1, 0), Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole))
        self.assertEqual(len(model.checked), 3)

    def test_entry_urls_are_leaf_urls_and_youtube_ids_are_canonicalized(self):
        youtube = preview_entry(dict(id='abc', url='abc', ie_key='Youtube', title='Video'), 1,
                                'https://example.com/list')
        self.assertEqual(youtube.url, 'https://www.youtube.com/watch?v=abc')
        video = preview_entry(dict(webpage_url='https://youtube.com/watch?v=abc&list=PLdemo'), 1,
                              'https://youtube.com/playlist?list=PLdemo')
        self.assertEqual(video.url, youtube.url)
        media = preview_entry(dict(webpage_url='http://localhost/list', playlist_webpage_url='http://localhost/list',
                                   url='http://localhost/media.mp4', title='Clip'), 2, 'http://localhost/list')
        self.assertEqual(media.url, 'http://localhost/media.mp4')
        for raw in (dict(url='file:///tmp/private'), dict(id='not-a-youtube-video'), dict(url='https://example.com/\x00')):
            self.assertFalse(preview_entry(raw, 1, 'https://example.com/list').available)

    def test_watch_playlist_preview_preserves_playlist_order_instead_of_selected_video_first(self):
        for url in ('https://youtube.com/watch?v=chosen&list=PLdemo&index=10', 'https://youtu.be/chosen?list=PLdemo'):
            self.assertEqual(playlist_input_url(url), 'https://www.youtube.com/playlist?list=PLdemo')
        self.assertEqual(playlist_input_url('https://example.com/list?token=a'), 'https://example.com/list?token=a')

    def test_streaming_preview_uses_frozen_network_settings_without_queueing(self):
        rows = []
        def extract(url, **kwargs):
            self.assertEqual(kwargs['cookie_options']['cookie_browser'], 'firefox')
            self.assertEqual(kwargs['network_options']['proxy_mode'], 'direct')
            self.assertFalse(kwargs['noplaylist'])
            kwargs['on_entries']([entry(1), entry(2)])
            self.assertFalse(self.manager.list_tasks())
            kwargs['on_entries']([entry(3)])
            return {'title': 'Playlist', '_type': 'playlist'}, []
        with patch('download_engine._flat_extract', side_effect=extract):
            title, warning = load_playlist_preview('https://example.com/list', asdict(self.options), threading.Event(), rows.extend)
        self.assertEqual(title, 'Playlist')
        self.assertFalse(warning)
        self.assertEqual([row.number for row in rows], [1, 2, 3])
        self.assertFalse(self.manager.list_tasks())

    def test_partial_failure_and_limit_keep_only_displayed_entries(self):
        def extract(url, **kwargs):
            kwargs['on_entries']([entry(number) for number in range(1, 7)])
            raise RuntimeError('page unavailable')
        for limit in (3, 20):
            with self.subTest(limit=limit):
                rows = []
                with patch('download_engine._flat_extract', side_effect=extract):
                    _, warning = load_playlist_preview('https://example.com/list', {}, threading.Event(), rows.extend, limit=limit)
                self.assertEqual(len(rows), 3 if limit == 3 else 6)
                self.assertTrue(warning)
        with patch('download_engine._flat_extract', side_effect=RuntimeError('first page failed')):
            with self.assertRaisesRegex(RuntimeError, 'first page failed'):
                load_playlist_preview('https://example.com/list', {}, threading.Event(), lambda batch: None)

    def test_cancel_or_shutdown_stops_metadata_without_creating_tasks(self):
        for shutdown in (False, True):
            cancel, stop = threading.Event(), threading.Event()
            event = PreviewCancellation(cancel, stop)
            rows = []
            def extract(url, **kwargs):
                (stop if shutdown else cancel).set()
                kwargs['on_entries']([entry(1)])
            with self.subTest(shutdown=shutdown), patch('download_engine._flat_extract', side_effect=extract):
                with self.assertRaises(DownloadCancelled):
                    load_playlist_preview('https://example.com/list', {}, event, rows.extend)
                self.assertFalse(rows)
        self.assertFalse(self.manager.list_tasks())

    def test_queue_only_selected_rows_with_options_and_duplicate_reporting(self):
        dialog = self.dialog()
        dialog._receive_batch(0, self.rows(1, 2, 3, 4))
        dialog._receive_finished(0, 'Playlist', '')
        dialog.first_spin.setValue(2)
        dialog.last_spin.setValue(3)
        dialog.range_button.click()
        self.manager.add_urls(['https://example.com/watch/2'], self.options)
        queued = Mock()
        dialog.queued.connect(queued)
        dialog.enqueue()
        queued.assert_called_once_with(dialog.url, 1, 1)
        self.assertEqual([task['url'] for task in self.manager.list_tasks()],
                         ['https://example.com/watch/2', 'https://example.com/watch/3'])
        self.assertEqual(self.manager.get_task('T2')['cookie_browser'], 'firefox')
        self.assertEqual(self.manager.get_task('T2')['download_rate'], '1048576')

    def test_no_selection_stale_batches_and_loading_cannot_queue(self):
        dialog = self.dialog()
        dialog._receive_batch(1, self.rows(1))
        self.assertFalse(dialog.model.entries)
        dialog._receive_batch(0, self.rows(1, 2))
        dialog._set_loading(True)
        dialog.enqueue()
        self.assertFalse(self.manager.list_tasks())
        dialog._receive_finished(0, '', '')
        dialog.none_button.click()
        self.assertFalse(dialog.add_button.isEnabled())
        dialog.enqueue()
        self.assertFalse(self.manager.list_tasks())
        dialog.reject()
        dialog._receive_batch(0, self.rows(3))
        dialog.enqueue()
        self.assertEqual(len(dialog.model.entries), 2)
        self.assertFalse(self.manager.list_tasks())

    def test_selected_tasks_survive_queue_restore_and_wait_for_resume(self):
        dialog = self.dialog()
        dialog._receive_batch(0, self.rows(1, 2, 3))
        dialog._receive_finished(0, '', '')
        dialog.model.select(2, 2)
        dialog.enqueue()
        _, document = self.manager.queue_snapshot()
        store = QueueStore(self.folder / 'queue.json')
        store.save(document)
        restored, warnings = store.load()
        self.assertFalse(warnings)
        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        manager.restore_queue(restored)
        self.assertTrue(manager.paused)
        self.assertEqual([task['url'] for task in manager.list_tasks()], ['https://example.com/watch/2'])

    def test_bulk_selection_opens_with_immutable_options_and_main_close_cancels_it(self):
        with patch.object(self.manager, 'set_worker_count'):
            window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        try:
            window.folder = str(self.folder)
            window.open_bulk_dialog()
            window.bulk_dialog.links_edit.setPlainText('https://example.com/playlist')
            with patch.object(PlaylistSelectionDialog, 'load'):
                window.bulk_dialog.playlist_button.click()
            dialog = window.playlist_dialog
            original = dialog.options
            window.folder = str(self.folder / 'other')
            self.assertEqual(dialog.options, original)
            self.assertTrue(dialog.options.folder == str(self.folder))
            window.close()
            self.assertTrue(dialog._cancel.is_set())
            self.assertTrue(dialog._closed)
        finally:
            finish_window_close(window)
            window.controller.poll()
            self.app.processEvents()

    def test_async_close_cancels_a_running_playlist_request(self):
        entered = threading.Event()
        def fetch(url, options, cancel, on_batch, **kwargs):
            entered.set()
            while not cancel.is_set():
                time.sleep(.01)
            raise DownloadCancelled()
        dialog = self.dialog()
        with patch('ui.playlist_selection.load_playlist_preview', side_effect=fetch):
            dialog.load()
            self.assertTrue(entered.wait(2))
            dialog.reject()
            for worker in list(self.manager.background_threads):
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertFalse(self.manager.list_tasks())

    def test_real_cli_previews_html5_collection_without_downloading_unselected_media(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                html = '<title>Local playlist</title>' + ''.join(
                    f'<video id="clip{number}"><source src="/clip{number}.mp4" type="video/mp4" width="320" height="180"></video>'
                    for number in range(1, 4))
                data = html.encode() if self.path == '/playlist.html' else b'not requested during preview'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if self.path == '/playlist.html' else 'video/mp4')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        worker.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/playlist.html'
            rows = []
            cache = ResolvedInfoCache()
            title, warning = load_playlist_preview(url, {'proxy_mode': 'direct'}, threading.Event(), rows.extend, resolved_info=cache)
            self.assertFalse(warning)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row.available for row in rows))
            self.assertTrue(rows[1].url.endswith('/clip2.mp4'), rows)
            self.assertEqual(requests, ['/playlist.html'])
            dialog = self.dialog()
            dialog.preview_info = cache
            dialog._receive_batch(0, rows)
            dialog._receive_finished(0, title, '')
            dialog.model.select(2, 2)
            dialog.enqueue()
            self.assertEqual([task['url'] for task in self.manager.list_tasks()], [rows[1].url])
            raw = self.manager.take_resolved_info('T1')
            self.assertIsNotNone(raw)
            self.assertEqual(raw['formats'][0]['height'], 180)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
