"""Pending metadata eviction must fall back to normal, cancellable extraction."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import download_engine
from resolved_info_cache import ResolvedInfoCache, metadata_size
from task_manager import TaskManager, TaskOptions
from utils import DownloadCancelled


class ResolvedInfoCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.cache = ResolvedInfoCache(max_entries=2, max_bytes=4096, ttl=5, clock=lambda: self.now)

    def put(self, tid, info):
        self.cache.put(tid, info, self.cache.prepare(info))

    def test_count_evicts_oldest_and_take_releases_budget(self):
        for index in range(3):
            self.put(str(index), {'title': str(index)})
        self.assertEqual(len(self.cache), 2)
        self.assertIsNone(self.cache.pop('0'))
        self.assertEqual(self.cache.pop('1'), {'title': '1'})
        self.cache.pop('2')
        self.assertEqual(self.cache.size_bytes, 0)

    def test_byte_budget_applies_before_count_and_rejects_oversize(self):
        self.cache.max_entries = 100
        for index in range(10):
            self.put(str(index), {'payload': str(index) * 2000})
            self.assertLessEqual(self.cache.size_bytes, self.cache.max_bytes)
        self.assertEqual(len(self.cache), 1)
        self.put('oversize', {'payload': 'x' * 10000})
        self.assertIsNone(self.cache.pop('oversize'))
        self.assertEqual(len(self.cache), 1)

    def test_expired_records_are_not_returned_and_idle_prune_releases_them(self):
        self.put('a', {'title': 'a'})
        self.now += 1
        self.put('b', {'title': 'b'})
        self.now = 15
        self.assertIsNone(self.cache.pop('a'))
        self.now = 16
        self.cache.prune()
        self.assertEqual(len(self.cache), 0)
        self.assertEqual(self.cache.size_bytes, 0)

    def test_replacement_expiry_and_clear_accounting(self):
        self.put('a', {'title': 'old'})
        self.put('b', {'title': 'other'})
        self.now += 2
        self.put('a', {'title': 'new'})
        self.now = 15
        self.cache.prune()
        self.assertEqual(len(self.cache), 1)
        self.assertEqual(self.cache.pop('a'), {'title': 'new'})
        self.put('c', {})
        self.cache.clear()
        self.assertEqual(len(self.cache), 0)
        self.assertEqual(self.cache.size_bytes, 0)

    def test_estimate_handles_shared_references_cycles_and_bounds_traversal(self):
        shared = 'x' * 1000
        cyclic = [shared, shared]
        cyclic.append(cyclic)
        self.assertEqual(metadata_size(cyclic, 10000), sys.getsizeof(cyclic) + sys.getsizeof(shared))
        self.assertIsNone(metadata_size({'opaque': object()}, 10000))
        self.assertIsNone(metadata_size(list(range(50)), 10000, max_nodes=10))

    def test_manager_expires_metadata_without_changing_task_or_active_owner(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        manager._resolved_info = self.cache
        tid = manager.add_urls(['https://example.com/a'], TaskOptions(folder='.'),
                               resolved_info={'https://example.com/a': {'title': 'a'}})[0]
        self.now = 16
        manager.has_ui_work()
        self.assertEqual(len(self.cache), 0)
        self.assertIsNotNone(manager.get_task(tid))
        self.assertIn('https://example.com/a', manager.active_urls)

    def test_cache_miss_uses_normal_extraction_with_cancellation_and_error_handling(self):
        for error in (DownloadCancelled(), RuntimeError('offline extraction failure')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder, \
                    patch('utils.ERROR_LOG_FILE', str(Path(folder, 'errors.log'))):
                manager = TaskManager()
                manager._resolved_info = ResolvedInfoCache(max_entries=1)
                first, _ = [manager.add_urls([url], TaskOptions(folder=folder),
                                            resolved_info={url: {'title': url}})[0]
                            for url in ('https://example.com/a', 'https://example.com/b')]
                with patch.object(download_engine, 'fetch_ytinfo_with_retry', side_effect=error) as fetch:
                    manager._process_task((first, 'https://example.com/a'), 1)
                fetch.assert_called_once()
                self.assertIn('cancel_event', fetch.call_args.kwargs)
                status = manager.get_task(first)['status']
                self.assertTrue(status.startswith('Cancelled' if isinstance(error, DownloadCancelled) else 'Error'), status)
                self.assertFalse(manager.tid_to_workerid)
