import math
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader
MIB = 1024 * 1024


class DiskSpaceTests(unittest.TestCase):
    def make_app(self, folder):
        app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(folder))
        app.current_process_pids = {}
        app.paused = False
        return app

    def test_source_output_and_trim_must_fit_together(self):
        app = self.make_app('.')
        with patch.object(downloader.shutil, 'disk_usage', return_value=SimpleNamespace(free=300 * MIB)):
            app._check_disk_space('.', 100 * MIB, output_bytes=110 * MIB)
            with self.assertRaises(downloader.InsufficientDiskSpace):
                app._check_disk_space('.', 100 * MIB, output_bytes=110 * MIB, trim_bytes=110 * MIB)

    def test_insufficient_peak_space_stops_before_downloading(self):
        for mode, codec, trim, free_mib in [
            ('mp3', 'Original (Automatic)', False, 200),
            ('merge', 'Original (Automatic)', False, 200),
            ('merge', 'H.264 (Compatibility)', False, 350),
            ('progressive', 'Original (Automatic)', True, 200),
        ]:
            with self.subTest(mode=mode, codec=codec), tempfile.TemporaryDirectory() as temp:
                app = self.make_app(temp)
                app.tasks['T1'].update(format='MP3' if mode == 'mp3' else 'MP4', codec_mode=codec,
                                       trim_start='0' if trim else '')
                info = cleanup_fixture.TaskCleanupTests.info(self, mode)
                for stream in info.formats:
                    stream._filesize = (10 if stream.only_audio and mode != 'mp3' else 100) * MIB
                    if codec == 'H.264 (Compatibility)' and stream.only_video:
                        stream.video_codec = 'vp9'
                app._download_format = Mock()
                with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=info), \
                        patch.object(downloader.shutil, 'disk_usage',
                                     return_value=SimpleNamespace(free=free_mib * MIB)):
                    app._process_task(('T1', 'url'), 1)
                app._download_format.assert_not_called()
                app._task_error.assert_called_once()
                self.assertIn('Insufficient disk space', app._task_error.call_args.args[1])
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_missing_sizes_use_duration_and_bitrate_estimates(self):
        info = downloader.YTInfo('url', {'duration': 120, 'formats': [
            {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'aac', 'abr': 128},
            {'format_id': 'video', 'vcodec': 'avc1', 'acodec': 'none', 'tbr': 800}]})
        self.assertEqual(downloader.safe_filesize(info.audio_only_formats()[0]), 120 * 128000 // 8)
        self.assertEqual(downloader.safe_filesize(info.video_only_formats()[0]), 120 * 800000 // 8)
        app = self.make_app('.')
        app.tasks['T1'].update(duration=120, mp3_bitrate='320k')
        self.assertEqual(app._estimate_mp3_bytes('T1', 100), math.ceil(120 * 320000 / 8 * 1.1))

    def test_unknown_size_still_requires_headroom_and_unreadable_disk_is_reported(self):
        app = self.make_app('.')
        with patch.object(downloader.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=downloader.DISK_HEADROOM_BYTES - 1)):
            with self.assertRaises(downloader.InsufficientDiskSpace):
                app._check_disk_space('.', 0)
        with patch.object(downloader.shutil, 'disk_usage', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Could not read disk space'):
                app._check_disk_space('.', 100)

    def test_ffmpeg_stage_counts_only_new_output_and_accepts_ytdlp_file_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            video, audio, output = [Path(temp, name) for name in ('video.mp4', 'audio.m4a', 'merged.mp4')]
            video.write_bytes(b'v' * 100)
            audio.write_bytes(b'a' * 10)
            app._check_disk_space = Mock()
            for codec, expected in [('copy', math.ceil(110 * 1.1)), ('libx264', 440)]:
                app._check_ffmpeg_disk_space('T1', ['ffmpeg', '-i', 'file:' + str(video),
                                                   '-i', 'file:' + str(audio), '-c:v', codec,
                                                   'file:' + str(output)])
                app._check_disk_space.assert_called_with(temp, expected)

    def test_low_disk_blocks_trim_without_losing_original(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            source = Path(temp, 'video.mp4')
            source.write_bytes(b'original')
            app._run_ffmpeg = Mock()
            with patch.object(downloader.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaises(downloader.InsufficientDiskSpace):
                    app._maybe_trim('T1', 1, str(source), '0', '1')
            app._run_ffmpeg.assert_not_called()
            self.assertEqual(source.read_bytes(), b'original')
            self.assertEqual(list(Path(temp).iterdir()), [source])

    def test_copy_publication_checks_additional_space_and_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            private = Path(tempfile.mkdtemp(prefix='.yd-T1-', dir=temp))
            source = private / 'Example.mp3'
            source.write_bytes(b'new audio')
            original = Path(temp, 'Example.mp3')
            original.write_bytes(b'original')
            with patch.object(downloader.os, 'link', side_effect=OSError('no hard links')), \
                    patch.object(downloader.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaises(downloader.InsufficientDiskSpace):
                    app._publish_output('T1', str(source), temp)
            self.assertEqual(original.read_bytes(), b'original')
            self.assertEqual(source.read_bytes(), b'new audio')
            self.assertEqual(set(Path(temp).iterdir()), {private, original})
            self.assertEqual(app.reserved_output_paths, {})

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required')
    def test_runtime_space_drop_kills_ffmpeg_and_clears_process_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(temp)
            command = [shutil.which('ffmpeg'), '-y', '-loglevel', 'quiet', '-re', '-f', 'lavfi',
                       '-i', 'sine=frequency=440', '-t', '30', str(Path(temp, 'output.wav'))]
            actual_popen = downloader.subprocess.Popen
            processes = []

            def launch(*args, **kwargs):
                process = actual_popen(*args, **kwargs)
                processes.append(process)
                return process

            with patch.object(downloader.shutil, 'disk_usage', side_effect=[
                    SimpleNamespace(free=1024 * MIB), SimpleNamespace(free=0)]), \
                    patch.object(downloader.subprocess, 'Popen', side_effect=launch):
                with self.assertRaises(downloader.InsufficientDiskSpace):
                    app._run_ffmpeg('T1', 1, command)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual(app.current_process_pids, {})


if __name__ == '__main__':
    unittest.main()
