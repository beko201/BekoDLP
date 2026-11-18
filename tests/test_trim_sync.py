"""Real long-GOP cuts must not introduce silent preroll or A/V marker drift."""
from array import array
from dataclasses import asdict
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from errors import MediaOperationError
from media_processing import ConfiguredOutput
from task_manager import TaskManager, TaskOptions
from utils import DownloadCancelled, NO_WINDOW


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
class TrimSyncTests(unittest.TestCase):
    @staticmethod
    def run_media(command):
        return subprocess.run(command, check=True, capture_output=True, **NO_WINDOW).stdout

    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixtures.name)
        cls.ffmpeg, cls.ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        cls.video = cls.root / 'long-gop.webm'
        cls.run_media([cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                       "color=black:s=128x96:r=25:d=12,drawbox=color=white:t=fill:enable='between(t,4,4.2)'",
                       '-c:v', 'libvpx-vp9', '-cpu-used', '8', '-threads', '2', '-g', '250', str(cls.video)])
        cls.audio = {}
        for extension, encoder, extra in (('webm', 'libopus', ['-cluster_time_limit', '1000']), ('m4a', 'aac', [])):
            path = cls.root / ('audio.' + extension)
            cls.run_media([cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                           "aevalsrc='(0.1+0.4*between(t,4,4.2))*sin(2*PI*440*t)':s=48000:d=12",
                           '-c:a', encoder, *extra, str(path)])
            cls.audio[extension] = path

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        log_patch = patch('utils.ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.manager = TaskManager()
        self.engine = self.manager.engine
        self.engine.configure_processing(1, 2)
        self.addCleanup(self.manager.close)

    def cut(self, extension='webm', **changes):
        options = TaskOptions(folder=str(self.folder), output_mode='video', trim_start='3.3', trim_end='8.3', **changes)
        tid = self.manager.add_urls([f'https://example.com/{self.manager.task_counter}'], options)[0]
        private = self.folder / tid
        private.mkdir()
        output = ConfiguredOutput(self.engine)
        sources = output.sources.inspect_sources((self.video, self.audio[extension]), 12)
        output.sources.prepare = lambda *args: sources
        path = output.process(tid, 1, 'offline', SimpleNamespace(duration=12), asdict(options), str(private), 'clip')
        return Path(path), self.manager.get_task(tid)

    def assert_sync(self, path, marker_time):
        probe = json.loads(self.run_media([self.ffprobe, '-v', 'error', '-show_entries',
                                         'stream=codec_type,start_time:format=duration', '-of', 'json', str(path)]))
        starts = {s['codec_type']: float(s['start_time']) for s in probe['streams']}
        self.assertLess(abs(starts['video'] - starts['audio']), .08)
        samples = array('f', self.run_media([self.ffmpeg, '-v', 'error', '-i', str(path), '-vn',
                                           '-ac', '1', '-ar', '48000', '-f', 'f32le', 'pipe:1']))
        first_rms = math.sqrt(sum(value * value for value in samples[:9600]) / 9600)
        self.assertGreater(first_rms, .02, 'The original continuous tone is missing at the cut start')
        window = 960
        energies = [math.sqrt(sum(v * v for v in samples[i:i + window]) / window)
                    for i in range(0, len(samples) - window, window)]
        audio_marker = next(i * .02 for i, rms in enumerate(energies) if rms > .2)
        frames = self.run_media([self.ffmpeg, '-v', 'error', '-i', str(path), '-an', '-vf',
                                 'scale=1:1:flags=area', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'])
        video_marker = next(i / 25 for i, value in enumerate(frames) if value > 128)
        self.assertAlmostEqual(audio_marker, marker_time, delta=.08)
        self.assertAlmostEqual(video_marker, audio_marker, delta=.08)
        return float(probe['format']['duration'])

    def test_original_separate_inputs_keep_sound_from_start_and_markers_in_sync(self):
        for extension in ('webm', 'm4a'):
            with self.subTest(audio=extension), patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
                path, task = self.cut(extension)
                self.assertEqual(runner.call_count, 1)
                command = runner.call_args.args[2]
                self.assertEqual(command[command.index('-c:v') + 1], 'copy')
                self.assertEqual(command[command.index('-c:a') + 1], 'copy')
                self.assertAlmostEqual(self.assert_sync(path, 4), 8.3, delta=.1)
                self.assertTrue(any('keyframe' in warning for warning in task['warnings']))

    def test_video_conversion_discards_copied_audio_preroll_without_marker_drift(self):
        path, task = self.cut(container='mkv', video_codec='h264')
        self.assertAlmostEqual(self.assert_sync(path, .7), 5, delta=.15)
        self.assertFalse(task.get('warnings'))

    def test_keyframe_lookup_uses_absolute_packet_time_relative_to_source_origin(self):
        shifted = self.folder / 'shifted.webm'
        self.run_media([self.ffmpeg, '-v', 'error', '-i', str(self.video), '-c', 'copy',
                        '-output_ts_offset', '5', str(shifted)])
        self.assertAlmostEqual(self.engine._ffprobe_copy_start(shifted, 0, 3.3), 0, delta=.001)

    def test_cancelled_keyframe_lookup_does_not_rebuild_full_output(self):
        with patch.object(self.engine, '_ffprobe_copy_start', side_effect=DownloadCancelled()), \
                patch.object(self.engine, '_run_ffmpeg') as runner:
            with self.assertRaises(DownloadCancelled):
                self.cut()
        runner.assert_not_called()

    def test_failed_keyframe_lookup_preserves_full_output_with_explicit_warning(self):
        with patch.object(self.engine, '_ffprobe_copy_start', side_effect=MediaOperationError('invalid timestamps')):
            path, task = self.cut()
        self.assertGreater(self.engine._ffprobe_duration(str(path), self.engine.ffprobe), 11)
        self.assertTrue(any('full file was saved' in warning for warning in task['warnings']))
