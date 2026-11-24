"""CLI playlist batches reach the queue before extraction finishes."""
import json
import unittest
from unittest.mock import Mock, patch

import download_engine
from task_manager import PlaylistFetcher, TaskManager, TaskOptions
from ytdlp_process import YtDlpProcess, YtDlpProcessError


class PlaylistStreamingTests(unittest.TestCase):
    def manager(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        return manager

    def stream(self, entries):
        def run(adapter, args, consume):
            self.assertIn('--lazy-playlist', args)
            for entry in entries:
                adapter.check()
                consume(json.dumps({'playlist_id': 'local', 'playlist_title': 'Local', **entry}))
        return patch.object(YtDlpProcess, '_run', run)

    def test_entries_queue_before_exhaustion_and_emit_one_total_notice(self):
        manager = self.manager()
        generated = []
        def entries():
            for index in range(260):
                if index == 100:
                    self.assertEqual(manager.summary().total, 100)
                generated.append(index)
                yield {'url': f'https://example.com/{index}'}
        with self.stream(entries()):
            PlaylistFetcher(manager, 'https://example.com/playlist', TaskOptions(folder='.'), True).run()
        self.assertEqual(generated, list(range(260)))
        self.assertEqual(manager.summary().total, 260)
        notices = []
        while not manager.events.empty():
            event = manager.events.get_nowait()
            manager.events.task_done()
            if event.kind == 'notice':
                notices.append(event.data['message'])
        self.assertEqual(notices, ['260 videos added to the queue!'])

    def test_streaming_metadata_retains_only_a_small_preview(self):
        batches = []
        with self.stream({'url': f'https://example.com/{i}'} for i in range(250)):
            info, entries = download_engine._flat_extract('https://example.com/playlist',
                on_entries=lambda batch: batches.append(list(batch)))
        self.assertEqual([len(batch) for batch in batches], [100, 100, 50])
        self.assertEqual(len(entries), 2)
        self.assertEqual(info['title'], 'Local')
        self.assertEqual(len({entry['url'] for batch in batches for entry in batch}), 250)

    def test_nonstreaming_caller_receives_all_entries(self):
        with self.stream({'url': f'https://example.com/{i}'} for i in range(250)):
            info, entries = download_engine._flat_extract('https://example.com/playlist')
        self.assertEqual(len(entries), 250)
        self.assertEqual(info['entries'], entries)

    def test_partial_failure_retains_available_tail_and_reports_a_warning(self):
        manager = self.manager()
        def entries():
            for index in range(150):
                yield {'url': f'https://example.com/{index}'}
            raise YtDlpProcessError('next playlist page failed', 1)
        with self.stream(entries()), patch('engine_metadata.log_warning'):
            PlaylistFetcher(manager, 'https://example.com/playlist', TaskOptions(folder='.'), True).run()
        self.assertEqual(manager.summary().total, 150)
        kinds = []
        while not manager.events.empty():
            kinds.append(manager.events.get_nowait().kind)
            manager.events.task_done()
        self.assertIn('info', kinds)
        self.assertNotIn('error', kinds)

    def test_selected_video_is_first_and_duplicates_across_batches_are_skipped(self):
        chosen = 'https://www.youtube.com/watch?v=chosen'
        entries = [{'url': f'https://example.com/{i}'} for i in range(210)]
        entries += [{'url': chosen}, {'url': 'https://example.com/0'}]
        batches = []
        with self.stream(entries):
            download_engine._extract_urls_from_input(chosen + '&list=PLdemo', True,
                                                      on_batch=lambda urls: batches.extend(urls))
        self.assertEqual(batches[0], chosen)
        self.assertEqual(len(batches), 211)
        self.assertEqual(len(set(batches)), 211)

    def test_shutdown_does_not_queue_an_unfinished_batch(self):
        manager = self.manager()
        def entries():
            for index in range(200):
                if index == 110:
                    manager.close()
                yield {'url': f'https://example.com/{index}'}
        with self.stream(entries()):
            PlaylistFetcher(manager, 'https://example.com/playlist', TaskOptions(folder='.'), True).run()
        self.assertEqual(manager.summary().total, 100)

    def test_empty_collection_creates_no_video_task(self):
        manager = self.manager()
        with self.stream([]):
            PlaylistFetcher(manager, 'https://www.youtube.com/playlist?list=PLempty',
                            TaskOptions(folder='.'), True).run()
        self.assertEqual(manager.summary().total, 0)

    def test_empty_watch_playlist_keeps_the_explicit_video(self):
        manager = self.manager()
        chosen = 'https://www.youtube.com/watch?v=chosen'
        with self.stream([]):
            PlaylistFetcher(manager, chosen + '&list=PLempty', TaskOptions(folder='.'), True).run()
        self.assertEqual([task['url'] for task in manager.list_tasks()], [chosen])

    def test_single_video_metadata_is_preserved_for_reuse(self):
        info = {'_type': 'video', 'id': 'target', 'title': 'Embedding title',
                'url': 'https://cdn.example.com/video.mp4',
                'http_headers': {'Referer': 'https://example.com/embed'}}
        def run(adapter, args, consume):
            consume(json.dumps(info))
        with patch.object(YtDlpProcess, '_run', run):
            result, entries = download_engine._flat_extract('https://example.com/embed', on_entries=Mock())
        self.assertEqual(result, info)
        self.assertIsNone(entries)
