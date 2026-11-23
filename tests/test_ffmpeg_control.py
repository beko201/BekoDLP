import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


from component_support import downloader
FFMPEG = shutil.which('ffmpeg')


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('Timed out waiting for process state')


@unittest.skipUnless(FFMPEG, 'ffmpeg is required')
class FFmpegControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        log_patch = patch.object(downloader, 'ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.app = downloader.ComponentHarness.__new__(downloader.ComponentHarness)
        self.app.tasks_lock = threading.Lock()
        self.app._closing = False
        self.app._shutdown_event = threading.Event()
        self.app.current_process_pids = {}
        self.app.cancel_events = {'T1': threading.Event()}
        self.app.tid_to_workerid = {'T1': 1}
        self.app.tasks = {'T1': {'status': 'Worker1: processing', 'title': 'test'}}
        self.app.reserved_output_paths = {}
        self.app.paused = False
        self.app.pause_button = Mock()
        self.app.ffmpeg = FFMPEG
        self.app.ffprobe = shutil.which('ffprobe')
        self.app.mp3_bitrate_var = SimpleNamespace(get=lambda: '192k')
        self.app._notify = Mock()
        self.updates = []

        def update(tid, **values):
            self.app.tasks[tid].update(values)
            self.updates.append(values)

        self.app._task_update = update

    def run_async(self, operation):
        results = []

        def run():
            try:
                results.append(operation())
            except BaseException as error:
                results.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def cleanup():
            self.app.cancel_events['T1'].set()
            self.app.paused = False
            thread.join(5)

        self.addCleanup(cleanup)
        return thread, results

    def long_command(self):
        # Quiet output verifies that control does not depend on progress lines.
        return [FFMPEG, '-loglevel', 'quiet', '-re', '-f', 'lavfi',
                '-i', 'sine=frequency=440', '-t', '30', '-f', 'null', '-']

    def make_audio(self, name='source.wav'):
        target = self.folder / name
        subprocess.run([FFMPEG, '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', str(target)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       **downloader.NO_WINDOW)
        return target

    def test_pause_resume_and_cancel_quiet_process(self):
        thread, results = self.run_async(
            lambda: self.app._run_ffmpeg('T1', 1, self.long_command()))
        wait_until(lambda: 1 in self.app.current_process_pids)
        pid = self.app.current_process_pids[1]
        self.app.toggle_pause()
        wait_until(lambda: any(v.get('status') == 'Paused' for v in self.updates))
        process = downloader.psutil.Process(pid)
        before = process.cpu_times()
        time.sleep(0.25)
        after = process.cpu_times()
        self.assertAlmostEqual(before.user + before.system, after.user + after.system, delta=0.02)
        self.app.toggle_pause()
        wait_until(lambda: self.app.tasks['T1']['status'] == 'Processing')
        self.app.toggle_pause()
        wait_until(lambda: self.app.tasks['T1']['status'] == 'Paused')
        self.assertTrue(self.app._cancel_task_by_tid('T1'))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], downloader.DownloadCancelled)
        self.assertEqual(self.app.current_process_pids, {})
        self.assertFalse(downloader.psutil.pid_exists(pid))

    def test_peer_failure_signal_stops_quiet_ffmpeg_without_user_cancellation(self):
        stop_event = threading.Event()
        thread, results = self.run_async(
            lambda: self.app._run_ffmpeg('T1', 1, self.long_command(), stop_event=stop_event))
        wait_until(lambda: 1 in self.app.current_process_pids)
        pid = self.app.current_process_pids[1]
        stop_event.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], downloader.DownloadCancelled)
        self.assertFalse(self.app.manager.is_cancelled('T1'))
        self.assertEqual(self.app.current_process_pids, {})
        self.assertEqual(self.app.engine.managed_processes, set())
        self.assertFalse(downloader.psutil.pid_exists(pid))

    def test_peer_failure_signal_prevents_ffmpeg_launch_while_paused(self):
        stop_event = threading.Event()
        stop_event.set()
        self.app.paused = True
        with patch.object(downloader.subprocess, 'Popen') as launch:
            with self.assertRaises(downloader.DownloadCancelled):
                self.app._run_ffmpeg('T1', 1, self.long_command(), stop_event=stop_event)
        launch.assert_not_called()
        self.assertFalse(self.app.manager.is_cancelled('T1'))
        self.assertEqual(self.app.current_process_pids, {})

    def test_cancel_does_not_need_process_suspension(self):
        with patch.object(downloader.psutil, 'Process', side_effect=AssertionError('unexpected suspension')):
            thread, results = self.run_async(
                lambda: self.app._run_ffmpeg('T1', 1, self.long_command()))
            wait_until(lambda: 1 in self.app.current_process_pids)
            self.app._cancel_task_by_tid('T1')
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(results[0], downloader.DownloadCancelled)
            self.assertEqual(self.app.current_process_pids, {})

    def test_cancel_before_launch_while_paused(self):
        self.app.paused = True
        with patch.object(downloader.subprocess, 'Popen') as popen:
            thread, results = self.run_async(
                lambda: self.app._run_ffmpeg('T1', 1, self.long_command()))
            time.sleep(0.15)
            popen.assert_not_called()
            self.app.cancel_events['T1'].set()
            thread.join(5)
            self.assertIsInstance(results[0], downloader.DownloadCancelled)
            popen.assert_not_called()

    def test_mp3_conversion_and_trim(self):
        source = self.make_audio()
        output = self.folder / 'output.mp3'
        self.app._ffmpeg_convert_to_mp3('T1', 1, str(source), str(output))
        self.assertGreater(output.stat().st_size, 0)
        self.app._maybe_trim('T1', 1, str(output), '0.1', '0.5')
        self.assertGreater(output.stat().st_size, 0)
        self.assertFalse((self.folder / 'output_trim.mp3').exists())
        self.assertEqual(self.app.current_process_pids, {})

    def test_trim_cancellation_propagates_and_removes_partial_output(self):
        source = self.make_audio()
        original = source.read_bytes()

        def cancelled(tid, wid, cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b'partial trim')
            raise downloader.DownloadCancelled()

        self.app._run_ffmpeg = cancelled
        with self.assertRaises(downloader.DownloadCancelled):
            self.app._maybe_trim('T1', 1, str(source), '0.1', '0.5')
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.folder / 'source_trim.wav').exists())

    def test_real_mp3_trim_beyond_duration_preserves_playable_original(self):
        source = self.make_audio('source.mp3')
        original = source.read_bytes()
        # Stale extractor metadata must not override the actual file duration.
        self.app.tasks['T1']['duration'] = 100
        self.app._maybe_trim('T1', 1, str(source), '10', '12')
        self.assertEqual(source.read_bytes(), original)
        self.assertGreater(self.app._ffprobe_duration(str(source), self.app.ffprobe), 0)
        self.assertIn('Trimming could not be applied', self.app.tasks['T1']['warnings'][0])
        self.assertFalse((self.folder / 'source_trim.mp3').exists())
        self.assertEqual(self.app.reserved_output_paths, {})

    def test_process_failure_clears_registration(self):
        with self.assertRaises(RuntimeError):
            self.app._run_ffmpeg('T1', 1, [FFMPEG, '-invalid-test-option'])
        self.assertEqual(self.app.current_process_pids, {})
        code = self.app._run_ffmpeg('T1', 1, [FFMPEG, '-invalid-test-option'], check=False)
        self.assertNotEqual(code, 0)

    def test_ytdlp_merger_runs_in_registered_process_tree(self):
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        video = self.folder / 'video.mp4'
        audio = self.make_audio('audio.wav')
        output = self.folder / 'merged.mkv'
        subprocess.run([FFMPEG, '-y', '-f', 'lavfi', '-i',
                        'color=c=black:s=32x32:d=1', '-an', '-c:v', 'libx264', str(video)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       **downloader.NO_WINDOW)
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(self.folder)))
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        info = {'id': 'merge', 'title': 'Merge', 'duration': 1,
                'formats': [
                    {'format_id': 'video', 'url': base + '/video.mp4', 'ext': 'mp4',
                     'vcodec': 'h264', 'acodec': 'none', 'height': 32},
                    {'format_id': 'audio', 'url': base + '/audio.wav', 'ext': 'wav',
                     'vcodec': 'none', 'acodec': 'pcm_s16le'}]}
        try:
            with patch.object(self.app.engine, '_register_process', wraps=self.app.engine._register_process) as register:
                with self.app.engine.ytdlp_process({'format': 'bv+ba', 'merge_output_format': 'mkv',
                        'outtmpl': str(output)}, 'T1', folder=str(self.folder)) as ydl:
                    result = ydl.process_ie_result(info)
            self.assertEqual(result['filepath'], str(output))
            self.assertGreater(output.stat().st_size, 0)
            register.assert_called_once()
            self.assertEqual(self.app.engine.managed_processes, set())
            self.assertEqual(self.app.current_process_pids, {})
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
