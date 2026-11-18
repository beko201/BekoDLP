from queue import Queue, Empty
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Timed out waiting for shutdown state')


class ShutdownTests(unittest.TestCase):
    def make_app(self):
        manager = downloader.TaskManager()
        self.addCleanup(manager.close)
        return manager


    def test_stopped_worker_does_not_start_dequeued_task(self):
        for closing in (False, True):
            with self.subTest(closing=closing):
                app = self.make_app()
                entered, release = threading.Event(), threading.Event()

                class GatedQueue(Queue):
                    def get(self, *args, **kwargs):
                        entered.set()
                        release.wait(2)
                        return super().get(*args, **kwargs)

                queue = GatedQueue()
                queue.put(('T1', 'url'))
                app._process_task = Mock()
                worker = downloader.Worker(app, queue, 1)
                worker.start()
                self.assertTrue(entered.wait(2))
                worker.stop()
                if closing:
                    app._shutdown_event.set()
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                app._process_task.assert_not_called()
                self.assertEqual(queue.unfinished_tasks, 0 if closing else 1)
                self.assertEqual(queue.qsize(), 0 if closing else 1)

    def test_metadata_retry_backoff_is_interrupted_by_cancel(self):
        cancel = threading.Event()
        attempted = threading.Event()
        errors = []

        def extract(*args, **kwargs):
            attempted.set()
            raise OSError('network failure')

        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        fake.extract_info.side_effect = extract

        def run():
            try:
                downloader.fetch_ytinfo_with_retry('url', cancel_event=cancel)
            except BaseException as error:
                errors.append(error)

        with patch.object(downloader.yt_dlp, 'YoutubeDL', return_value=fake):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.assertTrue(attempted.wait(2))
            cancel.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], downloader.DownloadCancelled)
        fake.extract_info.assert_called_once()

    def test_late_playlist_result_does_not_enqueue_after_close(self):
        app = self.make_app()
        entered = threading.Event()

        def resolve(*args, cancel_event, **kwargs):
            entered.set()
            cancel_event.wait(2)
            return ['https://example.com/video']

        with patch.object(downloader, '_extract_urls_from_input', side_effect=resolve):
            options = downloader.TaskOptions(folder='.', quality='480p')
            fetcher = downloader.PlaylistFetcher(app, 'url', options)
            app.start_thread(fetcher)
            self.assertTrue(entered.wait(2))
            app.close()
            wait_until(app.is_idle)
            self.assertTrue(app.finish_shutdown())
            self.assertFalse(fetcher.is_alive())
        self.assertEqual(app.list_tasks(), [])


if __name__ == '__main__':
    unittest.main()
