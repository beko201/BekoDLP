from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


def video_format(height, progressive=False, codec='avc1'):
    return {'format_id': f'{height}-{progressive}-{codec}', 'height': height,
            'vcodec': codec, 'acodec': 'aac' if progressive else 'none',
            'ext': 'mp4', 'url': 'https://example.com/media', 'protocol': 'https'}


AUDIO = {'format_id': 'audio', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac',
         'url': 'https://example.com/audio', 'protocol': 'https'}


class ResolutionCapTests(unittest.TestCase):
    def test_native_selection_uses_highest_resolution_within_cap(self):
        cases = [
            ([video_format(1080), video_format(360)], '480p', 360),
            ([video_format(1080, True), video_format(360, True)], '480p', 360),
            ([video_format(360, True), video_format(480)], '480p', 480),
            ([video_format(720, True), video_format(360)], '480p', 360),
            ([video_format(360, True), video_format(720)], '480p', 360),
            ([video_format(360, True, 'vp9'), video_format(360)], '480p', 360),
            ([video_format(4320), video_format(1440), video_format(1080)], '1440p(2K)', 1440),
            ([video_format(4320), video_format(2160)], '2160p(4K)', 2160),
        ]
        for formats, quality, expected in cases:
            for codec in ('Original (Automatic)', 'H.264 (Compatibility)'):
                with self.subTest(formats=formats, quality=quality, codec=codec):
                    with tempfile.TemporaryDirectory() as temp:
                        app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp), 'MP4', codec)
                        app.tasks['T1']['quality'] = quality
                        selected_heights = []
                        download = app._download_format

                        def record(tid, key, url, stream, filename, *, folder, stop_event=None):
                            if not stream.only_audio:
                                selected_heights.append(stream.height)
                            return download(tid, key, url, stream, filename, folder=folder, stop_event=stop_event)

                        def convert(tid, wid, cmd, *args, **kwargs):
                            Path(cmd[-1]).write_bytes(b'output')
                            return 0

                        app._download_format = record
                        app._run_ffmpeg = convert
                        info = downloader.YTInfo('url', {'title': 'Example', 'formats': [*formats, AUDIO]})
                        with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=info):
                            app._process_task(('T1', 'url'), 1)
                        app._task_error.assert_not_called()
                        self.assertEqual(selected_heights, [expected])
                        self.assertTrue(Path(app.tasks['T1']['output_path']).is_file())

    def test_no_eligible_native_format_keeps_cap_in_fallback(self):
        for codec in ('Original (Automatic)', 'H.264 (Compatibility)'):
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as temp:
                app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp), 'MP4', codec)
                app.tasks['T1']['quality'] = '480p'
                app._download_format = Mock()
                app._complete_via_selector = Mock(side_effect=RuntimeError('no eligible format'))
                info = downloader.YTInfo('url', {'formats': [video_format(1080), AUDIO]})
                with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=info):
                    app._process_task(('T1', 'url'), 1)
                app._download_format.assert_not_called()
                app._complete_via_selector.assert_called_once()
                self.assertEqual(app._complete_via_selector.call_args.args[6], 480)

    def test_real_ytdlp_selector_never_uses_unbounded_fallback(self):
        for force_h264 in (False, True):
            for heights, expected in (([360, 1080], 360), ([1080], None), ([None], None)):
                with self.subTest(h264=force_h264, heights=heights), tempfile.TemporaryDirectory() as temp:
                    app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp), 'MP4')
                    selected = []

                    class SelectionOnly:
                        def __init__(self, opts, runner, *, request_check=None):
                            self.opts = opts

                        def __enter__(self):
                            return self

                        def __exit__(self, *args):
                            return False

                        def extract_info(self, url, download):
                            info = {'id': 'test', 'title': 'Test', 'extractor': 'generic',
                                    'formats': [*(video_format(h) for h in heights), AUDIO]}
                            with downloader.yt_dlp.YoutubeDL({**self.opts, 'logger': Mock()}) as ydl:
                                result = ydl.process_ie_result(info, download=False)
                            chosen = result.get('requested_formats') or [result]
                            selected.extend(f['height'] for f in chosen if f.get('vcodec') != 'none')
                            raise RuntimeError('selection checked; skip actual download')

                    with patch.object(downloader, 'ManagedYoutubeDL', SelectionOnly):
                        expected_error = (downloader.yt_dlp.utils.ExtractorError if expected is None
                                          else RuntimeError)
                        message = 'Requested format is not available' if expected is None else 'selection checked'
                        with self.assertRaisesRegex(expected_error, message):
                            app._complete_via_selector('T1', 1, 'url', 'Test', 'Test', 'MP4',
                                                       480, force_h264, '', '', folder=temp)
                    self.assertEqual(selected, [] if expected is None else [expected])

    def test_invalid_quality_does_not_enable_unlimited_download(self):
        for quality in ('invalid', '0p', '-480p'):
            with self.subTest(quality=quality), tempfile.TemporaryDirectory() as temp:
                app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp), 'MP4')
                app.tasks['T1']['quality'] = quality
                app._download_format = Mock()
                info = downloader.YTInfo('url', {'formats': [video_format(1080), AUDIO]})
                with patch.object(downloader, 'fetch_ytinfo_with_retry', return_value=info):
                    app._process_task(('T1', 'url'), 1)
                app._download_format.assert_not_called()
                app._task_error.assert_called_once()
                self.assertIn('Invalid resolution', app._task_error.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
