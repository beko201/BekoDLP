"""Playlist batches arrive before exhaustion, with bounded extractor paging caches."""
import unittest
from unittest.mock import MagicMock, Mock, patch

import yt_dlp
import download_engine
from task_manager import PlaylistFetcher, TaskManager, TaskOptions


class PlaylistStreamingTests(unittest.TestCase):
    def manager(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        return manager

    def fake_ydl(self, info):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = info
        ydl.process_ie_result.side_effect = lambda info, **kwargs: info
        return ydl

    def test_lazy_entries_queue_before_exhaustion_and_emit_one_total_notice(self):
        manager = self.manager()
        generated = []
        def entries():
            for index in range(260):
                if index == 100:
                    self.assertEqual(manager.summary().total, 100)
                generated.append(index)
                yield {"url": f"https://example.com/{index}"}
        lazy = yt_dlp.utils.LazyList(entries())
        lazy[4]  # Prefetched entries must not be skipped when releasing caches.
        ydl = self.fake_ydl({"_type": "playlist", "entries": lazy})
        with patch.object(download_engine.yt_dlp, "YoutubeDL", return_value=ydl) as factory:
            PlaylistFetcher(manager, "https://example.com/playlist", TaskOptions(folder="."), True).run()
        self.assertEqual(generated, list(range(260)))
        self.assertEqual(manager.summary().total, 260)
        self.assertFalse(lazy._cache)
        self.assertTrue(factory.call_args.args[0]["lazy_playlist"])
        ydl.extract_info.assert_called_once_with("https://example.com/playlist", download=False, process=False)
        notices = []
        while not manager.events.empty():
            event = manager.events.get_nowait()
            manager.events.task_done()
            if event.kind == "notice":
                notices.append(event.data["message"])
        self.assertEqual(notices, ["260 videos added to the queue!"])

    def test_paged_entries_fetch_each_page_once_and_release_page_cache(self):
        calls, batches = [], []
        def page(index):
            calls.append(index)
            return [{"url": f"https://example.com/{i}"} for i in range(index * 40, min(250, (index + 1) * 40))]
        pages = yt_dlp.utils.OnDemandPagedList(page, 40)
        ydl = self.fake_ydl({"_type": "playlist", "entries": pages})
        with patch.object(download_engine.yt_dlp, "YoutubeDL", return_value=ydl):
            result = download_engine._extract_urls_from_input("https://example.com/playlist", True,
                                                            on_batch=lambda urls: batches.append(list(urls)))
        self.assertEqual(result, [])
        self.assertEqual([len(batch) for batch in batches], [100, 100, 50])
        self.assertEqual(calls, list(range(7)))
        self.assertFalse(pages._cache)
        self.assertEqual(len({url for batch in batches for url in batch}), 250)

    def test_partial_failure_retains_the_available_tail_and_reports_a_warning(self):
        manager = self.manager()
        def entries():
            for index in range(150):
                yield {"url": f"https://example.com/{index}"}
            raise RuntimeError("next playlist page failed")
        ydl = self.fake_ydl({"_type": "playlist", "entries": entries()})
        with patch.object(download_engine.yt_dlp, "YoutubeDL", return_value=ydl), \
                patch.object(download_engine, "log_warning"):
            PlaylistFetcher(manager, "https://example.com/playlist", TaskOptions(folder="."), True).run()
        self.assertEqual(manager.summary().total, 150)
        kinds = []
        while not manager.events.empty():
            event = manager.events.get_nowait()
            manager.events.task_done()
            kinds.append(event.kind)
        self.assertIn("info", kinds)
        self.assertNotIn("error", kinds)

    def test_selected_video_is_first_and_duplicate_entries_across_batches_are_skipped(self):
        chosen = "https://www.youtube.com/watch?v=chosen"
        entries = [{"url": f"https://example.com/{i}"} for i in range(210)]
        entries += [{"url": chosen}, {"url": "https://example.com/0"}]
        ydl = self.fake_ydl({"_type": "playlist", "entries": iter(entries)})
        batches = []
        with patch.object(download_engine.yt_dlp, "YoutubeDL", return_value=ydl):
            download_engine._extract_urls_from_input(chosen + "&list=PLdemo", True,
                                                     on_batch=lambda urls: batches.extend(urls))
        self.assertEqual(batches[0], chosen)
        self.assertEqual(len(batches), 211)
        self.assertEqual(len(set(batches)), 211)

    def test_shutdown_during_iteration_does_not_queue_the_unfinished_batch(self):
        manager = self.manager()
        def entries():
            for index in range(200):
                if index == 110:
                    manager.close()
                yield {"url": f"https://example.com/{index}"}
        ydl = self.fake_ydl({"_type": "playlist", "entries": entries()})
        with patch.object(download_engine.yt_dlp, "YoutubeDL", return_value=ydl):
            PlaylistFetcher(manager, "https://example.com/playlist", TaskOptions(folder="."), True).run()
        self.assertEqual(manager.summary().total, 100)
        ydl.__exit__.assert_called_once()

    def test_empty_collection_creates_no_video_task(self):
        manager = self.manager()
        ydl = self.fake_ydl({"_type": "playlist", "entries": []})
        with patch.object(download_engine.yt_dlp, "YoutubeDL", return_value=ydl):
            PlaylistFetcher(manager, "https://www.youtube.com/playlist?list=PLempty", TaskOptions(folder="."), True).run()
        self.assertEqual(manager.summary().total, 0)
        self.assertEqual(ydl.extract_info.call_count, 1)

    def test_empty_watch_playlist_keeps_the_explicit_video_instead_of_queuing_the_collection(self):
        manager = self.manager()
        chosen = 'https://www.youtube.com/watch?v=chosen'
        ydl = self.fake_ydl({'_type': 'playlist', 'entries': [],
                             'webpage_url': 'https://www.youtube.com/playlist?list=PLempty'})
        with patch.object(download_engine.yt_dlp, 'YoutubeDL', return_value=ydl):
            PlaylistFetcher(manager, chosen + '&list=PLempty', TaskOptions(folder='.'), True).run()
        self.assertEqual([task['url'] for task in manager.list_tasks()], [chosen])

    def test_real_ytdlp_raw_playlist_processing_does_not_exhaust_before_first_batch(self):
        from yt_dlp.extractor.common import InfoExtractor
        manager = self.manager()
        class LocalPlaylistIE(InfoExtractor):
            _VALID_URL = r'localplaylist:(?P<id>\w+)'
            def _real_extract(self, url):
                def entries():
                    for index in range(250):
                        if index == 100:
                            self_test.assertEqual(manager.summary().total, 100)
                        yield self.url_result(f'https://example.com/{index}', ie='Generic')
                return self.playlist_result(entries(), 'local', 'Local playlist')
        self_test = self
        original = yt_dlp.YoutubeDL
        def make_ydl(options):
            ydl = original(options, auto_init=False)
            ydl.add_info_extractor(LocalPlaylistIE())
            return ydl
        with patch.object(download_engine.yt_dlp, 'YoutubeDL', side_effect=make_ydl):
            PlaylistFetcher(manager, 'localplaylist:local', TaskOptions(folder='.'), True).run()
        self.assertEqual(manager.summary().total, 250)

    def test_transparent_redirect_preserves_embedding_metadata_and_headers(self):
        outer = {'_type': 'url_transparent', 'url': 'https://example.com/target', 'id': 'outer',
                 'title': 'Embedding title', 'http_headers': {'Referer': 'https://example.com/embed'}}
        inner = {'_type': 'video', 'id': 'target', 'url': 'https://cdn.example.com/video.mp4'}
        ydl = self.fake_ydl(outer)
        ydl.extract_info.side_effect = [outer, inner]
        with patch.object(download_engine.yt_dlp, 'YoutubeDL', return_value=ydl):
            info, entries = download_engine._flat_extract('https://example.com/embed', on_entries=lambda batch: None)
        self.assertEqual(info['id'], 'target')
        self.assertEqual(info['title'], 'Embedding title')
        self.assertEqual(info['http_headers'], outer['http_headers'])
        self.assertIsNone(entries)
