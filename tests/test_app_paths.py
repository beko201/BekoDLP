"""Portable tool discovery must ignore similarly named files in the current folder."""
from contextlib import chdir
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import app_paths
from utils import portable_deno_path, which_executable


class PortablePathTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows portable tools')
    def test_frozen_tools_and_notices_use_executable_folder_from_another_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / 'BekoDLP'
            elsewhere = root / 'elsewhere'
            for folder in (install / 'tools', install / 'licenses', elsewhere / 'tools'):
                folder.mkdir(parents=True)
            for name in ('ffmpeg.exe', 'ffprobe.exe', 'deno.exe'):
                (install / 'tools' / name).write_bytes(b'installed')
                (elsewhere / 'tools' / name).write_bytes(b'wrong tool')
            with chdir(elsewhere), patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(install / 'BekoDLP.exe')):
                self.assertEqual(app_paths.install_root(), install.resolve())
                self.assertEqual(app_paths.licenses_root(), (install / 'licenses').resolve())
                self.assertEqual(Path(portable_deno_path()), (install / 'tools/deno.exe').resolve())
                for name in ('ffmpeg', 'ffprobe'):
                    self.assertEqual(Path(which_executable([name])), (install / 'tools' / (name + '.exe')).resolve())

    def test_source_resource_roots_do_not_depend_on_cwd(self):
        with tempfile.TemporaryDirectory() as temp, chdir(temp):
            root = Path(app_paths.__file__).resolve().parent
            self.assertEqual(app_paths.install_root(), root)
            self.assertEqual(app_paths.tools_root(), root / 'tools/runtime')
            self.assertEqual(app_paths.licenses_root(), root / 'build/release-notices')
