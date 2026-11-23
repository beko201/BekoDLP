"""Real cover downloads, stream-copy embedding, final publication and preservation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

import download_engine
from cover_processing import CoverProcessor, artwork, stream_signature
from queue_store import QueueStore
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions
from ui.output_controls import OutputControls
from utils import DownloadCancelled, NO_WINDOW, which_executable


class CoverArtworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixtures.name)
        cls.ffmpeg, cls.ffprobe = which_executable(['ffmpeg']), which_executable(['ffprobe'])
        def make(args, output):
            subprocess.run([cls.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', *args, str(cls.root / output)],
                check=True, timeout=20, **NO_WINDOW)
        make(['-f', 'lavfi', '-i', 'color=c=blue:s=120x90', '-frames:v', '1'], 'cover.jpg')
        for extension, codec in (('mp3', 'libmp3lame'), ('m4a', 'aac'), ('flac', 'flac'), ('wav', 'pcm_s16le'), ('opus', 'libopus')):
            make(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', codec], 'audio.' + extension)
        make(['-f', 'lavfi', '-i', 'testsrc2=s=128x96:r=10:d=1', '-f', 'lavfi', '-i', 'sine=duration=1',
              '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', '-movflags', '+faststart'], 'video.mp4')
        make(['-i', str(cls.root / 'video.mp4'), '-c', 'copy'], 'video.mkv')
        cls.image = (cls.root / 'cover.jpg').read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager()
        self.addCleanup(self.manager.close)
        self.manager.engine.processing_limits.configure(1, 2)
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get('Cookie'), self.headers.get('X-Cover-Test')))
                if 'session=cover-fixture' not in self.headers.get('Cookie', ''):
                    self.send_error(403)
                    return
                if self.path == '/cover.jpg':
                    data, mime = owner.image, 'image/jpeg'
                elif self.path == '/captions.vtt':
                    data, mime = b'WEBVTT\n\n00:00:00.100 --> 00:00:00.800\nCover subtitle\n', 'text/vtt'
                elif self.path == '/broken.jpg':
                    data, mime = b'not an image', 'image/jpeg'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.cookie = self.folder / 'cookies.txt'
        self.cookie.write_text('# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tcover-fixture\n')
        self.original_cookie = self.cookie.read_bytes()
        self.options = TaskOptions(folder=str(self.folder), output_mode='video', embed_cover='on',
                                  proxy_mode='direct', cookie_mode='file', cookie_file=str(self.cookie))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def metadata(self, image='/cover.jpg', subtitles=False):
        raw = {'id': 'fixture', 'title': 'Cover fixture', 'duration': 1,
            'formats': [{'format_id': '0', 'url': self.base + '/media.mp4', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'}],
            'http_headers': {'X-Cover-Test': 'yes'}}
        if image:
            raw.update(thumbnail=self.base + image, thumbnails=[{'url': self.base + image, 'id': '0',
                'http_headers': {'X-Cover-Test': 'yes'}}])
        if subtitles:
            raw['subtitles'] = {'en': [{'url': self.base + '/captions.vtt', 'ext': 'vtt'}]}
            raw['chapters'] = [{'start_time': 0, 'end_time': .4, 'title': 'First'},
                               {'start_time': .4, 'end_time': 1, 'title': 'Second'}]
        return download_engine.YTInfo(self.base + '/watch', raw)._media_info

    def task(self, fixture='audio.mp3', options=None):
        tid = self.manager.add_urls([self.base + '/watch/' + str(len(self.manager.list_tasks()))], options or self.options)[0]
        self.manager.tid_to_workerid[tid] = 1
        path = self.folder / (tid + '-' + fixture)
        path.write_bytes((self.root / fixture).read_bytes())
        return tid, path

    def probe(self, path):
        return json.loads(subprocess.check_output([self.ffprobe, '-v', 'error', '-show_streams', '-show_format',
            '-show_chapters', '-of', 'json', str(path)], **NO_WINDOW))

    def audio_hash(self, path):
        return subprocess.check_output([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(path),
            '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'], **NO_WINDOW)

    def test_real_mp3_m4a_flac_mp4_mkv_covers_preserve_media_packets(self):
        for fixture in ('audio.mp3', 'audio.m4a', 'audio.flac', 'video.mp4', 'video.mkv'):
            with self.subTest(fixture=fixture):
                tid, path = self.task(fixture)
                before, digest = self.probe(path), self.audio_hash(path)
                CoverProcessor(self.manager.engine).prepare(tid, str(path), self.metadata())
                self.assertFalse(self.manager.get_task(tid).get('warnings'), self.manager.get_task(tid).get('warnings'))
                after = self.probe(path)
                self.assertEqual(stream_signature(before), stream_signature(after))
                self.assertEqual(sum(artwork(stream) for stream in after['streams']), 1)
                self.assertEqual(self.audio_hash(path), digest)
        self.assertTrue(self.requests)
        self.assertTrue(all(path == '/cover.jpg' and 'session=cover-fixture' in cookie and header == 'yes'
                            for path, cookie, header in self.requests))
        self.assertEqual(self.cookie.read_bytes(), self.original_cookie)

    def test_subtitles_custom_name_and_cover_are_published_together(self):
        options = replace(self.options, subtitle_mode='both', subtitle_languages='en', filename_template='Custom', chapter_mode='embed')
        tid, path = self.task('video.mp4', options)
        self.manager.engine._subtitle_context[tid] = {'metadata': self.metadata(subtitles=True), 'start': 0}
        self.manager.engine._output_naming[tid] = (('Playlist',), 'Custom')
        result = Path(self.manager.engine._publish_output(tid, str(path), str(self.folder)))
        self.assertEqual(result, self.folder / 'Playlist/Custom.mp4')
        probe = self.probe(result)
        self.assertEqual(sum(artwork(stream) for stream in probe['streams']), 1)
        self.assertEqual(len([stream for stream in probe['streams'] if stream['codec_type'] == 'subtitle']), 1)
        self.assertEqual([chapter['tags']['title'] for chapter in probe['chapters']], ['First', 'Second'])
        self.assertIn('Cover subtitle', result.with_suffix('.en.srt').read_text())
        self.assertFalse(self.manager.get_task(tid).get('warnings'), self.manager.get_task(tid).get('warnings'))

    def test_missing_corrupt_and_unsupported_art_preserve_the_original_media(self):
        for fixture, image in (('audio.mp3', ''), ('audio.mp3', '/broken.jpg'), ('audio.wav', '/cover.jpg'), ('audio.opus', '/cover.jpg')):
            with self.subTest(fixture=fixture, image=image):
                tid, path = self.task(fixture)
                original = path.read_bytes()
                CoverProcessor(self.manager.engine).prepare(tid, str(path), self.metadata(image))
                self.assertEqual(path.read_bytes(), original)
                self.assertTrue(self.manager.get_task(tid).get('warnings'))

    def test_separate_chapters_receive_clipped_subtitles_and_covers(self):
        from media_sources import SourcePreparer
        options = replace(self.options, output_mode='audio', container='m4a', subtitle_mode='files',
                          subtitle_languages='en', chapter_mode='split', filename_template='Album')
        tid, source = self.task('video.mp4', options)
        sources = SourcePreparer(self.manager.engine).inspect_sources([source], 1)
        metadata = self.metadata(subtitles=True)
        info = download_engine.YTInfo(self.base + '/watch', metadata.info)
        with patch('download_engine.fetch_ytinfo_with_retry', return_value=info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources) as downloaded:
            self.manager._process_task((tid, self.base + '/watch'), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task['status'].startswith('Completed'), task)
        outputs = sorted(Path(task['output_path']).glob('*.m4a'))
        self.assertEqual(len(outputs), 2)
        self.assertEqual(downloaded.call_count, 1)
        for path in outputs:
            self.assertEqual(sum(artwork(stream) for stream in self.probe(path)['streams']), 1)
            self.assertTrue(path.with_suffix('.en.srt').is_file())
        from subtitle_processing import read_cues
        first_cue = read_cues(outputs[0].with_suffix('.en.srt'))[0]
        self.assertAlmostEqual(first_cue[0], .1, delta=.001)
        # Stream-copy AAC ends at a packet boundary (about 23 ms per packet).
        self.assertAlmostEqual(first_cue[1], .4, delta=.05)
        self.assertIn('00:00:00,000 --> 00:00:00,400', outputs[1].with_suffix('.en.srt').read_text())
        self.assertFalse(task.get('warnings'), task.get('warnings'))

    def test_multiple_ranges_apply_cover_subtitles_and_chapter_markers_to_each_clip(self):
        from media_sources import SourcePreparer
        from subtitle_processing import read_cues
        options = replace(self.options, output_mode='audio', container='m4a', subtitle_mode='files',
            subtitle_languages='en', trim_ranges='0.1 - 0.3\n0.6 - 0.8', chapter_mode='embed', filename_template='Clips')
        tid, source = self.task('video.mp4', options)
        sources = SourcePreparer(self.manager.engine).inspect_sources([source], 1)
        info = download_engine.YTInfo(self.base + '/watch', self.metadata(subtitles=True).info)
        with patch('download_engine.fetch_ytinfo_with_retry', return_value=info), \
                patch.object(SourcePreparer, 'prepare', return_value=sources) as downloaded:
            self.manager._process_task((tid, self.base + '/watch'), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task['status'].startswith('Completed'), task)
        outputs = sorted(Path(task['output_path']).glob('*.m4a'))
        self.assertEqual(len(outputs), 2)
        self.assertEqual(downloaded.call_count, 1)
        for path, title in zip(outputs, ['First', 'Second']):
            probe = self.probe(path)
            self.assertEqual(sum(artwork(stream) for stream in probe['streams']), 1)
            self.assertEqual([chapter['tags']['title'] for chapter in probe['chapters']], [title])
            cue = read_cues(path.with_suffix('.en.srt'))[0]
            self.assertAlmostEqual(cue[0], 0, delta=.001)
            self.assertAlmostEqual(cue[1], .2, delta=.05)
        self.assertFalse(task.get('warnings'), task.get('warnings'))

    def test_embedding_failure_and_cancellation_do_not_replace_media(self):
        tid, path = self.task()
        original = path.read_bytes()
        with patch.object(CoverProcessor, 'embed', side_effect=RuntimeError('fixture failure')):
            CoverProcessor(self.manager.engine).prepare(tid, str(path), self.metadata())
        self.assertEqual(path.read_bytes(), original)
        self.manager.cancel_events[tid].set()
        with self.assertRaises(DownloadCancelled):
            CoverProcessor(self.manager.engine).prepare(tid, str(path), self.metadata())
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.folder.glob('.cover-*')))

    def test_cover_off_sends_no_requests_and_old_tasks_default_to_off(self):
        options = replace(self.options, embed_cover='off')
        tid, path = self.task(options=options)
        original = path.read_bytes()
        CoverProcessor(self.manager.engine).prepare(tid, str(path), self.metadata())
        self.assertFalse(self.requests)
        self.assertEqual(path.read_bytes(), original)
        _, document = self.manager.queue_snapshot()
        document['tasks'][0]['options'].pop('embed_cover')
        store = QueueStore(self.folder / 'queue.json')
        store.save(document)
        loaded, _ = store.load()
        self.assertEqual(loaded['tasks'][0]['options']['embed_cover'], 'off')

    def test_existing_cover_is_replaced_once_and_thumbnail_size_limit_is_enforced(self):
        tid, path = self.task()
        processor = CoverProcessor(self.manager.engine)
        processor.prepare(tid, str(path), self.metadata())
        digest = self.audio_hash(path)
        processor.prepare(tid, str(path), self.metadata())
        self.assertEqual(sum(artwork(stream) for stream in self.probe(path)['streams']), 1)
        self.assertEqual(self.audio_hash(path), digest)
        self.assertFalse(self.manager.get_task(tid).get('warnings'))
        with tempfile.TemporaryDirectory(dir=self.folder) as temporary:
            info, _ = self.metadata().snapshot()
            with self.manager.engine.ytdlp_process({}, tid, folder=temporary) as downloader:
                with self.assertRaisesRegex(RuntimeError, 'size limit'):
                    downloader.download_thumbnail(info, temporary, max_bytes=10)

    def test_checkbox_preferences_and_frozen_tasks_keep_cover_choice(self):
        controls = OutputControls()
        self.assertEqual(controls.values()['embed_cover'], 'off')
        controls.cover.enabled.setChecked(True)
        options = TaskOptions(folder=str(self.folder), **controls.values())
        controls.cover.enabled.setChecked(False)
        self.assertEqual(options.embed_cover, 'on')
        self.manager.add_urls([self.base + '/saved'], options)
        _, document = self.manager.queue_snapshot()
        store = QueueStore(self.folder / 'queue.json')
        store.save(document)
        loaded, _ = store.load()
        self.assertEqual(loaded['tasks'][0]['options']['embed_cover'], 'on')
        controls.restore({**DEFAULTS, 'embed_cover': 'on'})
        self.assertTrue(controls.cover.enabled.isChecked())
        settings, warnings = validate_settings({'embed_cover': 'bad'})
        self.assertEqual(settings['embed_cover'], 'off')
        self.assertTrue(warnings)
