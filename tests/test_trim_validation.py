from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


class TrimValidationTests(unittest.TestCase):
    def test_valid_timecodes_and_blank_fields(self):
        cases = [(None, None), ('', None), ('  ', None), ('0', 0), ('90', 90),
                 ('1.5', 1.5), ('1:30', 90), ('75:00', 4500),
                 ('1:02:30.5', 3750.5), (' 01:05.25 ', 65.25)]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(downloader.parse_timecode(text), expected)
        for start, end, expected in [('', '', (None, None)), ('0', '', (0, None)),
                                     ('', '30', (None, 30)), ('1:00', '', (60, None)),
                                     ('1:00', '1:30', (60, 90))]:
            with self.subTest(start=start, end=end):
                self.assertEqual(downloader.validate_trim_range(start, end), expected)

    def test_invalid_timecodes_raise_instead_of_silently_disabling_trim(self):
        cases = ['-1:90', '-1', '+10', 'inf', '-inf', 'Infinity', 'nan', 'NaN',
                 '1e3', '1:60', '1:99', '1:60:00', '1:00:60', '1:2:3:4',
                 '1.5:30', 'abc', '1::30', ':30', '1:', '1,5', '9' * 400,
                 ('9' * 308) + ':00:00']
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    downloader.parse_timecode(text)

    def test_invalid_ranges_identify_the_field_or_order(self):
        for start, end, message in [('abc', '', 'Start'), ('', 'inf', 'End'),
                                     ('2:00', '1:00', 'greater'), ('1:00', '60', 'greater'),
                                     ('', '0', 'greater')]:
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(ValueError, message):
                    downloader.validate_trim_range(start, end)


    def test_existing_invalid_task_fails_before_fetch_or_download(self):
        with tempfile.TemporaryDirectory() as temp:
            app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
            app.tasks['T1'].update(trim_start='2:00', trim_end='1:00')
            with patch.object(downloader, 'fetch_ytinfo_with_retry') as fetch:
                app._process_task(('T1', 'url'), 1)
            fetch.assert_not_called()
            app._task_error.assert_called_once()
            self.assertIn('greater', app._task_error.call_args.args[1])
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_invalid_trim_never_launches_ffmpeg_or_replaces_original(self):
        with tempfile.TemporaryDirectory() as temp:
            app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
            original = Path(temp, 'video.mp4')
            original.write_bytes(b'original')
            app._run_ffmpeg = Mock()
            app._dedupe_path = Mock()
            for start, end in [('-1:90', ''), ('', 'inf'), ('2:00', '1:00'), ('30', '30')]:
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    app._maybe_trim('T1', 1, str(original), start, end)
            app._run_ffmpeg.assert_not_called()
            app._dedupe_path.assert_not_called()
            self.assertEqual(original.read_bytes(), b'original')

    def test_end_only_trim_has_explicit_positive_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
            original = Path(temp, 'video.mp4')
            original.write_bytes(b'original')

            def trim(tid, wid, cmd, **kwargs):
                self.assertNotIn('-ss', cmd)
                self.assertEqual(float(cmd[cmd.index('-t') + 1]), 30)
                Path(cmd[-1]).write_bytes(b'trimmed')

            app._run_ffmpeg = trim
            app._maybe_trim('T1', 1, str(original), '', '30')
            self.assertEqual(original.read_bytes(), b'trimmed')

    def test_start_at_or_beyond_source_duration_preserves_original(self):
        for start in ('1', '10'):
            with self.subTest(start=start), tempfile.TemporaryDirectory() as temp:
                app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
                original = Path(temp, 'audio.mp3')
                original.write_bytes(b'original')
                app._run_ffmpeg = Mock()
                app._maybe_trim('T1', 1, str(original), start, '12')
                app._run_ffmpeg.assert_not_called()
                self.assertEqual(original.read_bytes(), b'original')
                self.assertIn('Trimming could not be applied', app.tasks['T1']['warnings'][0])
                self.assertEqual(app.reserved_output_paths, {})

    def test_invalid_trimmed_output_preserves_original_and_cleans_artifacts(self):
        for failure in ('unreadable', 'zero_duration', 'no_media_stream'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                app = cleanup_fixture.TaskCleanupTests.make_app(self, Path(temp))
                original = Path(temp, 'audio.mp3')
                original.write_bytes(b'original')
                app._run_ffmpeg = lambda tid, wid, cmd, **kw: Path(cmd[-1]).write_bytes(b'bad output')
                if failure == 'unreadable':
                    app._ffprobe_media = Mock(side_effect=RuntimeError('invalid media'))
                elif failure == 'zero_duration':
                    app._ffprobe_media.return_value['format']['duration'] = '0'
                else:
                    app._ffprobe_media = Mock(return_value={'streams': [{'codec_type': 'data'}]})
                app._maybe_trim('T1', 1, str(original), '0', '0.5')
                self.assertEqual(original.read_bytes(), b'original')
                self.assertEqual(list(Path(temp).iterdir()), [original])
                self.assertIn('Trimming could not be applied', app.tasks['T1']['warnings'][0])
                self.assertEqual(app.reserved_output_paths, {})


if __name__ == '__main__':
    unittest.main()
