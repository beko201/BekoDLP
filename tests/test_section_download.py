"""Real seekable HTTP sections, source timing, codecs, captions and explicit full mode."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

import download_engine
from media_processing import ConfiguredOutput
from media_sources import SourcePreparer
from queue_store import QueueStore
from section_download import SectionMetadata
from settings import DEFAULTS
from task_manager import TaskManager, TaskOptions
from ui.output_controls import OutputControls
from utils import DownloadCancelled, NO_WINDOW, which_executable
from ytdlp_process import YtDlpProcess


class SectionDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixtures.name)
        cls.ffmpeg = which_executable(['ffmpeg'])
        cls.ffprobe = which_executable(['ffprobe'])
        source = cls.root / 'source.mp4'
        subprocess.run([cls.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
            'testsrc2=size=320x180:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
            '-t', '120', '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', '-g', '50', '-bf', '0',
            '-c:a', 'aac', '-movflags', '+faststart', str(source)], check=True, timeout=45, **NO_WINDOW)
        for mode, output in (('-an', 'video.mp4'), ('-vn', 'audio.m4a')):
            subprocess.run([cls.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                mode, '-c', 'copy', '-movflags', '+faststart', str(cls.root / output)],
                check=True, timeout=15, **NO_WINDOW)
        cls.payloads = {name: (cls.root / name).read_bytes() for name in ('source.mp4', 'video.mp4', 'audio.m4a')}

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager()
        self.manager.engine.processing_limits.configure(1, 2)
        self.addCleanup(self.manager.close)
        self.requests = []
        self.transferred = 0
        self.unauthorized = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.path.lstrip('/').split('?', 1)[0]
                data = owner.payloads.get(name)
                if data is None:
                    self.send_error(404)
                    return
                # Authenticate the loopback fixture with an extractor header.
                # Bundled FFmpeg does not forward domain cookies to IP:port URLs;
                # yt-dlp still receives the copied jar and passes scoped cookies.
                valid = self.headers.get('X-Section-Fixture') == 'yes'
                if not valid:
                    owner.unauthorized.append((name, self.headers.get('Cookie'), self.headers.get('X-Section-Fixture')))
                    self.send_error(403, 'Missing fixture header')
                    return
                header = self.headers.get('Range', '')
                owner.requests.append((name, header))
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', header)
                start = int(match[1]) if match else 0
                end = min(int(match[2]) if match and match[2] else len(data) - 1, len(data) - 1)
                self.send_response(206 if match else 200)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Type', 'audio/mp4' if name.endswith('.m4a') else 'video/mp4')
                self.send_header('Content-Length', str(end - start + 1))
                if match:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                self.end_headers()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
                try:
                    for offset in range(start, end + 1, 4096):
                        chunk = data[offset:min(offset + 4096, end + 1)]
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        owner.transferred += len(chunk)
                        time.sleep(.0005)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        cookie = self.folder / 'cookies.txt'
        cookie.write_text('# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tsection-fixture\n')
        self.cookie_bytes = cookie.read_bytes()
        self.options = TaskOptions(folder=str(self.folder), output_mode='video', container='mkv',
            trim_start='20.5', trim_end='23.5', trim_download_mode='section', proxy_mode='direct',
            subtitle_mode='files', cookie_mode='file', cookie_file=str(cookie))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def info(self, separate=False):
        def fmt(identifier, name, video, audio):
            return dict(format_id=identifier, url=self.base + '/' + name, ext='m4a' if name.endswith('.m4a') else 'mp4',
                protocol='http', vcodec=video, acodec=audio, height=180 if video != 'none' else None,
                filesize=len(self.payloads[name]), http_headers={'X-Section-Fixture': 'yes'})
        formats = [fmt('137', 'video.mp4', 'avc1', 'none'), fmt('140', 'audio.m4a', 'none', 'mp4a')] if separate else [
            fmt('0', 'source.mp4', 'avc1', 'mp4a')]
        return download_engine.YTInfo(self.base + '/watch', dict(id='local', title='Section', duration=120,
            formats=formats), asdict(self.options), asdict(self.options))

    def process(self, options=None, separate=False):
        options = options or self.options
        url = self.base + '/watch?attempt=' + str(len(self.manager.list_tasks()))
        tid = self.manager.add_urls([url], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        private = self.folder / ('private-' + tid)
        private.mkdir()
        output = ConfiguredOutput(self.manager.engine).process(tid, 1, self.base + '/watch', self.info(separate),
            self.manager.get_task(tid), str(private), 'Section')
        return tid, output

    def probe(self, path):
        return json.loads(subprocess.check_output([self.ffprobe, '-v', 'error', '-show_format', '-show_streams',
            '-of', 'json', str(path)], **NO_WINDOW))

    def test_original_section_uses_less_data_and_preserves_sync_and_source_caption_offset(self):
        tid, output = self.process()
        probe = self.probe(output)
        self.assertAlmostEqual(float(probe['format']['duration']), 3.5, delta=.15)
        self.assertEqual([stream['codec_name'] for stream in probe['streams']], ['h264', 'aac'])
        self.assertAlmostEqual(self.manager.engine._subtitle_context[tid]['start'], 20, delta=.05)
        starts = [float(stream.get('start_time') or 0) for stream in probe['streams']]
        self.assertLess(abs(starts[0] - starts[1]), .06)
        self.assertTrue(any(header and not header.startswith('bytes=0-') for _, header in self.requests))
        self.assertLess(self.transferred, len(self.payloads['source.mp4']) * .8)
        self.assertFalse(self.unauthorized)
        self.assertEqual(Path(self.options.cookie_file).read_bytes(), self.cookie_bytes)

    def test_multiple_ranges_download_separate_sections(self):
        options = replace(self.options, trim_start='', trim_end='', trim_ranges='20.5 - 23.5\n90.25 - 92.25')
        with patch.object(self.manager.engine, 'ytdlp_process', wraps=self.manager.engine.ytdlp_process) as native:
            tid, batch = self.process(options)
        sections = [call.args[0]['download_section'] for call in native.call_args_list if 'download_section' in call.args[0]]
        self.assertEqual(sections, [(20.5, 23.5), (90.25, 92.25)])
        self.assertEqual(len(batch.parts), 2)
        self.assertAlmostEqual(batch.parts[0].start, 20, delta=.05)
        self.assertAlmostEqual(batch.parts[1].start, 90, delta=.05)
        self.assertAlmostEqual(float(self.probe(batch.parts[1].path)['format']['duration']), 2.25, delta=.2)
        self.assertLess(self.transferred, len(self.payloads['source.mp4']))
        self.assertFalse(self.unauthorized)

    def test_conversion_is_frame_accurate_without_a_second_network_download(self):
        tid, output = self.process(replace(self.options, video_codec='hevc'))
        probe = self.probe(output)
        self.assertAlmostEqual(float(probe['format']['duration']), 3, delta=.1)
        self.assertEqual(probe['streams'][0]['codec_name'], 'hevc')
        self.assertAlmostEqual(self.manager.engine._subtitle_context[tid]['start'], 20.5, delta=.001)
        def frame(path, start):
            return subprocess.check_output([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', str(start),
                '-i', str(path), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], **NO_WINDOW)
        actual, expected = frame(output, 0), frame(self.root / 'source.mp4', 20.52)
        self.assertEqual(len(actual), len(expected))
        self.assertLess(sum(abs(a - b) for a, b in zip(actual, expected)) / len(actual), 5)
        self.assertLess(self.transferred, len(self.payloads['source.mp4']) * .8)

    def test_audio_section_is_not_cut_twice(self):
        options = replace(self.options, output_mode='audio', container='mp3', audio_codec='mp3', trim_start='31', trim_end='33')
        tid, output = self.process(options)
        probe = self.probe(output)
        self.assertAlmostEqual(float(probe['format']['duration']), 2, delta=.1)
        self.assertEqual(probe['streams'][0]['codec_name'], 'mp3')
        self.assertAlmostEqual(self.manager.engine._subtitle_context[tid]['start'], 31, delta=.001)

    def test_manual_separate_video_audio_ids_are_preserved(self):
        options = replace(self.options, source_selection='manual', source_video_id='137', source_audio_id='140')
        tid, output = self.process(options, separate=True)
        probe = self.probe(output)
        self.assertEqual([stream['codec_name'] for stream in probe['streams']], ['h264', 'aac'])
        self.assertAlmostEqual(float(probe['format']['duration']), 3.5, delta=.15)
        self.assertEqual({name for name, _ in self.requests}, {'video.mp4', 'audio.m4a'})
        self.assertFalse(self.unauthorized)

    def test_start_only_and_end_only_sections_keep_the_original_timeline(self):
        for start, end, offset in (('118', '', 118), ('', '2', 0)):
            with self.subTest(start=start, end=end):
                options = replace(self.options, output_mode='audio', container='mp3', audio_codec='mp3',
                                  trim_start=start, trim_end=end)
                tid, output = self.process(options)
                self.assertAlmostEqual(float(self.probe(output)['format']['duration']), 2, delta=.1)
                self.assertAlmostEqual(self.manager.engine._subtitle_context[tid]['start'], offset, delta=.001)

    def test_limits_socks_and_cancel_never_fall_back_to_full_download(self):
        for changes in (dict(download_rate='1024'), dict(proxy_mode='custom', proxy_url='socks5://localhost:1080')):
            with self.subTest(changes=changes):
                task = asdict(replace(self.options, **changes))
                preparer = SourcePreparer(self.manager.engine)
                with patch.object(preparer, 'download_sources', side_effect=AssertionError('full fallback')):
                    with self.assertRaises(RuntimeError):
                        preparer.prepare('Tmissing', 1, self.base, self.info(), task, str(self.folder))
        tid = self.manager.add_urls([self.base], self.options)[0]
        self.manager.tid_to_workerid[tid] = 1
        with patch.object(self.manager.engine, '_download_from_info', side_effect=DownloadCancelled()), \
                patch.object(SourcePreparer, 'download_sources', side_effect=AssertionError('full fallback')):
            with self.assertRaises(DownloadCancelled):
                SourcePreparer(self.manager.engine).prepare(tid, 1, self.base, self.info(),
                    self.manager.get_task(tid), str(self.folder))
        self.assertFalse(self.requests)

    def test_cli_bounds_and_intermediate_metadata_are_explicit_and_do_not_mutate_source(self):
        args = YtDlpProcess(dict(download_section=(1.5, 2.5)))._arguments()
        self.assertEqual(args[args.index('--download-sections') + 1], '*1.500000000-2.500000000')
        self.assertIn('-copyts', args[args.index('--downloader-args') + 1])
        for value in ((-1, 2), (1, 1), (float('nan'), 2)):
            with self.assertRaises(ValueError):
                YtDlpProcess(dict(download_section=value))._arguments()
        info = self.info(separate=True)
        wrapped = SectionMetadata(info._media_info, ['140'])
        raw, _ = wrapped.snapshot()
        self.assertEqual(raw['formats'][0]['ext'], 'mp4')
        self.assertEqual(info._media_info.info['formats'][1]['ext'], 'm4a')
        with self.assertRaises(RuntimeError):
            SectionMetadata(info._media_info, ['missing']).snapshot()

    def test_ui_and_queue_preserve_method_while_old_queue_defaults_to_full(self):
        controls = OutputControls()
        self.assertEqual(controls.values()['trim_download_mode'], 'section')
        controls.restore({**DEFAULTS, 'trim_download_mode': 'full'})
        self.assertEqual(controls.values()['trim_download_mode'], 'full')
        self.manager.add_urls([self.base], self.options)
        _, document = self.manager.queue_snapshot()
        store = QueueStore(self.folder / 'queue.json')
        store.save(document)
        loaded, _ = store.load()
        self.assertEqual(loaded['tasks'][0]['options']['trim_download_mode'], 'section')
        loaded['tasks'][0]['options'].pop('trim_download_mode')
        store.save(loaded)
        loaded, _ = store.load()
        self.assertEqual(loaded['tasks'][0]['options']['trim_download_mode'], 'full')
