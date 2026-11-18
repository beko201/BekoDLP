import importlib.util
import atexit
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


from component_support import downloader
_TEST_LOGS = tempfile.TemporaryDirectory()
atexit.register(_TEST_LOGS.cleanup)
downloader.ERROR_LOG_FILE = str(Path(_TEST_LOGS.name, 'errors.log'))
downloader.FALLBACK_ERROR_LOG_FILE = str(Path(_TEST_LOGS.name, 'fallback.log'))


class TaskCleanupTests(unittest.TestCase):
    def make_app(self, folder, fmt='MP3', codec='Original (Automatic)'):
        app = downloader.ComponentHarness.__new__(downloader.ComponentHarness)
        app.tasks_lock = threading.Lock()
        app._closing = False
        app._shutdown_event = threading.Event()
        app.background_threads = set()
        app.tasks = {'T1': {'url': 'https://example.com/video', 'folder': str(folder),
                            'format': fmt, 'quality': '1080p', 'codec_mode': codec,
                            'status': 'Queued', 'output_path': None}}
        app.tid_to_workerid = {}
        app.cancel_events = {'T1': threading.Event()}
        app.active_urls = {'https://example.com/video'}
        app.manager._active_url_owners = {'https://example.com/video': 'T1'}
        app.reserved_output_paths = {}
        app.stream_progress = {}
        app.download_speed_history = {}
        app.ffmpeg = 'test-ffmpeg'
        app.ffprobe = 'test-ffprobe'
        app.mp3_bitrate_var = Mock(get=lambda: '192k')
        app._notify = Mock()
        app._fetch_and_set_thumbnail = Mock()
        app._update_queue_label = Mock()
        app._ffprobe_duration = Mock(return_value=1)
        # These lifecycle tests use placeholder bytes rather than real media.
        app._ffprobe_media = Mock(return_value={
            'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': '1',
                       'tags': {'major_brand': 'isom'}},
            'streams': [{'codec_type': 'video', 'codec_name': 'h264'},
                        {'codec_type': 'audio', 'codec_name': 'aac'}],
        })

        def update(tid, **values):
            app.tasks[tid].update(values)

        app._task_update = update
        app._task_error = Mock(side_effect=lambda tid, message: update(tid, status='Error: ' + message))

        def download(tid, key, url, stream, filename, *, folder, stop_event=None):
            path = Path(folder, filename)
            path.write_bytes(b'downloaded stream')
            return str(path)

        app._download_format = download
        return app

    def info(self, mode):
        audio = {'format_id': 'a', 'ext': 'm4a', 'acodec': 'aac', 'vcodec': 'none'}
        video = {'format_id': 'v', 'ext': 'mp4', 'acodec': 'none', 'vcodec': 'avc1', 'height': 1080}
        formats = [audio] if mode == 'mp3' else [audio, video]
        if mode == 'progressive':
            formats = [{**video, 'acodec': 'aac'}]
        return downloader.YTInfo('https://example.com/video', {'title': 'Example', 'formats': formats})

    def test_failure_and_cancellation_clean_outputs_and_preserve_existing_files(self):
        for mode in ('mp3', 'merge', 'progressive'):
            for codec in ('Original (Automatic)', 'H.264 (Compatibility)'):
                for error_type in (RuntimeError, downloader.DownloadCancelled):
                    with self.subTest(mode=mode, codec=codec, error=error_type.__name__):
                        with tempfile.TemporaryDirectory() as temp:
                            folder = Path(temp)
                            fmt = 'MP3' if mode == 'mp3' else 'MP4'
                            app = self.make_app(folder, fmt, codec)
                            original = folder / ('Example_T1.mp4' if mode == 'progressive'
                                                 else 'Example.mp3' if mode == 'mp3' else 'Example.mp4')
                            original.write_bytes(b'existing user file')
                            unrelated = folder / 'unrelated.mp4'
                            unrelated.write_bytes(b'keep this too')

                            def fail_output(path):
                                path = Path(path)
                                path.write_bytes(b'partial output')
                                Path(str(path) + '.part').write_bytes(b'partial fragment')
                                Path(str(path) + '.ytdl').write_bytes(b'resume metadata')
                                raise error_type('simulated interruption')

                            if mode == 'progressive':
                                app._download_format = lambda *args, folder, **kwargs: fail_output(Path(folder, args[4]))
                            else:
                                app._run_ffmpeg = lambda tid, wid, cmd, *args, **kwargs: fail_output(cmd[-1])

                            with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=self.info(mode)):
                                app._process_task(('T1', 'https://example.com/video'), 1)

                            self.assertEqual({p.name for p in folder.iterdir()}, {original.name, unrelated.name})
                            self.assertEqual(original.read_bytes(), b'existing user file')
                            self.assertEqual(unrelated.read_bytes(), b'keep this too')
                            status = app.tasks['T1']['status']
                            if error_type is downloader.DownloadCancelled:
                                self.assertEqual(status, 'Cancelled')
                                app._task_error.assert_not_called()
                            else:
                                self.assertTrue(status.startswith('Error:'))
                                app._task_error.assert_called_once()
                            self.assertIsNone(app.tasks['T1']['output_path'])
                            self.assertEqual(app.reserved_output_paths, {})
                            self.assertEqual(app.active_urls, set())

    def test_successful_outputs_survive_cleanup(self):
        for mode in ('mp3', 'merge', 'progressive'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                app = self.make_app(folder, 'MP3' if mode == 'mp3' else 'MP4')

                def convert(tid, wid, cmd, *args, **kwargs):
                    Path(cmd[-1]).write_bytes(b'complete output')
                    return 0

                app._run_ffmpeg = convert
                with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=self.info(mode)):
                    app._process_task(('T1', 'https://example.com/video'), 1)
                app._task_error.assert_not_called()
                self.assertTrue(app.tasks['T1']['status'].startswith('Completed'))
                output = Path(app.tasks['T1']['output_path'])
                self.assertTrue(output.is_file())
                self.assertEqual(list(folder.iterdir()), [output])

    def test_failed_merge_retry_cleans_final_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app = self.make_app(folder, 'MP4')
            attempts = []

            def convert(tid, wid, cmd, *args, **kwargs):
                Path(cmd[-1]).write_bytes(b'failed encode')
                attempts.append(cmd)
                if len(attempts) == 1:
                    return 1
                raise RuntimeError('fallback encoding failed')

            app._run_ffmpeg = convert
            with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=self.info('merge')):
                app._process_task(('T1', 'https://example.com/video'), 1)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(list(folder.iterdir()), [])
            app._task_error.assert_called_once()

    def test_failed_path_reservation_does_not_delete_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            original = folder / 'Example.mp4'
            original.write_bytes(b'original')
            app = self.make_app(folder, 'MP4')
            app._dedupe_path = Mock(side_effect=OSError('cannot reserve output'))
            with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=self.info('merge')):
                app._process_task(('T1', 'https://example.com/video'), 1)
            self.assertEqual(original.read_bytes(), b'original')
            self.assertEqual(list(folder.iterdir()), [original])

    def test_locked_output_is_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app = self.make_app(folder)
            actual_remove = downloader.os.remove
            attempts = []

            def remove(path):
                if Path(path).name == 'Example.mp3':
                    attempts.append(path)
                    if len(attempts) == 1:
                        raise PermissionError('temporarily locked')
                return actual_remove(path)

            def fail(tid, wid, cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b'incomplete')
                raise RuntimeError('conversion failed')

            app._run_ffmpeg = fail
            with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=self.info('mp3')):
                with patch.object(downloader.os, 'remove', side_effect=remove):
                    app._process_task(('T1', 'https://example.com/video'), 1)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
