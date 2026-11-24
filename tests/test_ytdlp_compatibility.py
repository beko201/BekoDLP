"""The downloader's supported boundary is the CLI, not yt-dlp's Python internals."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import download_engine
import ytdlp_process
from utils import NO_WINDOW


class YtDlpCompatibilityTests(unittest.TestCase):
    def test_application_imports_and_engine_start_without_yt_dlp_packages(self):
        code = """
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('yt_dlp', 'yt_dlp_ejs', 'curl_cffi'):
        raise AssertionError('retired embedded downloader dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import main, diagnostics
from task_manager import TaskManager
manager = TaskManager()
manager.close()
assert manager.finish_shutdown()
assert 'yt_dlp' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                timeout=15, **NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_requirements_do_not_install_the_embedded_downloader(self):
        root = Path(__file__).resolve().parents[1]
        names = (root / 'requirements.txt').read_text(encoding='utf-8').lower()
        for name in ('yt-dlp', 'yt-dlp-ejs', 'curl-cffi'):
            self.assertNotIn(name, names)

    def test_missing_executable_fails_with_setup_instructions(self):
        with patch.object(ytdlp_process, 'executable_path', return_value=None), \
                patch.object(download_engine, 'which_executable') as ffmpeg:
            with self.assertRaisesRegex(ytdlp_process.YtDlpProcessError, 'install_ytdlp.py'):
                download_engine.DownloadEngine(Mock())
        ffmpeg.assert_not_called()

    def test_version_is_diagnostic_and_not_an_exact_python_api_pin(self):
        ytdlp_process.executable_version.cache_clear()
        self.addCleanup(ytdlp_process.executable_version.cache_clear)
        response = subprocess.CompletedProcess([], 0, '2099.01.01\n', '')
        with patch.object(ytdlp_process.subprocess, 'run', return_value=response) as run:
            self.assertEqual(ytdlp_process.executable_version('test.exe'), '2099.01.01')
            self.assertEqual(ytdlp_process.executable_version('test.exe'), '2099.01.01')
            run.assert_called_once()

    def test_invalid_or_failed_version_probe_is_explicit(self):
        for output in ('', 'not a version', '2026.10.26\nextra'):
            with self.subTest(output=output):
                ytdlp_process.executable_version.cache_clear()
                with patch.object(ytdlp_process.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 0, output, '')):
                    self.assertIn('unavailable', ytdlp_process.executable_version('test.exe'))
        ytdlp_process.executable_version.cache_clear()

    def test_http_expiry_is_exposed_without_importing_network_exception_types(self):
        for status in (401, 403, 410):
            error = ytdlp_process.YtDlpProcessError(f'HTTP Error {status}: forbidden', 1)
            self.assertTrue(download_engine._is_expired_media_error(error))
        self.assertFalse(download_engine._is_expired_media_error(
            ytdlp_process.YtDlpProcessError('HTTP Error 404: missing', 1)))

    def test_cli_arguments_preserve_option_boundaries_and_disable_user_configuration(self):
        adapter = ytdlp_process.YtDlpProcess({'outtmpl': 'C:/A B/%(title)s.%(ext)s',
            'format': 'bv[height<=480]+ba/b[height<=480]',
            'js_runtimes': {'deno': {'path': 'C:/A B/deno.exe'}}}, executable='yt-dlp.exe')
        args = adapter._arguments()
        self.assertIn('--ignore-config', args)
        self.assertIn('--no-plugin-dirs', args)
        self.assertEqual(args[args.index('--output') + 1], 'C:/A B/%(title)s.%(ext)s')
        self.assertEqual(args[args.index('--js-runtimes') + 1], 'deno:C:/A B/deno.exe')
