"""Exact source pinning, lifecycle, persisted choices and actual CLI transfers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

import download_engine
from media_sources import SourcePreparer
from queue_store import QueueStore
from source_formats import format_size, selected_sources
from task_manager import TaskManager, TaskOptions
from ui.source_formats_dialog import SourceFormatsDialog
from ui.main_window import MainWindow
from qt_support import finish_window_close
from utils import DownloadCancelled


def metadata():
    return dict(id='fixture', title='Source fixture', duration=10, webpage_url='https://example.com/video', formats=[
        dict(format_id='v-low', url='https://example.com/low.mp4', ext='mp4', height=360, fps=30, vcodec='avc1', acodec='none'),
        dict(format_id='v-high', url='https://example.com/high.webm', ext='webm', height=2160, fps=60, vcodec='vp9', acodec='none', dynamic_range='HDR10'),
        dict(format_id='a-en', url='https://example.com/en.m4a', ext='m4a', vcodec='none', acodec='mp4a', abr=128, language='en'),
        dict(format_id='a-tr', url='https://example.com/tr.webm', ext='webm', vcodec='none', acodec='opus', abr=64, language='tr', audio_channels=2, asr=48000),
        dict(format_id='combined', url='https://example.com/both.mp4', ext='mp4', height=720, vcodec='avc1', acodec='mp4a'),
        dict(format_id='drm', url='https://example.com/drm.mp4', ext='mp4', height=1080, vcodec='avc1', acodec='none', has_drm=True),
        dict(format_id='storyboard', url='https://example.com/story.jpg', ext='mhtml', vcodec='none', acodec='none')])


class SourceFormatTests(unittest.TestCase):
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
        self.options = TaskOptions(folder=str(self.folder), output_mode='video', quality='360p', container='mkv')
        self.info = download_engine.YTInfo('https://example.com/video', metadata())

    def explicit(self, **kwargs):
        return replace(self.options, source_selection='manual', source_video_id='v-high', source_audio_id='a-tr', **kwargs)

    def dialog(self, options=None):
        dialog = SourceFormatsDialog(self.manager, self.info.url, options or self.options)
        self.addCleanup(dialog.reject)
        dialog._receive(0, self.info, '')
        return dialog

    def test_manual_selection_overrides_quality_and_automatic_codec_preferences(self):
        options = self.explicit(video_codec='h264', audio_codec='aac')
        streams = SourcePreparer.select_sources(self.info, asdict(options))
        self.assertEqual([s.format_id for s in streams], ['v-high', 'a-tr'])

    def test_combined_sources_and_audio_extraction_are_supported(self):
        for mode in ('audio', 'video'):
            with self.subTest(mode=mode):
                options = replace(self.options, output_mode=mode, container='auto', source_selection='manual',
                    source_video_id='combined' if mode == 'video' else '', source_audio_id='combined' if mode == 'audio' else '')
                self.assertEqual(selected_sources(self.info, asdict(options))[0].format_id, 'combined')

    def test_video_without_audio_requires_an_explicit_no_audio_output(self):
        options = replace(self.options, source_selection='manual', source_video_id='v-low')
        with self.assertRaisesRegex(RuntimeError, 'Choose an audio source'):
            selected_sources(self.info, asdict(options))
        self.assertEqual(len(selected_sources(self.info, asdict(replace(options, audio_codec='none')))), 1)

    def test_missing_drm_duplicate_and_mismatched_sources_fail_without_substitution(self):
        for video, audio in (('missing', 'a-tr'), ('drm', 'a-tr'), ('a-en', 'a-tr'), ('combined', 'a-tr'), ('v-high', 'v-low')):
            with self.subTest(video=video, audio=audio):
                task = asdict(self.explicit())
                task.update(source_video_id=video, source_audio_id=audio)
                with self.assertRaises(RuntimeError):
                    selected_sources(self.info, task)
        duplicate = metadata()
        duplicate['formats'].append(deepcopy(duplicate['formats'][1]))
        with self.assertRaises(RuntimeError):
            selected_sources(download_engine.YTInfo(self.info.url, duplicate), asdict(self.explicit()))

    def test_invalid_selector_expressions_are_not_accepted_as_ids(self):
        for value in ('best+audio', 'v[height>1]', 'a/b', 'line\nfeed', 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.options, source_selection='manual', source_video_id=value)
        with self.assertRaises(ValueError):
            replace(self.options, source_audio_id='a-en')

    def test_queue_persists_ids_without_temporary_stream_urls_and_reads_old_defaults(self):
        self.manager.add_urls([self.info.url], self.explicit())
        store = QueueStore(self.folder / 'queue.json')
        _, document = self.manager.queue_snapshot()
        store.save(document)
        loaded, warnings = store.load()
        self.assertFalse(warnings)
        options = loaded['tasks'][0]['options']
        self.assertEqual(options['source_audio_id'], 'a-tr')
        self.assertEqual(options['source_video_id'], 'v-high')
        self.assertNotIn('high.webm', store.path.read_text())
        for name in ('source_selection', 'source_video_id', 'source_audio_id'):
            options.pop(name)
        store.save(loaded)
        loaded, _ = store.load()
        self.assertEqual(loaded['tasks'][0]['options']['source_selection'], 'auto')

    def test_dialog_displays_details_filters_and_queues_frozen_per_video_choices(self):
        dialog = self.dialog()
        self.assertEqual(dialog.table.rowCount(), 7)
        self.assertIn('HDR10', [dialog.table.item(1, col).text() for col in range(dialog.table.columnCount())])
        self.assertEqual(dialog.video_combo.findData('drm'), -1)
        dialog._pick_row(5, 0)
        self.assertFalse(dialog.add_button.isEnabled())
        dialog._pick_row(0, 0)
        dialog.video_combo.setCurrentIndex(dialog.video_combo.findData('v-high'))
        dialog.audio_combo.setCurrentIndex(dialog.audio_combo.findData('a-tr'))
        dialog.filter_edit.setText('HDR10')
        self.assertFalse(dialog.table.isRowHidden(1))
        self.assertTrue(dialog.table.isRowHidden(0))
        self.assertTrue(dialog.add_button.isEnabled())
        dialog.enqueue()
        task = self.manager.get_task('T1')
        self.assertEqual(task['source_video_id'], 'v-high')
        self.assertEqual(task['source_audio_id'], 'a-tr')
        self.assertIsNotNone(self.manager.take_resolved_info('T1'))
        self.assertEqual(self.options.source_selection, 'auto')

    def test_dialog_combined_source_uses_included_audio_and_audio_output_hides_video_choice(self):
        dialog = self.dialog()
        dialog.video_combo.setCurrentIndex(dialog.video_combo.findData('combined'))
        self.assertFalse(dialog.audio_combo.isEnabled())
        self.assertEqual(dialog.chosen_options().source_audio_id, '')
        audio = self.dialog(replace(self.options, output_mode='audio', container='auto'))
        self.assertFalse(audio.video_combo.isEnabled())
        audio.audio_combo.setCurrentIndex(audio.audio_combo.findData('a-en'))
        self.assertEqual(audio.chosen_options().source_video_id, '')
        self.assertEqual(audio.chosen_options().source_audio_id, 'a-en')

    def test_empty_error_and_stale_metadata_cannot_be_queued(self):
        dialog = self.dialog()
        dialog._generation = 3
        dialog.info = None
        dialog.add_button.setEnabled(False)
        dialog._receive(2, self.info, '')
        self.assertIsNone(dialog.info)
        dialog._receive(3, None, 'fixture error')
        self.assertIn('fixture error', dialog.status.text())
        self.assertFalse(dialog.add_button.isEnabled())
        empty = metadata()
        empty['formats'] = []
        dialog._receive(3, download_engine.YTInfo(self.info.url, empty), '')
        self.assertFalse(dialog.add_button.isEnabled())

    def test_async_inspection_uses_frozen_cookies_proxy_and_cancels_on_close(self):
        options = replace(self.options, cookie_mode='browser', cookie_browser='firefox', proxy_mode='direct')
        entered = threading.Event()
        def fetch(url, **kwargs):
            self.assertEqual(kwargs['cookie_options']['cookie_browser'], 'firefox')
            self.assertEqual(kwargs['network_options']['proxy_mode'], 'direct')
            entered.set()
            kwargs['cancel_event'].wait(3)
            raise DownloadCancelled()
        dialog = SourceFormatsDialog(self.manager, self.info.url, options)
        with patch('ui.source_formats_dialog.fetch_ytinfo_with_retry', side_effect=fetch):
            dialog.load()
            self.assertTrue(entered.wait(2))
            dialog.reject()
            for worker in list(self.manager.background_threads):
                worker.join(2)
                self.assertFalse(worker.is_alive())
        self.assertFalse(self.manager.list_tasks())

    def test_main_window_requires_a_url_and_freezes_the_current_output_profile(self):
        with patch.object(self.manager, 'set_worker_count'):
            window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        try:
            with patch.object(window, '_show_message') as message:
                window.open_source_formats()
            message.assert_called_once()
            window.folder = str(self.folder)
            window.url_edit.setText(self.info.url)
            with patch.object(SourceFormatsDialog, 'load'):
                window.open_source_formats()
            dialog = window.source_dialog
            dialog._receive(0, self.info, '')
            original_container = dialog.options.container
            window.output_controls.container_combo.setCurrentIndex(window.output_controls.container_combo.findData('mkv'))
            self.assertEqual(dialog.options.container, original_container)
        finally:
            finish_window_close(window)
            window.controller.poll()
            self.app.processEvents()

    def test_unavailable_cached_manual_format_never_falls_back_to_url_extraction(self):
        tid = self.manager.add_urls([self.info.url], self.explicit())[0]
        ydl = Mock()
        with self.assertRaisesRegex(RuntimeError, 'selected source format is unavailable'):
            self.manager.engine._download_from_info(ydl, self.info.url, self.info._media_info,
                threading.Event(), format_id='missing', tid=tid)
        ydl.extract_info.assert_not_called()
        ydl.download.assert_not_called()

    def test_size_distinguishes_exact_estimated_and_unknown(self):
        self.assertEqual(format_size({'filesize': 1024 ** 2}), '1.00 MiB')
        self.assertTrue(format_size({'filesize_approx': 1024 ** 2}).startswith('≈'))
        self.assertTrue(format_size({'tbr': 128}, 10).startswith('≈'))
        self.assertEqual(format_size({'tbr': float('nan')}, 10), '—')

    def test_html5_unknown_codecs_are_selectable_and_verified_after_downloading(self):
        raw = metadata()
        raw['formats'] = [dict(format_id='0', url='https://example.com/sample.mp4', ext='mp4', vcodec=None)]
        info = download_engine.YTInfo(self.info.url, raw)
        dialog = SourceFormatsDialog(self.manager, self.info.url, self.options)
        self.addCleanup(dialog.reject)
        dialog._receive(0, info, '')
        self.assertTrue(dialog.add_button.isEnabled())
        self.assertEqual(dialog.chosen_options().source_video_id, '0')
        options = replace(self.options, output_mode='audio', container='auto', source_selection='manual', source_audio_id='0')
        self.assertEqual(selected_sources(info, asdict(options))[0].format_id, '0')

    def test_real_cli_downloads_only_the_exact_id_even_if_id_is_a_selector_keyword(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                data = b'EXACT SOURCE' * 100 if self.path == '/chosen.mp4' else b'WRONG SOURCE' * 100
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            cases = [('best', video, audio) for video, audio in (('avc1', 'mp4a'), ('avc1', 'none'),
                ('none', 'mp4a'), (None, None), (None, 'none'), ('none', None))]
            cases += [('0', None, None), ('137', 'avc1', 'none'), ('140', 'none', 'mp4a')]
            for index, (format_id, video_codec, audio_codec) in enumerate(cases):
                with self.subTest(id=format_id, video=video_codec, audio=audio_codec):
                    raw = dict(id='local', title='Exact', formats=[
                        dict(format_id=format_id, url=base + '/chosen.mp4', ext='mp4', protocol='http', vcodec=video_codec, acodec=audio_codec, height=360),
                        dict(format_id='other', url=base + '/wrong.mp4', ext='mp4', protocol='http', vcodec='avc1', acodec='mp4a', height=2160)])
                    info = download_engine.YTInfo(base + f'/watch{index}', raw)
                    options = replace(self.options, proxy_mode='direct', source_selection='manual', source_video_id=format_id, audio_codec='none')
                    tid = self.manager.add_urls([info.url], options)[0]
                    self.manager.tid_to_workerid[tid] = 1
                    path = self.manager.engine._download_format(tid, 'video', info.url, info.formats[0], f'chosen{index}.mp4', folder=str(self.folder))
                    self.assertEqual(Path(path).read_bytes(), b'EXACT SOURCE' * 100)
            self.assertTrue(requests)
            self.assertEqual(set(requests), {'/chosen.mp4'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
