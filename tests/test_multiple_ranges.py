"""Real multi-cut output, order, validation, persistence and failure isolation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import asdict
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import download_engine
from errors import MediaOperationError
from media_processing import ConfiguredOutput
from media_sources import SourcePreparer
from range_options import parse_ranges
from queue_store import QueueStore
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions
from ui.i18n import set_language
from ui.output_controls import OutputControls
from utils import DownloadCancelled, NO_WINDOW, which_executable


class MultipleRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixtures.name)
        for args, name in [
            (['-f', 'lavfi', '-i', 'sine=duration=4', '-c:a', 'pcm_s16le'], 'audio.wav'),
            (['-f', 'lavfi', '-i', 'testsrc2=s=128x96:r=10:d=4', '-c:v', 'libx264',
              '-threads', '2', '-g', '10', '-movflags', '+faststart'], 'video.mp4')]:
            subprocess.run([which_executable(['ffmpeg']), '-hide_banner', '-loglevel', 'error', '-y', *args,
                str(cls.root / name)], check=True, timeout=20, **NO_WINDOW)

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
        self.info = download_engine.YTInfo('https://example.com/ranges', {
            'id': 'ranges', 'url': 'https://example.com/media.wav', 'title': 'Ranges', 'duration': 4,
            'chapters': [{'title': 'First', 'start_time': 0, 'end_time': 2},
                         {'title': 'Second', 'start_time': 2, 'end_time': 4}]})

    def prepare(self, fixture='audio.wav', **choices):
        options = TaskOptions(folder=str(self.folder), output_mode=choices.pop('output_mode', 'audio'),
                              trim_ranges=choices.pop('trim_ranges', '2 - 3\n0.5 - 1.5'), **choices)
        tid = self.manager.add_urls(['https://example.com/ranges/' + str(len(self.manager.list_tasks()))], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        private = self.folder / ('private-' + tid)
        private.mkdir()
        source = private / fixture
        shutil.copyfile(self.root / fixture, source)
        sources = SourcePreparer(self.engine).inspect_sources([source], 4)
        return tid, options, private, sources

    def test_full_download_once_order_names_and_queue_roundtrip(self):
        tid, options, _, sources = self.prepare(audio_codec='mp3', filename_template='Clips', subfolder_template='Edited')
        with patch('download_engine.fetch_ytinfo_with_retry', return_value=self.info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources) as downloaded:
            self.manager._process_task((tid, 'offline'), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task['status'].startswith('Completed'), task)
        self.assertIn('2 clips', task['status'])
        output = Path(task['output_path'])
        self.assertEqual(output, self.folder / 'Edited/Clips')
        files = sorted(output.glob('*.mp3'))
        self.assertEqual([p.name for p in files], ['001 - 2.000-3.000.mp3', '002 - 0.500-1.500.mp3'])
        self.assertTrue(all(abs(float(self.engine._ffprobe_media(str(p))['format']['duration']) - 1) < .1 for p in files))
        self.assertEqual(downloaded.call_count, 1)
        self.assertEqual(downloaded.call_args.args[4]['trim_start'], '')
        store = QueueStore(self.folder / 'queue.json')
        store.save(self.manager.queue_snapshot()[1])
        document, warnings = store.load()
        self.assertFalse(warnings)
        restored = TaskManager()
        self.addCleanup(restored.close)
        restored.restore_queue(document)
        self.assertEqual(restored.get_task(tid)['trim_ranges'], options.trim_ranges)
        self.assertEqual(restored.get_task(tid)['output_path'], str(output))

    def test_chapter_embedding_rebases_each_range(self):
        tid, _, _, sources = self.prepare(audio_codec='aac', container='m4a',
            trim_ranges='0.5 - 1.5\n2.5 - 3.5', chapter_mode='embed')
        with patch('download_engine.fetch_ytinfo_with_retry', return_value=self.info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources):
            self.manager._process_task((tid, 'offline'), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task['status'].startswith('Completed'), task)
        outputs = sorted(Path(task['output_path']).glob('*.m4a'))
        self.assertEqual(len(outputs), 2)
        for output, title in zip(outputs, ['First', 'Second']):
            chapters = self.engine._ffprobe_media(str(output))['chapters']
            self.assertEqual([c['tags']['title'] for c in chapters], [title])
            self.assertAlmostEqual(float(chapters[0]['start_time']), 0, delta=.02)
        self.assertFalse(task.get('warnings'), task.get('warnings'))

    def test_copy_alignment_and_conversion_offsets(self):
        for codec in ['original', 'vp9']:
            tid, options, private, sources = self.prepare('video.mp4', output_mode='video',
                video_codec=codec, audio_codec='none', container='mkv', trim_ranges='0.5 - 1.5\n2.5 - 3.5')
            configured = ConfiguredOutput(self.engine)
            with patch.object(configured.sources, 'prepare', return_value=sources):
                batch = configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Video')
            self.assertEqual(len(batch.parts), 2)
            if codec == 'original':
                self.assertLess(batch.parts[0].start, .5)
                self.assertTrue(self.manager.get_task(tid).get('warnings'))
            else:
                self.assertEqual([p.start for p in batch.parts], [.5, 2.5])
                self.assertTrue(all(abs(float(self.engine._ffprobe_media(p.path)['format']['duration']) - 1) < .15 for p in batch.parts))

    def test_failure_never_falls_back_or_publishes_partial_group(self):
        tid, _, _, sources = self.prepare(audio_codec='mp3')
        original = self.engine._run_ffmpeg
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise MediaOperationError('second range failed')
            return original(*args, **kwargs)
        with patch('download_engine.fetch_ytinfo_with_retry', return_value=self.info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources), \
                patch.object(self.engine, '_run_ffmpeg', side_effect=fail_second):
            self.manager._process_task((tid, 'offline'), 1)
        self.assertEqual(count, 2)
        self.assertFalse(self.manager.get_task(tid)['output_path'])
        self.assertFalse((self.folder / 'Ranges').exists())
        self.assertFalse(list(self.folder.glob('.yd-*')))

    def test_cancel_prevents_second_section_download(self):
        tid, options, private, sources = self.prepare(trim_download_mode='section')
        configured = ConfiguredOutput(self.engine)
        with patch.object(configured.sources, 'prepare', return_value=sources) as download, \
                patch.object(self.engine, '_run_ffmpeg', side_effect=DownloadCancelled):
            with self.assertRaises(DownloadCancelled):
                configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Cancelled')
        self.assertEqual(download.call_count, 1)

    def test_invalid_ranges_rejected_before_download(self):
        for value in ['-1 - 3', '1 - 1', 'nan - 3', '0:60 - 2:00', '1 -', '1e2 - 150', '0 - 1\n' * 101]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                TaskOptions(folder=str(self.folder), output_mode='audio', trim_ranges=value)
        for extra in [dict(trim_start='1'), dict(chapter_mode='split')]:
            with self.assertRaises(ValueError):
                TaskOptions(folder=str(self.folder), output_mode='audio', trim_ranges='1 - 2', **extra)
        tid, options, private, _ = self.prepare(trim_ranges='5 - 6')
        configured = ConfiguredOutput(self.engine)
        with patch.object(configured.sources, 'prepare') as download:
            with self.assertRaises(MediaOperationError):
                configured.process(tid, 1, 'offline', self.info, asdict(options), str(private), 'Bad')
        download.assert_not_called()
        self.assertEqual(parse_ranges('1:02:03.5 - 1:03:00; 0 - 1'), ((3723.5, 3780), (0, 1)))

    def test_controls_translation_preferences_and_old_defaults(self):
        controls = OutputControls()
        self.addCleanup(controls.deleteLater)
        controls.trim_start_edit.setText('1')
        controls.trim_end_edit.setText('3')
        controls.ranges.editor.setPlainText('0 - 1\n2 - 3')
        self.assertFalse(controls.trim_start_edit.isEnabled())
        self.assertEqual(controls.trim_values(), {'trim_start': '', 'trim_end': ''})
        options = TaskOptions(folder=str(self.folder), **controls.values(), **controls.trim_values())
        set_language('tr')
        self.addCleanup(set_language, 'en')
        self.assertEqual(controls.ranges.title(), 'Birden fazla zaman aralığı')
        self.assertEqual(controls.values()['trim_ranges'], options.trim_ranges)
        controls.ranges.editor.clear()
        self.assertTrue(controls.trim_start_edit.isEnabled())
        self.assertEqual(controls.trim_values(), {'trim_start': '1', 'trim_end': '3'})
        controls.restore({**DEFAULTS, 'trim_ranges': options.trim_ranges})
        self.assertEqual(controls.values()['trim_ranges'], options.trim_ranges)
        settings, warnings = validate_settings({'trim_ranges': 'bad'})
        self.assertEqual(settings['trim_ranges'], '')
        self.assertIn('trim_ranges', warnings)
        self.assertEqual(TaskOptions(folder=str(self.folder)).trim_ranges, '')
