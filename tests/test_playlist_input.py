import unittest
from unittest.mock import Mock, patch

import download_engine
from task_manager import PlaylistFetcher, TaskManager, TaskOptions
from utils import DownloadCancelled, PlaylistLinkNotAllowed


class PlaylistInputTests(unittest.TestCase):
    VIDEO = 'https://www.youtube.com/watch?v=video-id'

    def test_pure_youtube_playlist_is_blocked_before_extraction(self):
        for url in ('https://www.youtube.com/playlist?list=PLdemo',
                    'https://m.youtube.com/playlist?list=PLdemo',
                    'youtube.com/playlist?list=PLdemo',
                    'https://www.youtube.com/playlist?list='):
            with self.subTest(url=url), patch('engine_metadata._flat_extract') as extract:
                with self.assertRaises(PlaylistLinkNotAllowed):
                    download_engine._extract_urls_from_input(url, False)
                extract.assert_not_called()

    def test_metadata_collections_are_blocked_regardless_of_entry_count(self):
        for kind in ('playlist', 'multi_video', None):
            for entries in ([], [{'url': self.VIDEO}], [None],
                            [{'url': self.VIDEO}, {'url': 'https://example.com/other'}]):
                info = {'webpage_url': 'https://example.com/album', 'entries': entries}
                if kind:
                    info['_type'] = kind
                with self.subTest(kind=kind, entries=entries), \
                        patch('engine_metadata._flat_extract', return_value=(info, entries or None)):
                    with self.assertRaises(PlaylistLinkNotAllowed):
                        download_engine._extract_urls_from_input(info['webpage_url'], False)

    def test_failed_preflight_does_not_enqueue_an_unverified_collection(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        error = RuntimeError('album unavailable')
        with patch('engine_metadata._flat_extract', side_effect=error), \
                patch('engine_metadata.log_warning'), patch('task_workers.log_error'):
            PlaylistFetcher(manager, 'https://example.com/album', TaskOptions(folder='.'), False).run()
        self.assertEqual(manager.list_tasks(), [])
        events = []
        while not manager.events.empty():
            events.append(manager.events.get_nowait())
        self.assertEqual([event.kind for event in events], ['error'])
        self.assertIn('album unavailable', events[0].data['message'])

    def test_blocked_collection_emits_playlist_event_without_creating_tasks(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        PlaylistFetcher(manager, 'https://www.youtube.com/playlist?list=PLdemo',
                        TaskOptions(folder='.'), False).run()
        self.assertEqual(manager.list_tasks(), [])
        self.assertEqual(manager.events.get_nowait().kind, 'playlist_blocked')
        self.assertTrue(manager.task_queue.empty())

    def test_watch_link_with_playlist_uses_only_explicit_video_even_on_failure(self):
        for url in (self.VIDEO + '&list=PLdemo', 'https://youtu.be/video-id?list=PLdemo'):
            for result in ('video', 'playlist', 'failure'):
                with self.subTest(url=url, result=result):
                    kwargs = {'side_effect': RuntimeError('offline')} if result == 'failure' else {
                        'return_value': ({'_type': result, 'webpage_url': self.VIDEO}, None)}
                    with patch('engine_metadata._flat_extract', **kwargs) as extract, \
                            patch('engine_metadata.log_warning'):
                        self.assertEqual(download_engine._extract_urls_from_input(url, False), [self.VIDEO])
                        extract.assert_called_once_with(self.VIDEO, noplaylist=True, cancel_event=None)

    def test_bulk_playlist_expansion_and_foreign_urls_are_preserved(self):
        urls = [self.VIDEO, 'https://www.youtube.com/watch?v=other-id']
        info = {'_type': 'playlist', 'entries': [{'url': u} for u in urls]}
        with patch('engine_metadata._flat_extract', return_value=(info, info['entries'])):
            self.assertEqual(download_engine._extract_urls_from_input(
                'https://www.youtube.com/playlist?list=PLdemo', True), urls)
            foreign = 'https://example.com/album?list=PLdemo&v=foreign-id'
            self.assertEqual(download_engine._extract_urls_from_input(foreign, True), urls)

    def test_single_foreign_video_still_works_and_reuses_metadata(self):
        url = 'https://www.tiktok.com/@name/video/123'
        info = {'_type': 'video', 'id': '123', 'webpage_url': url, 'url': 'https://cdn.example/video.mp4'}
        cached = {}
        with patch('engine_metadata._flat_extract', return_value=(info, None)):
            self.assertEqual(download_engine._extract_urls_from_input(url, False, resolved_info=cached), [url])
        self.assertIs(cached[url], info)

    def test_cancel_is_preserved_and_missing_metadata_is_not_queued(self):
        with patch('engine_metadata._flat_extract', side_effect=DownloadCancelled):
            with self.assertRaises(DownloadCancelled):
                download_engine._extract_urls_from_input(self.VIDEO, False)
        with patch('engine_metadata._flat_extract', return_value=(None, None)):
            with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                download_engine._extract_urls_from_input('https://example.com/unknown', False)


if __name__ == '__main__':
    unittest.main()
