import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import download_engine
from task_manager import TaskManager, TaskOptions
import utils


FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')


class ContainerPolicyTests(unittest.TestCase):
    def test_selection_uses_all_codec_families_and_defaults_to_mkv(self):
        cases = [
            (['h264'], ['aac'], 'mp4'),
            (['hevc'], ['aac'], 'mp4'),
            (['av1'], ['aac'], 'mp4'),
            (['vp9'], ['opus'], 'webm'),
            (['vp8'], ['vorbis'], 'webm'),
            (['av1'], ['opus'], 'webm'),
            (['h264'], ['opus'], 'mkv'),
            (['vp9'], ['aac'], 'mkv'),
            (['ffv1'], ['flac'], 'mkv'),
            (['h264', 'vp9'], ['aac'], 'mkv'),
            (['vp9'], ['opus', 'aac'], 'mkv'),
            ([None], ['aac'], 'mkv'),
            (['unknown'], ['unknown'], 'mkv'),
        ]
        for videos, audios, expected in cases:
            with self.subTest(videos=videos, audios=audios):
                self.assertEqual(download_engine.DownloadEngine._select_merge_container(videos, audios), expected)


@unittest.skipUnless(FFMPEG and FFPROBE, 'ffmpeg and ffprobe are required')
class ContainerMergeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.output = self.folder / 'output'
        self.output.mkdir()
        log_patch = patch.object(utils, 'ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.manager = TaskManager()
        self.tid = self.manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.output)))[0]
        self.engine = self.manager.engine

    def run_media(self, args):
        return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, **utils.NO_WINDOW).stdout

    def sources(self, video_codec='libx264', video_ext='mov', audio_codec='aac', audio_ext='m4a'):
        video = self.folder / ('video.' + video_ext)
        audio = self.folder / ('audio.' + audio_ext)
        self.run_media([FFMPEG, '-y', '-f', 'lavfi', '-i', 'color=c=black:s=32x32:d=0.4',
                        '-an', '-c:v', video_codec, str(video)])
        self.run_media([FFMPEG, '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4',
                        '-vn', '-c:a', audio_codec, str(audio)])
        return video, audio

    def packet_hash(self, path, stream):
        return self.run_media([FFMPEG, '-v', 'error', '-i', str(path), '-map', stream,
                               '-c', 'copy', '-f', 'hash', '-'])

    def process(self, video, audio, *, metadata_ext=None):
        # Deliberately wrong codec metadata: the actual media must determine
        # the container even when the extractor misidentifies the streams.
        info = download_engine.YTInfo('url', {'title': 'Example', 'duration': 0.4, 'formats': [
            {'format_id': 'v', 'ext': metadata_ext or video.suffix[1:],
             'vcodec': 'avc1', 'acodec': 'none', 'height': 32},
            {'format_id': 'a', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac'},
        ]})

        def download(tid, key, url, fmt, filename, *, folder, stop_event=None):
            path = Path(folder, filename)
            shutil.copyfile(video if fmt.only_video else audio, path)
            size = path.stat().st_size
            self.manager.report_progress(tid, key, size, size)
            return str(path)

        with patch.object(download_engine, 'fetch_ytinfo_with_retry', return_value=info), \
                patch.object(self.engine, '_download_format', side_effect=download):
            self.manager._process_task((self.tid, 'url'), 1)
        return self.manager.get_task(self.tid)

    def assert_output(self, task, video, audio, ext, vcodec, acodec):
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        output = Path(task['output_path'])
        self.assertEqual(output.suffix, '.' + ext)
        probe = json.loads(self.run_media([FFPROBE, '-v', 'error', '-show_streams', '-show_format',
                                          '-of', 'json', str(output)]))
        self.assertEqual([s['codec_name'] for s in probe['streams'] if s['codec_type'] == 'video'], [vcodec])
        self.assertEqual([s['codec_name'] for s in probe['streams'] if s['codec_type'] == 'audio'], [acodec])
        self.assertIn({'mp4': 'mp4', 'webm': 'webm', 'mkv': 'matroska'}[ext],
                      probe['format']['format_name'].split(','))
        self.assertEqual(self.packet_hash(video, '0:v:0'), self.packet_hash(output, '0:v:0'))
        self.assertEqual(self.packet_hash(audio, '0:a:0'), self.packet_hash(output, '0:a:0'))
        self.assertFalse(list(self.output.glob('.yd-*')))
        return output

    def test_mov_h264_and_aac_merge_as_mp4_without_reencoding(self):
        video, audio = self.sources()
        task = self.process(video, audio)
        output = self.assert_output(task, video, audio, 'mp4', 'h264', 'aac')
        self.assertEqual(list(self.output.iterdir()), [output])

    def test_vp9_and_opus_choose_webm_even_with_mp4_metadata(self):
        video, audio = self.sources('libvpx-vp9', 'mkv', 'libopus', 'ogg')
        task = self.process(video, audio, metadata_ext='mp4')
        self.assert_output(task, video, audio, 'webm', 'vp9', 'opus')

    def test_vp9_and_aac_choose_mkv_instead_of_transcoding_audio(self):
        video, audio = self.sources('libvpx-vp9', 'webm')
        task = self.process(video, audio)
        self.assert_output(task, video, audio, 'mkv', 'vp9', 'aac')

    def test_h264_and_opus_choose_mkv_instead_of_webm(self):
        video, audio = self.sources(audio_codec='libopus', audio_ext='ogg')
        task = self.process(video, audio)
        self.assert_output(task, video, audio, 'mkv', 'h264', 'opus')

    def test_other_codec_families_choose_mkv_without_reencoding(self):
        video, audio = self.sources('ffv1', 'mkv', 'flac', 'flac')
        task = self.process(video, audio)
        self.assert_output(task, video, audio, 'mkv', 'ffv1', 'flac')

    def test_failed_mp4_retries_as_mkv_and_preserves_existing_file(self):
        video, audio = self.sources()
        existing = self.output / 'Example.mkv'
        existing.write_bytes(b'existing user file')
        run = self.engine._run_ffmpeg
        attempts = []

        def merge(tid, worker, cmd, *args, **kwargs):
            attempts.append(cmd)
            if len(attempts) == 1:
                Path(cmd[-1]).write_bytes(b'partial mp4')
                return 1
            return run(tid, worker, cmd, *args, **kwargs)

        with patch.object(self.engine, '_run_ffmpeg', side_effect=merge):
            task = self.process(video, audio)
        output = self.assert_output(task, video, audio, 'mkv', 'h264', 'aac')
        self.assertEqual(len(attempts), 2)
        for cmd in attempts:
            self.assertEqual(cmd[cmd.index('-c:v') + 1], 'copy')
            self.assertEqual(cmd[cmd.index('-c:a') + 1], 'copy')
        self.assertEqual(existing.read_bytes(), b'existing user file')
        self.assertEqual(set(self.output.iterdir()), {existing, output})

    def test_cancellation_never_triggers_a_second_merge(self):
        video, audio = self.sources()

        def cancelled(tid, worker, cmd, *args, **kwargs):
            Path(cmd[-1]).write_bytes(b'partial')
            self.manager.cancel_events[tid].set()
            return 1

        with patch.object(self.engine, '_run_ffmpeg', side_effect=cancelled) as runner:
            task = self.process(video, audio)
        runner.assert_called_once()
        self.assertEqual(task['status'], 'Cancelled')
        self.assertIsNone(task['output_path'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_failed_mkv_stops_without_audio_transcoding_or_partial_publication(self):
        video, audio = self.sources('libvpx-vp9', 'webm')

        def failed(tid, worker, cmd, *args, **kwargs):
            Path(cmd[-1]).write_bytes(b'partial mkv')
            return 1

        with patch.object(self.engine, '_run_ffmpeg', side_effect=failed) as runner:
            task = self.process(video, audio)
        runner.assert_called_once()
        self.assertTrue(task['status'].startswith('Error:'), task['status'])
        self.assertLess(task['progress'], 100)
        self.assertIsNone(task['output_path'])
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
