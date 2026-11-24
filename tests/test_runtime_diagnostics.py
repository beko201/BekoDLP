"""One session version snapshot, bounded tool probes and failure handling."""
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import diagnostics
import utils
from app_info import APP_VERSION


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        platform_patch = patch.object(diagnostics.platform, 'system', return_value='Test OS')
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        for target, value in (('diagnostics._RUNTIME_INFO', None),
                              ('diagnostics._SESSION_LOGGED', False),
                              ('utils.ERROR_LOG_FILE', str(self.folder / 'errors.log')),
                              ('utils.DEBUG_LOG_FILE', str(self.folder / 'debug.log')),
                              ('utils.FALLBACK_DEBUG_LOG_FILE', str(self.folder / 'debug-fallback.log')),
                              ('utils.FALLBACK_ERROR_LOG_FILE', str(self.folder / 'fallback.log')),
                              ('utils._LOG_STATE', None)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        version_patch = patch('ytdlp_process.executable_version', return_value='2026.10.26')
        version_patch.start()
        self.addCleanup(version_patch.stop)
        runtime_patch = patch.object(utils, 'portable_deno_path', return_value='custom-deno/deno')
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    @staticmethod
    def tool_output(command, **kwargs):
        name = Path(command[0]).stem
        line = 'deno 2.9.8 (stable, release)' if name == 'deno' else f'{name} version test-build'
        return SimpleNamespace(returncode=0, stdout=line + '\nconfiguration: ignored\n')

    def test_versions_are_probed_once_and_session_snapshot_is_logged_once(self):
        with patch.object(diagnostics.subprocess, 'run', side_effect=self.tool_output) as run, \
                patch.object(diagnostics.metadata, 'version', return_value='test-qt'):
            info = diagnostics.initialize_runtime_diagnostics('custom-ffmpeg/ffmpeg', 'custom-ffprobe/ffprobe')
            diagnostics.initialize_runtime_diagnostics('other/ffmpeg', 'other/ffprobe')
            report = diagnostics.build_diagnostic_report()
            returned = diagnostics.get_runtime_info()
            returned['FFmpeg'] = 'modified copy'
            self.assertEqual(diagnostics.get_runtime_info()['FFmpeg'], 'test-build')
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ['custom-ffmpeg/ffmpeg', '-version'])
        self.assertEqual(run.call_args_list[1].args[0], ['custom-ffprobe/ffprobe', '-version'])
        self.assertEqual(run.call_args_list[2].args[0], ['custom-deno/deno', '--version'])
        self.assertEqual(info['Deno'], '2.9.8')
        self.assertTrue(all(call.kwargs['timeout'] == diagnostics.TOOL_VERSION_TIMEOUT
                            for call in run.call_args_list))
        log = (self.folder / 'debug.log').read_text(encoding='utf-8')
        self.assertEqual(log.count('[event=session_started]'), 1)
        for key in ('Application', 'Python', 'Platform', 'yt-dlp', 'PySide6', 'FFmpeg', 'ffprobe', 'Deno', 'yt-dlp backend'):
            self.assertIn(key, log)
            self.assertIn(f'{key}: {info[key]}', report)
        self.assertIn(APP_VERSION, info['Application'])
        self.assertIn('test-qt', report)

    def test_failed_session_write_is_retried_without_reprobing(self):
        with patch.object(diagnostics.subprocess, 'run', side_effect=self.tool_output) as run, \
                patch.object(utils, 'log_error', side_effect=[False, True]) as write:
            diagnostics.initialize_runtime_diagnostics('ffmpeg', 'ffprobe')
            diagnostics.initialize_runtime_diagnostics('ffmpeg', 'ffprobe')
            diagnostics.initialize_runtime_diagnostics('ffmpeg', 'ffprobe')
        self.assertEqual(run.call_count, 3)
        self.assertEqual(write.call_count, 2)

    def test_tool_probe_failures_do_not_prevent_report_export(self):
        outcomes = [(subprocess.TimeoutExpired('ffmpeg', 2), 'timed out'),
                    (PermissionError('private path'), 'PermissionError'),
                    (SimpleNamespace(returncode=3, stdout=''), 'exit code 3'),
                    (SimpleNamespace(returncode=0, stdout='not a version'), 'unexpected version output')]
        for outcome, message in outcomes:
            with self.subTest(message=message):
                diagnostics._RUNTIME_INFO = None
                kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch.object(diagnostics.subprocess, 'run', **kwargs):
                    report = diagnostics.build_diagnostic_report()
                self.assertIn(message, report)
                self.assertIn('Application:', report)

    def test_missing_tools_and_pyside_metadata_are_explicit(self):
        with patch.object(utils, 'which_executable', return_value=None), \
                patch.object(utils, 'portable_deno_path', return_value=None), \
                patch.object(diagnostics.metadata, 'version', side_effect=diagnostics.metadata.PackageNotFoundError), \
                patch.object(diagnostics.subprocess, 'run') as run:
            info = diagnostics.get_runtime_info()
        self.assertEqual(info['FFmpeg'], 'unavailable (not found)')
        self.assertEqual(info['ffprobe'], 'unavailable (not found)')
        self.assertEqual(info['PySide6'], 'unavailable (not installed)')
        self.assertEqual(info['yt-dlp backend'], 'external executable (bundled EJS)')
        self.assertEqual(info['Deno'], 'unavailable (not found)')
        run.assert_not_called()
