"""Real chapter remuxing, split execution, queue restore and publication rollback."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import asdict, replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import download_engine
from chapter_options import ChapterBatch, chapter_ranges
from chapter_processing import ChapterProcessor, publish_chapter_folder
from errors import MediaOperationError
from media_processing import ConfiguredOutput
from media_sources import SourcePreparer
from queue_store import QueueStore
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions
from ui.output_controls import OutputControls
from ui.file_actions import reveal_file
from utils import DownloadCancelled, NO_WINDOW, which_executable


class ChapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixtures.name)
        cls.ffmpeg = which_executable(['ffmpeg'])
        def make(args, name):
            subprocess.run([cls.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', *args, str(cls.root / name)],
                check=True, timeout=20, **NO_WINDOW)
        for ext, codec in [('mp3', 'libmp3lame'), ('m4a', 'aac'), ('wav', 'pcm_s16le')]:
            make(['-f', 'lavfi', '-i', 'sine=duration=4', '-c:a', codec], 'audio.' + ext)
        make(['-f', 'lavfi', '-i', 'testsrc2=s=128x96:r=10:d=4', '-c:v', 'libx264',
              '-threads', '2', '-g', '10', '-movflags', '+faststart'], 'video.mp4')
        make(['-i', str(cls.root / 'video.mp4'), '-c', 'copy'], 'video.mkv')

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager()
        self.engine = self.manager.engine
        self.addCleanup(self.manager.close)
        self.info = download_engine.YTInfo('https://example.com/chapters', {
            'title': 'Chapter fixture', 'id': 'chapter-test', 'duration': 4, 'url': 'https://example.com/media.wav',
            'chapters': [{'start_time': 0, 'end_time': 2, 'title': 'First = intro; #1'},
                         {'start_time': 2, 'end_time': 4, 'title': 'Second / outro'}]})

    def prepare(self, fixture='audio.wav', **choices):
        options = TaskOptions(folder=str(self.folder), output_mode=choices.pop('output_mode', 'audio'), **choices)
        tid = self.manager.add_urls(['https://example.com/chapters/' + str(len(self.manager.list_tasks()))], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        private = self.folder / ('private-' + tid)
        private.mkdir()
        source = private / fixture
        shutil.copyfile(self.root / fixture, source)
        sources = SourcePreparer(self.engine).inspect_sources([source], 4)
        return tid, options, private, source, sources

    def probe(self, path):
        return self.engine._ffprobe_media(str(path), fresh=True)

    def test_embed_supported_formats_preserves_streams_and_source_titles(self):
        for fixture in ['audio.mp3', 'audio.m4a', 'video.mp4', 'video.mkv']:
            with self.subTest(fixture=fixture):
                tid, _, _, source, _ = self.prepare(fixture, chapter_mode='embed')
                before = self.probe(source)
                ChapterProcessor(self.engine).embed(tid, str(source), self.info._media_info)
                probe = self.probe(source)
                self.assertFalse(self.manager.get_task(tid).get('warnings'), self.manager.get_task(tid).get('warnings'))
                self.assertEqual([c['tags']['title'] for c in probe['chapters']], ['First = intro; #1', 'Second / outro'])
                self.assertAlmostEqual(float(probe['chapters'][1]['start_time']), 2, delta=.05)
                self.assertEqual([(s['codec_type'], s['codec_name']) for s in before['streams']],
                    [(s['codec_type'], s['codec_name']) for s in probe['streams'] if s['codec_type'] != 'data'])

    def test_trimmed_embedding_rebases_clipped_chapters(self):
        tid, options, private, _, sources = self.prepare('audio.m4a', chapter_mode='embed', trim_start='1', trim_end='3')
        configured = ConfiguredOutput(self.engine)
        with patch.object(configured.sources, 'prepare', return_value=sources):
            output = configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Trimmed')
        self.engine._subtitle_context[tid]['metadata'] = self.info._media_info
        published = self.engine._publish_output(tid, output, str(self.folder))
        chapters = self.probe(published)['chapters']
        self.assertEqual(len(chapters), 2)
        self.assertAlmostEqual(float(chapters[0]['start_time']), 0, delta=.03)
        self.assertAlmostEqual(float(chapters[1]['start_time']), 1, delta=.03)
        self.assertAlmostEqual(float(chapters[1]['end_time']), 2, delta=.05)

    def test_engine_split_downloads_once_and_publishes_numbered_group_with_naming(self):
        tid, options, _, _, sources = self.prepare(chapter_mode='split', audio_codec='mp3',
            trim_start='1', trim_end='3', filename_template='Album', subfolder_template='Music')
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources) as download:
            self.manager._process_task((tid, 'https://example.com/chapters'), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task['status'].startswith('Completed'), task)
        folder = Path(task['output_path'])
        self.assertEqual(folder, self.folder / 'Music/Album')
        outputs = sorted(folder.glob('*.mp3'))
        self.assertEqual(len(outputs), 2)
        self.assertTrue(outputs[0].name.startswith('001 - '))
        self.assertTrue(outputs[1].name.startswith('002 - '))
        self.assertTrue(all(abs(float(self.probe(path)['format']['duration']) - 1) < .15 for path in outputs))
        self.assertTrue(all(not self.probe(path).get('chapters') for path in outputs))
        self.assertEqual(download.call_count, 1)
        store = QueueStore(self.folder / 'queue.json')
        store.save(self.manager.queue_snapshot()[1])
        restored, warnings = store.load()
        self.assertFalse(warnings)
        self.assertEqual(restored['tasks'][0]['options']['chapter_mode'], 'split')
        reopened = TaskManager()
        self.addCleanup(reopened.close)
        reopened.restore_queue(restored)
        self.assertEqual(reopened.get_task(tid)['output_path'], str(folder))
        self.assertTrue(reopened.get_task(tid)['status'].startswith('Completed'))
        with patch('ui.file_actions.open_folder') as opened:
            reveal_file(str(folder))
        opened.assert_called_once_with(str(folder))

    def test_split_video_copy_warns_for_keyframe_and_encoding_is_exact(self):
        self.info._media_info.info['chapters'][1]['start_time'] = 2.3
        self.info._media_info.info['chapters'][0]['end_time'] = 2.3
        for codec in ['original', 'vp9']:
            with self.subTest(codec=codec):
                tid, options, private, _, sources = self.prepare('video.mp4', output_mode='video',
                    chapter_mode='split', video_codec=codec, audio_codec='none', container='mkv')
                configured = ConfiguredOutput(self.engine)
                with patch.object(configured.sources, 'prepare', return_value=sources):
                    batch = configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Title')
                self.assertIsInstance(batch, ChapterBatch)
                self.assertEqual(len(batch.parts), 2)
                if codec == 'original':
                    self.assertLess(batch.parts[1].start, 2.3)
                    self.assertTrue(self.manager.get_task(tid).get('warnings'))
                else:
                    self.assertEqual(batch.parts[1].start, 2.3)
                    self.assertAlmostEqual(float(self.probe(batch.parts[1].path)['format']['duration']), 1.7, delta=.15)

    def test_section_origin_is_applied_to_split_ranges(self):
        tid, options, private, _, sources = self.prepare(chapter_mode='split', trim_start='11', trim_end='13')
        sources = replace(sources, section_downloaded=True, timeline_origin=10)
        self.info._media_info.info['chapters'] = [
            {'start_time': 10, 'end_time': 12, 'title': 'First'},
            {'start_time': 12, 'end_time': 14, 'title': 'Second'}]
        configured = ConfiguredOutput(self.engine)
        with patch.object(configured.sources, 'prepare', return_value=sources):
            batch = configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Title')
        self.assertEqual([part.start for part in batch.parts], [11, 12])
        self.assertTrue(all(abs(float(self.probe(part.path)['format']['duration']) - 1) < .05 for part in batch.parts))

    def test_missing_chapters_keeps_normal_output_and_embed_failure_preserves_original(self):
        self.info._media_info.info.pop('chapters')
        tid, options, private, source, sources = self.prepare(chapter_mode='split')
        configured = ConfiguredOutput(self.engine)
        with patch.object(configured.sources, 'prepare', return_value=sources):
            result = configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Normal')
        self.assertTrue(Path(result).is_file())
        self.assertTrue(self.manager.get_task(tid).get('warnings'))
        tid, _, _, source, _ = self.prepare('audio.m4a', chapter_mode='embed')
        self.info._media_info.info['chapters'] = [{'start_time': 0, 'end_time': 4, 'title': 'Only'}]
        original = source.read_bytes()
        with patch.object(self.engine, '_run_ffmpeg', side_effect=RuntimeError('broken remux')):
            ChapterProcessor(self.engine).embed(tid, str(source), self.info._media_info)
        self.assertEqual(source.read_bytes(), original)
        self.manager.cancel_events[tid].set()
        with self.assertRaises(DownloadCancelled):
            ChapterProcessor(self.engine).embed(tid, str(source), self.info._media_info)

    def test_failed_second_chapter_leaves_no_published_group(self):
        tid, _, _, _, sources = self.prepare(chapter_mode='split', audio_codec='mp3')
        original = self.engine._run_ffmpeg
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise MediaOperationError('second chapter failed')
            return original(*args, **kwargs)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources), \
                patch.object(self.engine, '_run_ffmpeg', side_effect=fail_second):
            self.manager._process_task((tid, 'offline'), 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.folder / 'Chapter fixture').exists())
        self.assertFalse(self.manager.get_task(tid)['output_path'])
        self.assertFalse(list(self.folder.glob('.yd-*')))

    def test_group_collision_and_cancel_rollback_keep_existing_files(self):
        tid, _, private, source, _ = self.prepare()
        stage = private / 'stage'
        stage.mkdir()
        (stage / 'one.mp3').write_bytes(b'one')
        (stage / 'two.mp3').write_bytes(b'two')
        existing = self.folder / 'Album'
        existing.mkdir()
        (existing / 'existing').write_bytes(b'keep')
        published = publish_chapter_folder(self.engine, tid, str(stage), str(self.folder), 'Album')
        self.assertNotEqual(Path(published), existing)
        self.assertEqual((existing / 'existing').read_bytes(), b'keep')
        original = os.link
        def cancel_after_link(*args):
            original(*args)
            self.manager.cancel_events[tid].set()
        with patch('chapter_processing.os.link', side_effect=cancel_after_link):
            with self.assertRaises(DownloadCancelled):
                publish_chapter_folder(self.engine, tid, str(stage), str(self.folder), 'Cancelled')
        self.assertFalse(list(self.folder.glob('Cancelled*')))

    def test_controls_preferences_and_old_queue_defaults(self):
        controls = OutputControls()
        self.assertEqual(controls.values()['chapter_mode'], 'none')
        controls.restore({**DEFAULTS, 'chapter_mode': 'split'})
        self.assertEqual(controls.values()['chapter_mode'], 'split')
        settings, warnings = validate_settings({'chapter_mode': 'invalid'})
        self.assertEqual(settings['chapter_mode'], 'none')
        self.assertTrue(warnings)
        self.prepare(chapter_mode='embed')
        document = self.manager.queue_snapshot()[1]
        document['tasks'][0]['options'].pop('chapter_mode')
        store = QueueStore(self.folder / 'queue.json')
        store.save(document)
        restored, _ = store.load()
        self.assertEqual(restored['tasks'][0]['options']['chapter_mode'], 'none')
        with self.assertRaises(ValueError):
            TaskOptions(folder=str(self.folder), chapter_mode='unknown')
        controls.deleteLater()

    def test_ranges_reject_invalid_values_infer_ends_and_clip(self):
        chapters = chapter_ranges({'chapters': [None, {'start_time': float('nan')},
            {'start_time': 0, 'title': 'First'}, {'start_time': 2, 'end_time': None},
            {'start_time': 2, 'title': 'Duplicate'}, {'start_time': 4, 'end_time': 5}]}, 1, 3)
        self.assertEqual([(c.number, c.start, c.end) for c in chapters], [(3, 1, 2), (4, 2, 3)])
