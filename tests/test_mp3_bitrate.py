from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


class MP3BitrateTests(unittest.TestCase):
    def make_app(self, folder):
        app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(folder))
        app.folder = str(folder)
        app.tasks.clear()
        app.active_urls.clear()
        app.cancel_events.clear()
        app.task_counter = 0
        app.task_queue = Queue()
        app._ui_queue = Queue()
        app.tree = Mock()
        app.tree.selection.return_value = ()
        app.mp3_bitrate_var = Mock()
        app.mp3_bitrate_var.get.return_value = '128k'
        app._run_ffmpeg = Mock(return_value=0)
        return app

    def add_urls(self, app, urls, fmt, quality, codec):
        return app.manager.add_urls(urls, downloader.TaskOptions(
            folder=app.folder, format=fmt, quality=quality, codec_mode=codec,
            mp3_bitrate=app.mp3_bitrate_var.get()))

    def convert_in_worker(self, app, tid):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(app._ffmpeg_convert_to_mp3, tid, 1, 'source.m4a', 'output.mp3').result(timeout=2)
        cmd = app._run_ffmpeg.call_args.args[2]
        return cmd[cmd.index('-ab') + 1]

    def test_slow_link_resolution_preserves_bitrate_selected_at_submit(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            options = downloader.TaskOptions(folder=folder, format='MP3', mp3_bitrate=app.mp3_bitrate_var.get())
            fetcher = downloader.PlaylistFetcher(app.manager, 'url', options)
            app.mp3_bitrate_var.get.side_effect = AssertionError('live UI bitrate must not be read again')
            with patch.object(downloader, '_extract_urls_from_input', return_value=['https://example.com/one']):
                fetcher.run()
            self.assertEqual(app.tasks['T1']['mp3_bitrate'], '128k')
            self.assertEqual(self.convert_in_worker(app, 'T1'), '128k')
            app.mp3_bitrate_var.get.assert_called_once()

    def test_queued_tasks_keep_independent_bitrates_after_ui_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.add_urls(app, ['https://example.com/one'], 'MP3', '1080p', 'Original (Automatic)')
            app.mp3_bitrate_var.get.return_value = '320k'
            self.add_urls(app, ['https://example.com/two'], 'MP3', '1080p', 'Original (Automatic)')
            app.mp3_bitrate_var.get.side_effect = AssertionError('worker read a live input value')
            self.assertEqual(self.convert_in_worker(app, 'T1'), '128k')
            self.assertEqual(self.convert_in_worker(app, 'T2'), '320k')

    def test_batch_captures_bitrate_once_for_all_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            app.mp3_bitrate_var.get.side_effect = ['256k', AssertionError('batch read bitrate twice')]
            self.add_urls(app, ['https://example.com/one', 'https://example.com/two'],
                                  'MP3', '1080p', 'Original (Automatic)')
            self.assertEqual([t['mp3_bitrate'] for t in app.tasks.values()], ['256k', '256k'])
            app.mp3_bitrate_var.get.assert_called_once()

    def test_retry_preserves_original_bitrate(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.add_urls(app, ['https://example.com/one'], 'MP3', '1080p', 'Original (Automatic)')
            app.task_queue.get_nowait()
            app.task_queue.task_done()
            app.tasks['T1']['status'] = 'Error: network failure'
            app.active_urls.clear()
            app.tree.selection.return_value = ('T1',)
            app.mp3_bitrate_var.get.side_effect = AssertionError('retry must not read current bitrate')
            app.retry_failed()
            self.assertEqual(app.task_queue.get_nowait()[0], 'T1')
            self.assertEqual(self.convert_in_worker(app, 'T1'), '128k')

    def test_selector_fallback_uses_task_bitrate(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            self.add_urls(app, ['https://example.com/one'], 'MP3', '1080p', 'Original (Automatic)')
            app.mp3_bitrate_var.get.side_effect = AssertionError('fallback read current bitrate')
            source = Path(folder, 'Example_T1.m4a')
            source.write_bytes(b'audio source')
            ydl = Mock()
            ydl.__enter__ = Mock(return_value=ydl)
            ydl.__exit__ = Mock(return_value=False)

            def managed_ydl(opts, **kwargs):
                def extract(url, download):
                    private_source = Path(opts['outtmpl'].replace('%(ext)s', 'm4a'))
                    private_source.write_bytes(b'private source')
                    return {'requested_downloads': [{'filepath': str(private_source)}]}
                ydl.extract_info.side_effect = extract
                return ydl

            def convert(tid, wid, cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b'mp3 output')
                return 0

            app._run_ffmpeg.side_effect = convert
            with patch.object(downloader, 'YtDlpProcess', side_effect=managed_ydl):
                app._complete_via_selector('T1', 1, 'url', 'Example', 'Example', 'MP3',
                                           None, False, '', '', folder=folder)
            cmd = app._run_ffmpeg.call_args.args[2]
            self.assertEqual(cmd[cmd.index('-ab') + 1], '128k')
            self.assertTrue(Path(folder, 'Example.mp3').is_file())
            self.assertEqual(source.read_bytes(), b'audio source')


if __name__ == '__main__':
    unittest.main()
