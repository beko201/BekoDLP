"""Metadata naming, original playlist positions and atomic final publication."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import download_engine
from naming_templates import ensure_naming_folder, render_naming, validate_naming_options
from queue_store import QueueStore
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions, PlaylistFetcher
from ui.i18n import set_language
from ui.main_window import MainWindow
from qt_support import finish_window_close
from ui.naming_controls import NamingControls
from ui.playlist_selection import PlaylistSelectionDialog
from playlist_selection import preview_entry


class NamingTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = TaskOptions(folder=str(self.root), output_mode='video',
            filename_template='{playlist_index:03d} - {upload_date} - {title}', subfolder_template='{channel}/{playlist}')
        self.manager = TaskManager()
        self.addCleanup(self.manager.close)
        self.metadata = {'id': 'abc123', 'title': 'Ornek video', 'channel': 'Ornek kanal', 'upload_date': '20261122'}

    def test_render_title_channel_date_playlist_index_and_extension_independence(self):
        data = asdict(replace(self.options, playlist_title='My playlist', playlist_index='7'))
        folders, name = render_naming(data, self.metadata)
        self.assertEqual(folders, ('Ornek kanal', 'My playlist'))
        self.assertEqual(name, '007 - 2026-11-22 - Ornek video')
        self.assertEqual(render_naming({'filename_template': '{id} - {title}'}, self.metadata)[1], 'abc123 - Ornek video')
        self.assertEqual(render_naming({}, {'title': 'Türkçe video'})[1], 'Turkce video')

    def test_missing_metadata_has_stable_fallbacks_without_using_today(self):
        folders, name = render_naming(asdict(self.options), {'title': 'Video', 'upload_date': '20230230'})
        self.assertEqual(folders, ('Unknown channel', 'Single videos'))
        self.assertEqual(name, '000 - Unknown date - Video')
        self.assertIn('Unknown date', render_naming(asdict(self.options), {'upload_date': '2026112'})[1])

    def test_traversal_absolute_paths_field_access_and_unsupported_formats_are_rejected(self):
        cases = [{'subfolder_template': value} for value in ('../outside', '/root', r'C:\outside', r'\\server\share',
                  'one//two', 'one/./two', 'a/b/c/d/e/f')]
        cases += [{'filename_template': value} for value in ('', '..', 'a/b', r'a\b', '{title.__class__}',
                  '{title[0]}', '{title!r}', '{ext}', '{playlist_index:010d}', '{title', '{upload_date:%Y}')]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_naming_options(value)

    def test_metadata_cannot_inject_directories_or_reserved_windows_names(self):
        folders, name = render_naming({'subfolder_template': '{channel}', 'filename_template': '{title}'},
            {'channel': '../../outside', 'title': 'CON'})
        self.assertEqual(name, '_CON')
        self.assertNotIn('/', folders[0])
        self.assertNotIn('\\', folders[0])
        target = Path(ensure_naming_folder(self.root, folders))
        self.assertTrue(target.resolve().is_relative_to(self.root.resolve()))

    def test_long_paths_are_bounded_and_existing_files_or_links_are_not_followed(self):
        folders, name = render_naming(asdict(self.options), {**self.metadata, 'title': 'V' * 500})
        self.assertLessEqual(len(str(self.root)) + sum(len(value) + 1 for value in folders) + len(name) + 32, 240)
        (self.root / 'file').write_text('keep')
        with self.assertRaises(FileExistsError):
            ensure_naming_folder(self.root, ('file',))
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(RuntimeError):
                ensure_naming_folder(self.root, ('linked',))
        self.assertEqual((self.root / 'file').read_text(), 'keep')

    def test_worker_uses_metadata_templates_and_cleans_its_original_workspace(self):
        options = replace(self.options, playlist_title='Playlist', playlist_index='3')
        tid = self.manager.add_urls(['https://example.com/video'], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        info = download_engine.YTInfo('https://example.com/video', self.metadata)
        def process(instance, tid, worker, url, info, task, folder, title):
            path = Path(folder, 'temporary.mkv')
            path.write_bytes(b'completed media')
            return str(path)
        with patch('download_engine.fetch_ytinfo_with_retry', return_value=info), \
                patch('media_processing.ConfiguredOutput.process', process):
            self.manager.engine.process_task((tid, info.url), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        output = self.root / 'Ornek kanal/Playlist/003 - 2026-11-22 - Ornek video.mkv'
        self.assertEqual(Path(task['output_path']), output)
        self.assertEqual(output.read_bytes(), b'completed media')
        self.assertFalse(list(self.root.glob('.yd-*')))
        self.assertFalse(self.manager.engine._output_naming)

    def test_parallel_same_names_and_existing_files_are_preserved(self):
        folder = self.root / 'Channel'
        folder.mkdir()
        (folder / 'Same.mp4').write_bytes(b'original')
        tids = self.manager.add_urls(['https://example.com/one', 'https://example.com/two'], self.options)
        sources = []
        for index, tid in enumerate(tids):
            source = self.root / f'private{index}.mp4'
            source.write_bytes(str(index).encode())
            sources.append(source)
            self.manager.engine._output_naming[tid] = (('Channel',), 'Same')
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(lambda pair: self.manager.engine._publish_output(pair[0], str(pair[1]), str(self.root)), zip(tids, sources)))
        self.assertEqual(len(set(outputs)), 2)
        self.assertEqual((folder / 'Same.mp4').read_bytes(), b'original')
        self.assertEqual({Path(path).read_bytes() for path in outputs}, {b'0', b'1'})

    def test_subtitle_bundle_uses_the_same_custom_base_and_collision_suffix(self):
        options = replace(self.options, subtitle_mode='files')
        tid = self.manager.add_urls(['https://example.com/subs'], options)[0]
        private = self.root / 'private'
        private.mkdir()
        media, caption = private / 'temporary.mp4', private / 'temporary.en.srt'
        media.write_bytes(b'media')
        caption.write_text('caption')
        folder = self.root / 'Channel'
        folder.mkdir()
        (folder / 'Custom.en.srt').write_text('foreign caption')
        self.manager.engine._output_naming[tid] = (('Channel',), 'Custom')
        @contextmanager
        def prepare(*args, **kwargs):
            yield [('en', caption)]
        with patch('subtitle_processing.SubtitleProcessor.prepare', prepare):
            path = Path(self.manager.engine._publish_output(tid, str(media), str(self.root)))
        self.assertTrue(path.name.startswith('Custom'))
        self.assertEqual(path.with_suffix('.en.srt').read_text(), 'caption')
        self.assertEqual((folder / 'Custom.en.srt').read_text(), 'foreign caption')
        self.assertEqual(path.read_bytes(), b'media')

    def test_playlist_picker_retains_original_position_in_saved_task(self):
        dialog = PlaylistSelectionDialog(self.manager, 'https://example.com/list', self.options)
        self.addCleanup(dialog.reject)
        rows = [preview_entry({'playlist_index': position, 'title': f'Video {position}',
                               'url': f'https://example.com/{position}'}, index + 1, dialog.url)
                for index, position in enumerate((2, 7, 12))]
        dialog._receive_batch(0, rows)
        dialog._receive_finished(0, 'Playlist name', '')
        dialog.model.select(7, 12)
        dialog.enqueue()
        tasks = self.manager.list_tasks()
        self.assertEqual([task['playlist_index'] for task in tasks], ['7', '12'])
        self.assertEqual([task['playlist_title'] for task in tasks], ['Playlist name'] * 2)
        _, document = self.manager.queue_snapshot()
        store = QueueStore(self.root / 'queue.json')
        store.save(document)
        loaded, warnings = store.load()
        self.assertFalse(warnings)
        self.assertEqual(loaded['tasks'][1]['options']['playlist_index'], '12')
        self.assertEqual(loaded['tasks'][1]['options']['filename_template'], self.options.filename_template)
        for row in loaded['tasks']:
            for key in ('playlist_title', 'playlist_index', 'filename_template', 'subfolder_template'):
                row['options'].pop(key)
        store.save(loaded)
        loaded, _ = store.load()
        self.assertEqual(loaded['tasks'][0]['options']['filename_template'], '{title}')

    def test_bulk_streaming_captures_indices_before_entries_become_downloads(self):
        def extract(url, **kwargs):
            self.assertEqual(url, 'https://www.youtube.com/playlist?list=PLtest')
            kwargs['on_entries']([{'url': f'https://example.com/{n}', 'playlist_title': 'Bulk playlist', 'playlist_index': n}
                                  for n in range(1, 104)])
            return {'_type': 'playlist'}, []
        with patch('download_engine._flat_extract', side_effect=extract):
            PlaylistFetcher(self.manager, 'https://youtube.com/watch?v=chosen&list=PLtest', self.options, True).run()
        tasks = self.manager.list_tasks()
        self.assertEqual(len(tasks), 103)
        self.assertEqual(tasks[99]['playlist_index'], '100')
        self.assertEqual(tasks[100]['playlist_index'], '101')
        self.assertEqual(tasks[102]['playlist_title'], 'Bulk playlist')

    def test_presets_preview_settings_and_task_options_are_independent(self):
        controls = NamingControls()
        controls.preset.setCurrentIndex(3)
        self.assertEqual(controls.values()['subfolder_template'], '{playlist}')
        self.assertIn('003', controls.preview.text())
        frozen = TaskOptions(folder=str(self.root), **controls.values())
        controls.filename.setText('{channel} - {title}')
        self.assertEqual(frozen.filename_template, '{playlist_index:03d} - {title}')
        self.assertEqual(controls.preset.currentData(), -1)
        settings, warnings = validate_settings(controls.values())
        self.assertFalse(warnings)
        controls.restore(settings)
        self.assertEqual(controls.filename.text(), '{channel} - {title}')
        settings, warnings = validate_settings({'filename_template': '{evil.field}'})
        self.assertEqual(settings['filename_template'], '{title}')
        self.assertTrue(warnings)
        try:
            set_language('tr')
            self.assertIn('{title}', controls.hint.text())
            self.assertIn('Örnek', controls.preview.text())
        finally:
            set_language('en')

    def test_invalid_ui_template_is_reported_before_submission(self):
        with patch.object(self.manager, 'set_worker_count'):
            window = MainWindow(self.manager, settings_path=self.root / 'settings.json')
        try:
            window.folder = str(self.root)
            window.output_controls.naming.filename.setText('{unknown}')
            window.url_edit.setText('https://example.com/video')
            with patch.object(self.manager, 'submit_link') as submit, patch.object(window, '_show_message') as show:
                window.enqueue()
            submit.assert_not_called()
            self.assertEqual(show.call_args.args[1], 'File and folder naming')
        finally:
            finish_window_close(window)
            window.controller.poll()
            self.app.processEvents()
