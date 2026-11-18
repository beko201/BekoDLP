from pathlib import Path
from importlib.metadata import requires
import unittest
from unittest.mock import Mock, patch

import download_engine
import yt_dlp


class YtDlpCompatibilityTests(unittest.TestCase):
    def test_requirements_pin_matches_runtime_supported_version(self):
        requirements = Path(__file__).resolve().parents[1] / 'requirements.txt'
        lines = requirements.read_text(encoding='utf-8').splitlines()
        pins = [line.strip() for line in lines if line.strip().startswith('yt-dlp==')]
        self.assertEqual(pins, ['yt-dlp==' + download_engine.SUPPORTED_YTDLP_VERSION])
        ejs_requirements = {requirement.split(';', 1)[0].strip() for requirement in requires('yt-dlp')
                            if requirement.startswith('yt-dlp-ejs==')}
        self.assertTrue(ejs_requirements)
        self.assertTrue(ejs_requirements.issubset({line.strip() for line in lines}))
        download_engine.validate_ytdlp_compatibility()

    def test_unverified_or_unknown_version_fails_before_engine_starts(self):
        for version in ('2099.1.1', ''):
            with self.subTest(version=version), \
                    patch.object(yt_dlp.version, '__version__', version), \
                    patch.object(download_engine, 'which_executable') as find_tool:
                with self.assertRaises(download_engine.YtDlpCompatibilityError) as caught:
                    download_engine.DownloadEngine(Mock())
                self.assertIn(download_engine.SUPPORTED_YTDLP_VERSION, str(caught.exception))
                self.assertIn('requirements.txt', str(caught.exception))
                find_tool.assert_not_called()

    def test_direct_managed_ydl_use_also_rejects_unverified_version(self):
        with patch.object(yt_dlp.version, '__version__', '2099.1.1'), \
                patch.object(yt_dlp.YoutubeDL, '__init__', return_value=None) as initialize:
            with self.assertRaises(download_engine.YtDlpCompatibilityError):
                download_engine.ManagedYoutubeDL({'quiet': True}, Mock())
        initialize.assert_not_called()

    def test_missing_private_methods_have_actionable_errors(self):
        pp_class = yt_dlp.postprocessor.ffmpeg.FFmpegPostProcessor
        for method in ('real_run_ffmpeg', '_configuration_args', '_ffmpeg_filename_argument',
                       'check_version', 'try_utime'):
            with self.subTest(method=method), patch.object(pp_class, method, None):
                with self.assertRaises(download_engine.YtDlpCompatibilityError) as caught:
                    download_engine.validate_ytdlp_compatibility()
                self.assertIn(method, str(caught.exception))
                self.assertIn('requirements.txt', str(caught.exception))

    def test_changed_ffmpeg_call_signature_is_detected(self):
        def changed(self, inputs, outputs, *, required_new_option):
            pass

        with patch.object(yt_dlp.postprocessor.ffmpeg.FFmpegPostProcessor, 'real_run_ffmpeg', changed):
            with self.assertRaisesRegex(download_engine.YtDlpCompatibilityError, 'real_run_ffmpeg'):
                download_engine.validate_ytdlp_compatibility()

    def test_changed_run_pp_signature_is_detected(self):
        with patch.object(yt_dlp.YoutubeDL, 'run_pp', lambda self, pp, info, extra: None):
            with self.assertRaisesRegex(download_engine.YtDlpCompatibilityError, 'YoutubeDL.run_pp'):
                download_engine.validate_ytdlp_compatibility()

    def test_subclass_api_changes_are_checked_before_interception(self):
        class ChangedPP(yt_dlp.postprocessor.ffmpeg.FFmpegPostProcessor):
            def _configuration_args(self, exe, *, required_new_option):
                return []

        runner = Mock()
        with download_engine.ManagedYoutubeDL({'quiet': True}, runner) as ydl:
            pp = ChangedPP(ydl)
            with self.assertRaisesRegex(download_engine.YtDlpCompatibilityError, '_configuration_args'):
                ydl.run_pp(pp, {})
        runner.assert_not_called()
        self.assertNotIn('real_run_ffmpeg', pp.__dict__)


if __name__ == '__main__':
    unittest.main()
