from contextlib import nullcontext
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import download_engine
from task_manager import TaskManager, TaskOptions
from utils import DownloadCancelled


class ParallelDownloadFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.manager = TaskManager()
        self.tid = self.manager.add_urls(
            ['https://example.com/video'], TaskOptions(folder=str(self.folder)))[0]
        self.manager.tid_to_workerid[self.tid] = 1
        self.engine = self.manager.engine
        self.info = download_engine.YTInfo('url', {'title': 'Example', 'formats': [
            {'format_id': 'video', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'height': 1080},
            {'format_id': 'audio', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac'},
        ]})

    def group(self):
        return self.engine._download_streams_parallel(
            self.tid, 'url', self.info.video_only_formats()[0], self.info.audio_only_formats()[0],
            'video.mp4', 'audio.m4a', folder=str(self.folder))

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
            self.manager._shutdown_event.set()
            self.manager.paused = False
            thread.join(3)

        self.addCleanup(cleanup)
        return thread, results

    def test_first_failure_stops_peer_during_progress_pause_and_retry(self):
        for failed_stream in ('video', 'audio'):
            for mode in ('progress', 'paused', 'retry'):
                with self.subTest(failed_stream=failed_stream, mode=mode):
                    ready, stopped = threading.Event(), threading.Event()
                    original_error = OSError(f'{failed_stream} download failed')
                    self.manager.paused = False
                    both_started = threading.Barrier(2)
                    unrelated = self.folder / 'unrelated.mp4'
                    unrelated.write_bytes(b'preserve')

                    def fake_ydl(options, **kwargs):
                        def download(urls):
                            path = Path(options['outtmpl'])
                            with Path(str(path) + '.part').open('wb') as stream:
                                stream.write(b'partial')
                                if mode == 'paused':
                                    both_started.wait(2)
                                if options['format'] == failed_stream:
                                    if not ready.wait(1):
                                        raise RuntimeError('peer did not start')
                                    raise original_error
                                if mode == 'paused':
                                    self.manager.paused = True
                                ready.set()
                                try:
                                    while True:
                                        if mode == 'retry':
                                            options['cancel_check']()
                                        else:
                                            options['progress_hooks'][0]({'status': 'downloading'})
                                        time.sleep(0.01)
                                except DownloadCancelled:
                                    stopped.set()
                                    raise

                        return nullcontext(type('FakeYDL', (), {'download': staticmethod(download)})())

                    with patch.object(download_engine, 'YtDlpProcess', side_effect=fake_ydl):
                        thread, results = self.run_async(self.group)
                        thread.join(1.5)
                        try:
                            self.assertFalse(thread.is_alive(), 'peer kept downloading after failure')
                            self.assertTrue(stopped.is_set())
                            self.assertIs(results[0], original_error)
                            self.assertFalse(self.manager.is_cancelled(self.tid))
                            self.assertEqual(list(self.folder.iterdir()), [unrelated])
                        finally:
                            if thread.is_alive():
                                self.manager.cancel_events[self.tid].set()
                                self.manager.paused = False
                                thread.join(2)
                                self.manager.cancel_events[self.tid].clear()
                    self.manager.paused = False

    def test_new_group_succeeds_after_failure_without_cancelling_task(self):
        fail = True

        def fake_ydl(options, **kwargs):
            def download(urls):
                if fail:
                    raise OSError('first attempt failed')
                Path(options['outtmpl']).write_bytes(options['format'].encode())
            return nullcontext(type('FakeYDL', (), {'download': staticmethod(download)})())

        with patch.object(download_engine, 'YtDlpProcess', side_effect=fake_ydl):
            with self.assertRaisesRegex(OSError, 'first attempt failed'):
                self.group()
            self.assertFalse(self.manager.is_cancelled(self.tid))
            fail = False
            video, audio = self.group()
        self.assertEqual(Path(video).read_bytes(), b'video')
        self.assertEqual(Path(audio).read_bytes(), b'audio')
        self.assertFalse(self.manager.is_cancelled(self.tid))

    def test_stream_failure_is_reported_as_error_and_can_be_retried(self):
        def fake_ydl(options, **kwargs):
            def download(urls):
                raise OSError('original download error')
            return nullcontext(type('FakeYDL', (), {'download': staticmethod(download)})())

        with patch.object(download_engine, 'YtDlpProcess', side_effect=fake_ydl), \
                patch.object(download_engine, 'fetch_ytinfo_with_retry', return_value=self.info), \
                patch.object(self.manager, '_log_task_error') as log_error:
            self.manager._process_task((self.tid, 'url'), 1)
        self.assertEqual(self.manager.get_task(self.tid)['status'], 'Error: original download error')
        log_error.assert_called_once_with(self.tid, 'original download error')
        self.assertEqual(list(self.folder.iterdir()), [])
        self.manager.retry_failed()
        self.assertEqual(self.manager.get_task(self.tid)['status'], 'Queued')
        self.assertFalse(self.manager.is_cancelled(self.tid))

    def test_user_cancel_stops_both_streams_and_preserves_cancel_status(self):
        ready = threading.Barrier(3)

        def fake_ydl(options, **kwargs):
            def download(urls):
                with Path(options['outtmpl'] + '.part').open('wb'):
                    ready.wait(2)
                    while True:
                        options['progress_hooks'][0]({'status': 'downloading'})
                        time.sleep(0.01)
            return nullcontext(type('FakeYDL', (), {'download': staticmethod(download)})())

        with patch.object(download_engine, 'YtDlpProcess', side_effect=fake_ydl):
            thread, results = self.run_async(self.group)
            ready.wait(2)
            self.manager.cancel_task(self.tid)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], DownloadCancelled)
        self.assertTrue(self.manager.is_cancelled(self.tid))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_second_thread_launch_failure_waits_for_first_before_cleanup(self):
        real_thread = threading.Thread
        launch_error = RuntimeError("can't start new thread")
        opened, signalled, release = threading.Event(), threading.Event(), threading.Event()
        created, results = [], []
        partial = self.folder / 'video.mp4.part'
        unrelated = self.folder / 'unrelated.mp4'
        unrelated.write_bytes(b'preserve')

        def download(tid, key, url, stream, filename, *, folder, stop_event=None):
            with partial.open('wb') as output:
                output.write(b'partial')
                output.flush()
                opened.set()
                if not stop_event.wait(3):
                    raise AssertionError('Launch failure did not signal the running peer')
                signalled.set()
                release.wait(3)
            raise DownloadCancelled()

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            if len(created) == 2:
                thread.start = Mock(side_effect=launch_error)
            else:
                start = thread.start

                def start_and_wait():
                    start()
                    if not opened.wait(2):
                        raise AssertionError('First stream did not open its file')

                thread.start = start_and_wait
            return thread

        def run():
            try:
                self.group()
            except BaseException as error:
                results.append(error)

        operation = real_thread(target=run, daemon=True)
        cleanup = download_engine.remove_leftovers

        def checked_cleanup(*args, **kwargs):
            self.assertFalse(created[0].is_alive(), 'Cleanup raced with an active stream')
            return cleanup(*args, **kwargs)

        with patch.object(download_engine.threading, 'Thread', side_effect=make_thread), \
                patch.object(self.engine, '_download_format', side_effect=download), \
                patch.object(download_engine, 'remove_leftovers', side_effect=checked_cleanup) as cleaned:
            try:
                operation.start()
                self.assertTrue(signalled.wait(2))
                self.assertTrue(operation.is_alive(), 'Task returned before its peer finished')
                self.assertTrue(partial.exists())
                cleaned.assert_not_called()
            finally:
                release.set()
                operation.join(4)
        self.assertFalse(operation.is_alive())
        self.assertFalse(created[0].is_alive())
        self.assertIsNone(created[1].ident)
        self.assertEqual(results, [launch_error])
        self.assertFalse(self.manager.is_cancelled(self.tid))
        self.assertEqual(list(self.folder.iterdir()), [unrelated])

    def test_first_thread_launch_failure_starts_no_download_and_allows_retry(self):
        real_thread = threading.Thread
        launch_error = RuntimeError("can't start new thread")
        created = []

        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            thread.start = Mock(side_effect=launch_error)
            created.append(thread)
            return thread

        with patch.object(download_engine.threading, 'Thread', side_effect=make_thread), \
                patch.object(self.engine, '_download_format') as download:
            with self.assertRaises(RuntimeError) as caught:
                self.group()
            self.assertIs(caught.exception, launch_error)
            download.assert_not_called()
        self.assertTrue(all(thread.ident is None for thread in created))
        created[1].start.assert_not_called()
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertFalse(self.manager.is_cancelled(self.tid))

        def successful_download(tid, key, url, stream, filename, *, folder, stop_event=None):
            self.assertFalse(stop_event.is_set())
            path = Path(folder, filename)
            path.write_bytes(key.encode())
            return str(path)

        with patch.object(self.engine, '_download_format', side_effect=successful_download):
            video, audio = self.group()
        self.assertEqual(Path(video).read_bytes(), b'video')
        self.assertEqual(Path(audio).read_bytes(), b'audio')


if __name__ == '__main__':
    unittest.main()
