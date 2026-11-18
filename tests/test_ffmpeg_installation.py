"""Portable tool precedence and verified archive path checks."""
import os
import unittest
from unittest.mock import patch

from tools.install_ffmpeg import VERSION, validate_members
import utils


class PortableFFmpegTests(unittest.TestCase):
    def test_archive_paths_stay_within_expected_release_directory(self):
        root = f'ffmpeg-{VERSION}-full_build'
        validate_members([root + '/', root + '/bin/ffmpeg.exe', root + '/LICENSE'])
        for member in ('../ffmpeg.exe', '/ffmpeg.exe', 'C:/ffmpeg.exe',
                       root + '/../../file', 'unexpected/bin/ffmpeg.exe', root + '/a:b'):
            with self.subTest(member=member), self.assertRaises(ValueError):
                validate_members([member])

    @unittest.skipUnless(os.name == 'nt', 'Windows portable runtime')
    def test_app_local_tool_takes_precedence_over_an_older_system_install(self):
        with patch('utils.os.path.isfile', return_value=True), \
                patch('utils.shutil.which', return_value='C:/old/ffmpeg.exe') as system:
            result = utils.which_executable(['ffmpeg', 'ffmpeg.exe'])
        self.assertTrue(result.endswith(os.path.join('tools', 'runtime', 'ffmpeg', 'bin', 'ffmpeg.exe')))
        system.assert_not_called()

    def test_missing_portable_build_keeps_system_tool_fallback(self):
        with patch('utils.os.path.isfile', return_value=False), \
                patch('utils.shutil.which', return_value='C:/system/ffprobe.exe'):
            self.assertEqual(utils.which_executable(['ffprobe']), 'C:/system/ffprobe.exe')
