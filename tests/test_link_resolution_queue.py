import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import download_engine
from task_manager import LINK_RESOLVER_WORKERS, TaskManager, TaskOptions


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Link resolution did not finish in time')
        time.sleep(0.01)


class LinkResolutionQueueTests(unittest.TestCase):
    def make_manager(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())

        def cleanup():
            manager.close()
            wait_until(manager.is_idle)
            self.assertTrue(manager.finish_shutdown())

        self.addCleanup(cleanup)
        return manager

    def test_large_batch_limits_concurrency_and_preserves_each_tasks_options(self):
        manager = self.make_manager()
        release = threading.Event()
        self.addCleanup(release.set)
        state_lock = threading.Lock()
        active = peak = 0
        worker_ids = set()

        def resolve(url, expand_playlist, notes, **kwargs):
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
                worker_ids.add(threading.get_ident())
            try:
                if not release.wait(5):
                    raise RuntimeError('Test did not release the resolvers')
                return [url]
            finally:
                with state_lock:
                    active -= 1

        first = TaskOptions(folder='.', format='MP3', mp3_bitrate='128k')
        second = TaskOptions(folder='.', format='MP3', mp3_bitrate='320k')
        with patch.object(download_engine, '_extract_urls_from_input', side_effect=resolve):
            for index in range(200):
                self.assertTrue(manager.submit_link(f'https://example.com/{index}',
                                                    first if index % 2 == 0 else second, True))
            wait_until(lambda: active == LINK_RESOLVER_WORKERS)
            self.assertEqual(len(manager.background_threads), LINK_RESOLVER_WORKERS)
            self.assertEqual(manager.link_queue.qsize(), 200 - LINK_RESOLVER_WORKERS)
            release.set()
            wait_until(lambda: len(manager.list_tasks()) == 200)
            wait_until(lambda: manager.link_queue.unfinished_tasks == 0)
        self.assertEqual(peak, LINK_RESOLVER_WORKERS)
        self.assertEqual(len(worker_ids), LINK_RESOLVER_WORKERS)
        for task in manager.list_tasks():
            index = int(task['url'].rsplit('/', 1)[1])
            self.assertEqual(task['mp3_bitrate'], '128k' if index % 2 == 0 else '320k')

    def test_close_discards_pending_links_and_waits_for_active_resolvers(self):
        manager = self.make_manager()
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []
        calls_lock = threading.Lock()

        def resolve(url, *args, **kwargs):
            with calls_lock:
                calls.append(url)
            release.wait(5)
            return [url]  # Even a late successful result must be discarded.

        options = TaskOptions(folder='.')
        with patch.object(download_engine, '_extract_urls_from_input', side_effect=resolve):
            for index in range(50):
                manager.submit_link(f'https://example.com/{index}', options)
            wait_until(lambda: len(calls) == LINK_RESOLVER_WORKERS)
            manager.close()
            self.assertTrue(manager.link_queue.empty())
            self.assertIsNone(manager.submit_link('https://example.com/late', options))
            self.assertFalse(manager.is_idle())
            self.assertFalse(manager.finish_shutdown())
            release.set()
            wait_until(manager.is_idle)
        self.assertEqual(len(calls), LINK_RESOLVER_WORKERS)
        self.assertEqual(manager.list_tasks(), [])
        self.assertEqual(manager.link_queue.unfinished_tasks, 0)
        self.assertTrue(manager.finish_shutdown())

    def test_failed_resolution_does_not_prevent_remaining_links_from_running(self):
        manager = self.make_manager()
        options = TaskOptions(folder='.')

        def resolve(url, *args, **kwargs):
            if url.endswith('/bad'):
                raise RuntimeError('extractor failed')
            return [url]

        with patch.object(download_engine, '_extract_urls_from_input', side_effect=resolve), \
                patch('task_manager.log_error'):
            manager.submit_link('https://example.com/bad', options)
            for index in range(12):
                manager.submit_link(f'https://example.com/{index}', options)
            wait_until(lambda: len(manager.list_tasks()) == 12)
            wait_until(lambda: manager.link_queue.unfinished_tasks == 0)
        errors = []
        while not manager.events.empty():
            event = manager.events.get_nowait()
            if event.kind == 'error':
                errors.append(event)
        self.assertEqual(len(errors), 1)
        self.assertIn('extractor failed', errors[0].data['message'])

    def test_pending_aliases_and_active_urls_are_deduplicated_before_resolution(self):
        manager = self.make_manager()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        options = TaskOptions(folder='.')
        canonical = 'https://www.youtube.com/watch?v=example'
        def resolve(*args, **kwargs):
            entered.set()
            release.wait(3)
            return [canonical]
        with patch.object(download_engine, '_extract_urls_from_input', side_effect=resolve) as extract:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda i: manager.submit_link(
                    'https://youtu.be/example' if i % 2 else canonical, options), range(100)))
            self.assertEqual(sum(bool(result) for result in results), 1)
            self.assertTrue(entered.wait(2))
            release.set()
            wait_until(lambda: manager.link_queue.unfinished_tasks == 0)
            self.assertFalse(manager.submit_link(canonical, options, True))
            self.assertEqual(extract.call_count, 1)
            self.assertFalse(manager._pending_link_keys)

    def test_different_playlists_are_not_confused_with_their_active_selected_video(self):
        manager = self.make_manager()
        options = TaskOptions(folder='.')
        video = 'https://www.youtube.com/watch?v=example'
        manager.add_urls([video], options)
        release = threading.Event()
        self.addCleanup(release.set)
        def resolve(*args, **kwargs):
            release.wait(3)
            return []
        with patch.object(download_engine, '_extract_urls_from_input', side_effect=resolve) as extract:
            self.assertTrue(manager.submit_link(video + '&list=PLA', options, True))
            self.assertTrue(manager.submit_link(video + '&list=PLB', options, True))
            self.assertFalse(manager.submit_link('https://www.youtube.com/playlist?list=PLA', options, True))
            self.assertFalse(manager.submit_link(video, options, False))
            release.set()
            wait_until(lambda: manager.link_queue.unfinished_tasks == 0)
        self.assertEqual(extract.call_count, 2)

    def test_failed_resolution_releases_its_reservation_for_resubmission(self):
        manager = self.make_manager()
        options = TaskOptions(folder='.')
        url = 'https://example.com/retry'
        with patch.object(download_engine, '_extract_urls_from_input', side_effect=RuntimeError('offline')) as extract, \
                patch('task_manager.log_error'):
            self.assertTrue(manager.submit_link(url, options))
            wait_until(lambda: manager.link_queue.unfinished_tasks == 0)
            self.assertFalse(manager._pending_link_keys)
            extract.side_effect = None
            extract.return_value = [url]
            self.assertTrue(manager.submit_link(url, options))
            wait_until(lambda: manager.link_queue.unfinished_tasks == 0)
        self.assertEqual(manager.summary().total, 1)


if __name__ == '__main__':
    unittest.main()
