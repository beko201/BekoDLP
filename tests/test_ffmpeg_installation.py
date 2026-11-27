"""Portable tool precedence and verified archive path checks."""
import os
import unittest
from unittest.mock import Mock, patch

from tools.install_ffmpeg import VERSION, validate_members, replace_directory
import runtime_tools


class PortableFFmpegTests(unittest.TestCase):
    def test_transient_windows_lock_retries_the_same_directory_move(self):
        source, destination = Mock(), Mock()
        source.replace.side_effect = [PermissionError('locked'), None]
        with patch('tools.install_ffmpeg.time.sleep') as sleep:
            replace_directory(source, destination)
        self.assertEqual(source.replace.call_count, 2)
        source.replace.assert_called_with(destination)
        sleep.assert_called_once_with(.2)

    def test_persistent_lock_and_other_errors_are_not_silently_ignored(self):
        for failure, attempts in ((PermissionError('locked'), 30), (OSError('failed'), 1)):
            source = Mock()
            source.replace.side_effect = failure
            with patch('tools.install_ffmpeg.time.sleep'), self.assertRaises(type(failure)):
                replace_directory(source, Mock())
            self.assertEqual(source.replace.call_count, attempts)

    def test_archive_paths_stay_within_expected_release_directory(self):
        root = f'ffmpeg-{VERSION}-full_build'
        validate_members([root + '/', root + '/bin/ffmpeg.exe', root + '/LICENSE'])
        for member in ('../ffmpeg.exe', '/ffmpeg.exe', 'C:/ffmpeg.exe',
                       root + '/../../file', 'unexpected/bin/ffmpeg.exe', root + '/a:b'):
            with self.subTest(member=member), self.assertRaises(ValueError):
                validate_members([member])

    @unittest.skipUnless(os.name == 'nt', 'Windows portable runtime')
    def test_app_local_tool_takes_precedence_over_an_older_system_install(self):
        with patch('runtime_tools.os.path.isfile', return_value=True), \
                patch('runtime_tools.shutil.which', return_value='C:/old/ffmpeg.exe') as system:
            result = runtime_tools.which_executable(['ffmpeg', 'ffmpeg.exe'])
        self.assertTrue(result.endswith(os.path.join('tools', 'runtime', 'ffmpeg', 'bin', 'ffmpeg.exe')))
        system.assert_not_called()

    def test_missing_portable_build_keeps_system_tool_fallback(self):
        with patch('runtime_tools.os.path.isfile', return_value=False), \
                patch('runtime_tools.shutil.which', return_value='C:/system/ffprobe.exe'):
            self.assertEqual(runtime_tools.which_executable(['ffprobe']), 'C:/system/ffprobe.exe')
