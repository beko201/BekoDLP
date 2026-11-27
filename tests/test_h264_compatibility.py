import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import download_engine
from task_manager import TaskManager, TaskOptions
import app_logging
import errors
import process_utils


FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


@unittest.skipUnless(FFMPEG and FFPROBE, 'ffmpeg and ffprobe are required')
class H264CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        log_patch = patch.object(app_logging, 'ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.output = self.folder / 'output'
        self.output.mkdir()
        self.manager = TaskManager()
        self.tid = self.manager.add_urls(['https://example.com/video'], TaskOptions(
            folder=str(self.output), codec_mode='H.264 (Compatibility)'))[0]
        self.engine = self.manager.engine

    def run_media(self, args):
        return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, **process_utils.NO_WINDOW).stdout

    def source(self, video='libx264', audio='libopus', container='mp4'):
        path = self.folder / ('source.' + container)
        cmd = [FFMPEG, '-y', '-f', 'lavfi', '-i', 'color=c=black:s=32x32:d=0.4']
        if audio:
            cmd += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4']
        cmd += ['-c:v', video]
        if audio:
            cmd += ['-c:a', audio]
        cmd += [str(path)]
        self.run_media(cmd)
        return path

    def video_hash(self, path):
        return hashlib.sha256(self.run_media([
            FFMPEG, '-v', 'error', '-i', str(path), '-map', '0:v:0',
            '-c:v', 'copy', '-f', 'h264', '-',
        ])).digest()

    def assert_compatible(self, path, audio=True):
        info = json.loads(self.run_media([
            FFPROBE, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path),
        ]))
        self.assertIn('mp4', info['format']['format_name'].split(','))
        self.assertNotEqual(info['format'].get('tags', {}).get('major_brand', '').strip(), 'qt')
        self.assertEqual([s['codec_name'] for s in info['streams'] if s['codec_type'] == 'video'], ['h264'])
        self.assertEqual([s['codec_name'] for s in info['streams'] if s['codec_type'] == 'audio'],
                         ['aac'] if audio else [])

    def process_progressive(self, source):
        # Metadata intentionally claims AVC/AAC even when the media differs.
        info = download_engine.YTInfo('url', {'title': 'Example', 'duration': 0.4, 'formats': [
            {'format_id': 'p', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'aac', 'height': 32},
        ]})

        def download(tid, key, url, fmt, filename, *, folder):
            destination = Path(folder, filename)
            shutil.copyfile(source, destination)
            return str(destination)

        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info), \
                patch.object(self.engine, '_download_format', side_effect=download):
            self.manager._process_task((self.tid, 'url'), 1)
        return self.manager.get_task(self.tid)

    def separate_sources(self, video_codec='libx264', audio_codec='aac'):
        video = self.source(video=video_codec, audio=None)
        audio = self.folder / ('audio.m4a' if audio_codec == 'aac' else 'audio.webm')
        self.run_media([FFMPEG, '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4',
                        '-vn', '-c:a', audio_codec, str(audio)])
        return video, audio

    def audio_hash(self, path):
        return self.run_media([
            FFMPEG, '-v', 'error', '-i', str(path), '-map', '0:a:0',
            '-c:a', 'copy', '-f', 'hash', '-',
        ])

    def process_separate(self, video, audio, video_metadata='avc1', audio_metadata='mp4a.40.2'):
        info = download_engine.YTInfo('url', {'title': 'Example', 'duration': 0.4, 'formats': [
            {'format_id': 'v', 'ext': 'mp4', 'vcodec': video_metadata, 'acodec': 'none', 'height': 32},
            {'format_id': 'a', 'ext': 'm4a', 'vcodec': 'none', 'acodec': audio_metadata},
        ]})

        def download(tid, key, url, fmt, filename, *, folder, stop_event=None):
            destination = Path(folder, filename)
            shutil.copyfile(video if key == 'video' else audio, destination)
            return str(destination)

        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info), \
                patch.object(self.engine, '_download_format', side_effect=download):
            self.manager._process_task((self.tid, 'url'), 1)
        return self.manager.get_task(self.tid)

    def test_separate_aac_is_copied_without_changing_audio_packets(self):
        video, audio = self.separate_sources()
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_separate(video, audio)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.audio_hash(audio), self.audio_hash(task['output_path']))
        self.assertEqual(self.video_hash(video), self.video_hash(task['output_path']))
        runner.assert_called_once()
        cmd = runner.call_args.args[2]
        self.assertEqual(cmd[cmd.index('-c:a') + 1], 'copy')
        self.assertNotIn('-b:a', cmd)

    def test_real_aac_overrides_opus_metadata_when_merging(self):
        video, audio = self.separate_sources()
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_separate(video, audio, audio_metadata='opus')
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.audio_hash(audio), self.audio_hash(task['output_path']))
        cmd = runner.call_args.args[2]
        self.assertEqual(cmd[cmd.index('-c:a') + 1], 'copy')

    def test_real_opus_is_converted_even_when_metadata_claims_aac(self):
        video, audio = self.separate_sources(audio_codec='libopus')
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_separate(video, audio)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.video_hash(video), self.video_hash(task['output_path']))
        cmd = runner.call_args.args[2]
        self.assertEqual(cmd[cmd.index('-c:a') + 1], 'aac')
        self.assertEqual(cmd[cmd.index('-b:a') + 1], '192k')

    def test_video_reencode_retry_still_preserves_aac_packets(self):
        video, audio = self.separate_sources()
        run_ffmpeg = self.engine._run_ffmpeg
        attempts = []

        def fail_first_merge(tid, worker, cmd, *args, **kwargs):
            attempts.append(cmd)
            if len(attempts) == 1:
                Path(cmd[-1]).write_bytes(b'failed merge')
                return 1
            return run_ffmpeg(tid, worker, cmd, *args, **kwargs)

        with patch.object(self.engine, '_run_ffmpeg', side_effect=fail_first_merge):
            task = self.process_separate(video, audio)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.audio_hash(audio), self.audio_hash(task['output_path']))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1][attempts[1].index('-c:v') + 1], 'libx264')
        for cmd in attempts:
            self.assertEqual(cmd[cmd.index('-c:a') + 1], 'copy')
            self.assertNotIn('-b:a', cmd)

    def test_video_conversion_preserves_already_compatible_aac(self):
        video, audio = self.separate_sources(video_codec='mpeg4')
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_separate(video, audio, video_metadata='mp4v')
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.audio_hash(audio), self.audio_hash(task['output_path']))
        cmd = runner.call_args.args[2]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'libx264')
        self.assertEqual(cmd[cmd.index('-c:a') + 1], 'copy')

    def test_missing_audio_stream_fails_before_merge(self):
        video = self.source(audio=None)
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_separate(video, video)
        self.assertTrue(task['status'].startswith('Error:'), task['status'])
        self.assertIsNone(task['output_path'])
        runner.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_progressive_opus_becomes_aac_without_changing_h264_video(self):
        source = self.source()
        task = self.process_progressive(source)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.video_hash(source), self.video_hash(task['output_path']))
        self.assertEqual(list(self.output.iterdir()), [Path(task['output_path'])])

    def test_compatible_progressive_is_not_reencoded(self):
        source = self.source(audio='aac')
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_progressive(source)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        for call in runner.call_args_list:
            command = call.args[2]
            self.assertEqual(command[command.index('-c:v') + 1], 'copy')
            self.assertEqual(command[command.index('-c:a') + 1], 'copy')
        self.assertEqual(self.video_hash(source), self.video_hash(task['output_path']))
        self.assertEqual(self.audio_hash(source), self.audio_hash(task['output_path']))

    def test_real_video_codec_overrides_progressive_metadata(self):
        task = self.process_progressive(self.source(video='mpeg4', audio='aac'))
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'])

    def test_mov_disguised_as_mp4_is_remuxed_without_reencoding(self):
        source = self.source(audio='aac', container='mov')
        with patch.object(self.engine, '_run_ffmpeg', wraps=self.engine._run_ffmpeg) as runner:
            task = self.process_progressive(source)
        self.assert_compatible(task['output_path'])
        cmd = runner.call_args.args[2]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'copy')
        self.assertEqual(cmd[cmd.index('-c:a') + 1], 'copy')
        self.assertEqual(self.video_hash(source), self.video_hash(task['output_path']))

    def test_silent_h264_video_remains_silent(self):
        task = self.process_progressive(self.source(audio=None))
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assert_compatible(task['output_path'], audio=False)

    def test_selector_fallback_also_converts_opus_and_preserves_h264(self):
        source = self.source()

        def ydl(opts, **kwargs):
            instance = Mock()
            instance.__enter__ = Mock(return_value=instance)
            instance.__exit__ = Mock(return_value=False)

            def extract(url, download):
                destination = opts['outtmpl'].replace('%(ext)s', 'mp4')
                shutil.copyfile(source, destination)
                return {'requested_downloads': [{'filepath': destination}]}

            instance.extract_info.side_effect = extract
            return instance

        with patch('engine_transfers.YtDlpProcess', side_effect=ydl):
            self.engine._complete_via_selector(self.tid, 1, 'url', 'Example', 'Example',
                                               'MP4', 1080, True, '', '', folder=str(self.output))
        task = self.manager.get_task(self.tid)
        self.assert_compatible(task['output_path'])
        self.assertEqual(self.video_hash(source), self.video_hash(task['output_path']))

    def test_unreadable_media_is_never_published_as_compatible(self):
        source = self.folder / 'broken.mp4'
        source.write_bytes(b'not media')
        task = self.process_progressive(source)
        self.assertTrue(task['status'].startswith('Error:'), task['status'])
        self.assertIsNone(task['output_path'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_validation_or_cancellation_preserves_source_and_cleans_conversion(self):
        for failure in ('validation', 'cancel', 'ffmpeg'):
            with self.subTest(failure=failure):
                source = self.source()
                original = source.read_bytes()
                incompatible = self.engine._ffprobe_media(str(source))

                def convert(tid, worker, cmd, *args, **kwargs):
                    Path(cmd[-1]).write_bytes(b'invalid output')
                    if failure == 'cancel':
                        raise errors.DownloadCancelled()
                    if failure == 'ffmpeg':
                        raise RuntimeError('conversion failed')

                expected = errors.DownloadCancelled if failure == 'cancel' else RuntimeError
                with patch.object(self.engine, '_run_ffmpeg', side_effect=convert), \
                        patch.object(self.engine, '_ffprobe_media', return_value=incompatible):
                    with self.assertRaises(expected):
                        self.engine._ensure_h264_compatible(self.tid, 1, str(source))
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(list(self.folder.glob('*_compatible*.mp4')))
                self.assertEqual(self.engine.reserved_output_paths, {})


if __name__ == '__main__':
    unittest.main()
