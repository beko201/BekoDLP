"""Real subtitle downloads, muxing, clipping, bundle isolation and saved choices."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

import download_engine
from queue_store import QueueStore
from settings import validate_settings
from subtitle_options import SUBTITLE_DEFAULTS, select_languages, subtitle_selection, validate_subtitle_options
from subtitle_processing import clip_cues, publish_bundle, read_cues, write_cues
from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from errors import DownloadCancelled
from process_utils import NO_WINDOW
from runtime_tools import which_executable
from ytdlp_process import YtDlpProcess


VTT = 'WEBVTT\n\n00:00:00.100 --> 00:00:00.600\nEarly caption\n\n00:00:01.000 --> 00:00:02.000\n{title} — Türkçe\n\n00:00:02.500 --> 00:00:03.600\nLast caption\n'


class SubtitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.fixture_root = Path(cls.fixtures.name)
        cls.ffmpeg, cls.ffprobe = which_executable(['ffmpeg']), which_executable(['ffprobe'])
        cls.video = cls.fixture_root / 'source.mp4'
        subprocess.run([cls.ffmpeg, '-y', '-f', 'lavfi', '-i', 'testsrc2=s=128x96:r=10:d=4',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-c:v', 'libx264', '-threads', '2',
            '-g', '10', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(cls.video)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        cls.webm = cls.fixture_root / 'source.webm'
        subprocess.run([cls.ffmpeg, '-y', '-i', str(cls.video), '-c:v', 'libvpx-vp9', '-threads', '2',
            '-deadline', 'realtime', '-c:a', 'libopus', str(cls.webm)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        cls.audio = cls.fixture_root / 'source.mp3'
        subprocess.run([cls.ffmpeg, '-y', '-i', str(cls.video), '-vn', '-c:a', 'libmp3lame', str(cls.audio)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.private = self.folder / 'private'
        self.private.mkdir()
        self.manager = TaskManager()
        self.addCleanup(self.manager.close)
        self.engine = self.manager.engine
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            for box in list(window._message_boxes):
                box.done(QMessageBox.StandardButton.Ok)
            finish_window_close(window)
            window.controller.poll()
        for _ in range(3):
            self.app.processEvents()

    def info(self, subtitles=None, automatic=None, url='https://example.com/video'):
        return download_engine.YTInfo(url, {'id': 'test', 'title': 'Subtitle Sample', 'duration': 4,
            'formats': [{'format_id': 'main', 'url': 'http://127.0.0.1:9/unused.mp4', 'ext': 'mp4',
                         'vcodec': 'avc1', 'acodec': 'mp4a', 'height': 96}],
            'subtitles': subtitles if subtitles is not None else {
                lang: [{'ext': 'vtt', 'data': VTT.format(title=title)}]
                for lang, title in (('en', 'English'), ('tr', 'Türkçe'))},
            'automatic_captions': automatic or {}})

    def prepare(self, *, source=None, info=None, **settings):
        options = TaskOptions(folder=str(self.folder), subtitle_mode=settings.pop('subtitle_mode', 'both'), **settings)
        tid = self.manager.add_urls(['https://example.com/video'], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        source = source or self.video
        destination = self.private / ('Movie' + source.suffix)
        shutil.copyfile(source, destination)
        self.engine._subtitle_context[tid] = {'metadata': (info or self.info())._media_info, 'start': 0}
        return tid, destination

    def probe(self, path):
        return json.loads(subprocess.check_output([self.ffprobe, '-v', 'error', '-show_streams', '-show_format',
                                                   '-of', 'json', str(path)], text=True, **NO_WINDOW))

    def test_language_selection_is_bounded_and_automatic_captions_are_opt_in(self):
        info = {'subtitles': {'tr': [{}], 'en-US': [{}], 'live_chat': [{}]},
                'automatic_captions': {'de': [{}]}}
        selected = dict(SUBTITLE_DEFAULTS, subtitle_mode='files', subtitle_languages='tr,en.*')
        self.assertEqual(select_languages(info, selected), ['tr', 'en-US'])
        selected.update(subtitle_languages='all', subtitle_auto='on')
        self.assertEqual(select_languages(info, selected), ['de', 'en-US', 'tr'])
        for value in ('', '(a+)+', '../en', 'en,--exec evil'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_subtitle_options(dict(selected, subtitle_languages=value))

    def test_srt_and_webvtt_cues_are_clipped_and_shifted(self):
        cues = [(0.1, .6, 'Before'), (1, 2, 'Türkçe'), (2.5, 3.6, 'Last')]
        clipped = clip_cues(cues, 1, 1.8)
        self.assertEqual(clipped, [(0, 1, 'Türkçe'), (1.5, 1.7999999999999998, 'Last')])
        srt, vtt = self.private / 'clip.srt', self.private / 'clip.vtt'
        write_cues(srt, clipped, 'srt')
        write_cues(vtt, clipped, 'vtt')
        self.assertEqual(read_cues(srt)[0], (0, 1, 'Türkçe'))
        self.assertIn('00:00:01,800', srt.read_text(encoding='utf-8'))
        self.assertTrue(vtt.read_text(encoding='utf-8').startswith('WEBVTT\n\n'))

    def test_mp4_embeds_two_tracks_and_saves_matching_sidecars(self):
        tid, source = self.prepare()
        output = Path(self.engine._publish_output(tid, str(source), str(self.folder)))
        probe = self.probe(output)
        subs = [s for s in probe['streams'] if s['codec_type'] == 'subtitle']
        self.assertEqual([s['codec_name'] for s in subs], ['mov_text', 'mov_text'])
        self.assertEqual({s['tags']['language'] for s in subs}, {'eng', 'tur'})
        self.assertTrue(output.with_suffix('.en.srt').is_file())
        self.assertIn('Türkçe', output.with_suffix('.tr.srt').read_text(encoding='utf-8'))
        self.assertFalse(self.manager.get_task(tid).get('warnings'))
        self.assertFalse(list(self.private.glob('.subs-*')))

    def test_embed_only_leaves_no_external_subtitle_files(self):
        tid, source = self.prepare(subtitle_mode='embed', subtitle_languages='en')
        output = self.engine._publish_output(tid, str(source), str(self.folder))
        self.assertEqual(sum(s['codec_type'] == 'subtitle' for s in self.probe(output)['streams']), 1)
        self.assertFalse(list(self.folder.glob('*.srt')))

    def test_webm_uses_webvtt_tracks_and_vtt_sidecars(self):
        tid, source = self.prepare(source=self.webm, subtitle_languages='en', subtitle_format='vtt')
        output = Path(self.engine._publish_output(tid, str(source), str(self.folder)))
        self.assertEqual([s['codec_name'] for s in self.probe(output)['streams'] if s['codec_type'] == 'subtitle'], ['webvtt'])
        self.assertTrue(output.with_suffix('.en.vtt').read_text(encoding='utf-8').startswith('WEBVTT'))

    def test_mkv_uses_subrip_tracks(self):
        source = self.private / 'original.mkv'
        subprocess.run([self.ffmpeg, '-y', '-i', str(self.video), '-c', 'copy', str(source)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        tid, destination = self.prepare(source=source, subtitle_languages='tr')
        output = self.engine._publish_output(tid, str(destination), str(self.folder))
        self.assertEqual([s['codec_name'] for s in self.probe(output)['streams'] if s['codec_type'] == 'subtitle'], ['subrip'])

    def test_audio_downloads_save_separate_subtitles(self):
        tid, source = self.prepare(source=self.audio, output_mode='audio', container='mp3', audio_codec='mp3',
                                   subtitle_mode='files', subtitle_languages='en')
        output = Path(self.engine._publish_output(tid, str(source), str(self.folder)))
        self.assertTrue(output.with_suffix('.en.srt').exists())
        self.assertFalse(any(s['codec_type'] == 'subtitle' for s in self.probe(output)['streams']))

    def test_unavailable_languages_warn_and_preserve_the_media(self):
        tid, source = self.prepare(info=self.info(subtitles={}))
        original = source.read_bytes()
        output = Path(self.engine._publish_output(tid, str(source), str(self.folder)))
        self.assertEqual(output.read_bytes(), original)
        self.assertIn('No subtitles were available', self.manager.get_task(tid)['warnings'][0])

    def test_manual_captions_take_precedence_and_automatic_languages_are_optional(self):
        info = self.info(subtitles={'tr': [{'ext': 'vtt', 'data': VTT.format(title='Manual Turkish')}]},
                         automatic={lang: [{'ext': 'vtt', 'data': VTT.format(title='Automatic ' + lang)}]
                                    for lang in ('tr', 'en')})
        tid, source = self.prepare(info=info, subtitle_mode='files', subtitle_auto='on')
        output = Path(self.engine._publish_output(tid, str(source), str(self.folder)))
        self.assertIn('Manual Turkish', output.with_suffix('.tr.srt').read_text(encoding='utf-8'))
        self.assertIn('Automatic en', output.with_suffix('.en.srt').read_text(encoding='utf-8'))

    def test_sidecar_collisions_rename_the_whole_bundle(self):
        tid, source = self.prepare(subtitle_mode='files', subtitle_languages='en')
        existing = self.folder / 'Movie.en.srt'
        existing.write_bytes(b'foreign subtitle')
        output = Path(self.engine._publish_output(tid, str(source), str(self.folder)))
        self.assertNotEqual(output.name, 'Movie.mp4')
        self.assertTrue(output.with_suffix('.en.srt').is_file())
        self.assertEqual(existing.read_bytes(), b'foreign subtitle')
        self.assertFalse((self.folder / 'Movie.mp4').exists())

    def test_cancel_during_bundle_publication_removes_only_its_own_files(self):
        tid, source = self.prepare(subtitle_mode='files')
        sidecar = self.private / 'caption.srt'
        sidecar.write_text('captions', encoding='utf-8')
        foreign = self.folder / 'unrelated.srt'
        foreign.write_text('preserve', encoding='utf-8')
        real_link = os.link
        def link(input_path, target):
            real_link(input_path, target)
            self.manager.cancel_events[tid].set()
        with patch('subtitle_processing.os.link', side_effect=link):
            with self.assertRaises(DownloadCancelled):
                publish_bundle(self.engine, tid, str(source), str(self.folder), [('en', sidecar)])
        self.assertEqual(list(self.folder.glob('*.srt')), [foreign])
        self.assertFalse(list(self.folder.glob('*.mp4')))
        self.assertFalse(self.engine.reserved_output_paths)

    def test_bundle_copy_fallback_uses_exclusive_destinations(self):
        tid, source = self.prepare(subtitle_mode='files')
        sidecar = self.private / 'caption.srt'
        sidecar.write_text('captions', encoding='utf-8')
        with patch('subtitle_processing.os.link', side_effect=OSError('hard links unavailable')):
            output = Path(publish_bundle(self.engine, tid, str(source), str(self.folder), [('en', sidecar)]))
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertEqual(output.with_suffix('.en.srt').read_text(encoding='utf-8'), 'captions')

    def test_preferences_queue_and_old_snapshots_keep_subtitle_choices(self):
        defaults, errors = validate_settings({})
        self.assertFalse(errors)
        self.assertEqual(subtitle_selection(defaults), SUBTITLE_DEFAULTS)
        options = TaskOptions(folder=str(self.folder), subtitle_mode='both', subtitle_languages='en.*', subtitle_auto='on')
        tid = self.manager.add_urls(['https://example.com/queued'], options)[0]
        store = QueueStore(self.folder / 'queue.json')
        store.save(self.manager.queue_snapshot()[1])
        restored = TaskManager(engine_factory=lambda m: Mock())
        self.addCleanup(restored.close)
        document = store.load()[0]
        restored.restore_queue(document)
        self.assertEqual(subtitle_selection(restored.get_task(tid)), subtitle_selection(asdict(options)))
        for row in document['tasks']:
            for key in SUBTITLE_DEFAULTS:
                row['options'].pop(key)
        legacy = TaskManager(engine_factory=lambda m: Mock())
        self.addCleanup(legacy.close)
        legacy.restore_queue(document)
        self.assertEqual(subtitle_selection(legacy.get_task(tid)), SUBTITLE_DEFAULTS)

    def test_audio_mode_changes_embedding_to_files_and_invalid_languages_block_submission(self):
        manager = TaskManager(engine_factory=lambda m: Mock())
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=self.folder / 'settings.json')
        self.windows.append(window)
        self.addCleanup(manager.close)
        controls = window.output_controls
        subs = controls.subtitles
        changed = []
        subs.changed.connect(lambda: changed.append(True))
        subs.languages_edit.setText('en')
        subs.auto_check.setChecked(True)
        subs.format_combo.setCurrentIndex(subs.format_combo.findData('vtt'))
        self.assertEqual(len(changed), 3)
        subs.mode_combo.setCurrentIndex(subs.mode_combo.findData('both'))
        controls.format_combo.setCurrentIndex(controls.format_combo.findData('MP3'))
        self.assertEqual(subs.values()['subtitle_mode'], 'files')
        self.assertFalse(subs.mode_combo.model().item(subs.mode_combo.findData('embed')).isEnabled())
        window.folder = str(self.folder)
        window.url_edit.setText('https://example.com/video')
        subs.languages_edit.setText('../unsafe')
        with patch.object(manager, 'submit_link') as submit, patch.object(window, '_show_message') as show:
            window.enqueue()
        submit.assert_not_called()
        self.assertEqual(show.call_args.args[1], 'Subtitle settings')
        self.assertEqual(window.url_edit.text(), 'https://example.com/video')

    def test_real_task_downloads_authenticated_subtitles_and_aligns_them_to_the_trim(self):
        self.run_authenticated_task('1.2', '2.8', expected_start=0)

    def test_failed_trim_keeps_full_length_subtitle_timestamps(self):
        self.run_authenticated_task('9', '10', expected_start=.1)

    def run_authenticated_task(self, start, end, expected_start):
        requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cookie = self.headers.get('Cookie', '')
                requests.append((self.path, cookie))
                if 'session=subtitle-fixture' not in cookie:
                    self.send_response(403)
                    self.end_headers()
                    return
                payload = owner.video.read_bytes() if self.path == '/media.mp4' else VTT.format(title='Authenticated').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Content-Type', 'video/mp4' if self.path == '/media.mp4' else 'text/vtt')
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        jar = self.folder / 'cookies.txt'
        jar.write_text('# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tsubtitle-fixture\n')
        options = TaskOptions(folder=str(self.folder), output_mode='video', container='mp4', subtitle_mode='both',
                              cookie_mode='file', cookie_file=str(jar), trim_start=start, trim_end=end)
        tid = self.manager.add_urls([base + '/watch'], options)[0]
        info = self.info(subtitles={lang: [{'ext': 'vtt', 'url': base + '/' + lang + '.vtt'}]
                                   for lang in ('tr', 'en')}, url=base + '/watch')
        info._media_info.info['formats'][0]['url'] = base + '/media.mp4'
        try:
            with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info):
                self.manager._process_task((tid, base + '/watch'), 1)
            task = self.manager.get_task(tid)
            self.assertTrue(task['status'].startswith('Completed'), task)
            self.assertFalse(any('could not' in warning.lower() and 'subtitle' in warning.lower()
                                 for warning in task.get('warnings', [])), task.get('warnings'))
            output = Path(task['output_path'])
            cues = read_cues(output.with_suffix('.en.srt'))
            self.assertAlmostEqual(cues[0][0], expected_start, places=2)
            probe = self.probe(output)
            self.assertLessEqual(max(cue[1] for cue in cues), float(probe['format']['duration']) + .01)
            self.assertEqual(sum(stream['codec_type'] == 'subtitle' for stream in probe['streams']), 2)
            self.assertTrue(any(path == '/en.vtt' for path, _ in requests))
            self.assertTrue(all('session=subtitle-fixture' in cookie for _, cookie in requests))
            self.assertFalse(self.engine._subtitle_context)
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
