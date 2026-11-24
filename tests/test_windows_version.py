"""Check the actual PyInstaller version resource parser without building an EXE."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file

from app_info import APP_NAME, APP_VERSION, APP_EXECUTABLE
from tools import windows_version


class WindowsVersionTests(unittest.TestCase):
    def parse(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'version-info.txt'
            path.write_text(windows_version.version_resource(), encoding='utf-8')
            return load_version_info_from_text_file(str(path))

    def test_file_and_product_versions_match_application_identity(self):
        resource = self.parse()
        parts = tuple(int(part) for part in APP_VERSION.split('.'))
        parts += (0,) * (4 - len(parts))
        self.assertEqual(resource.ffi.fileVersionMS, (parts[0] << 16) | parts[1])
        self.assertEqual(resource.ffi.fileVersionLS, (parts[2] << 16) | parts[3])
        self.assertEqual(resource.ffi.productVersionMS, resource.ffi.fileVersionMS)
        self.assertEqual(resource.ffi.productVersionLS, resource.ffi.fileVersionLS)
        strings = {entry.name: entry.val for entry in resource.kids[0].kids[0].kids}
        self.assertEqual(strings['FileVersion'], APP_VERSION)
        self.assertEqual(strings['ProductVersion'], APP_VERSION)
        self.assertEqual(strings['ProductName'], APP_NAME)
        self.assertEqual(strings['OriginalFilename'], APP_EXECUTABLE)

    def test_future_app_version_updates_both_resource_versions(self):
        with patch.object(windows_version, 'APP_VERSION', '2.4.6'):
            resource = self.parse()
        self.assertEqual(resource.ffi.fileVersionMS, (2 << 16) | 4)
        self.assertEqual(resource.ffi.fileVersionLS, 6 << 16)
        strings = {entry.name: entry.val for entry in resource.kids[0].kids[0].kids}
        self.assertEqual(strings['FileVersion'], '2.4.6')
        self.assertEqual(strings['ProductVersion'], '2.4.6')

    def test_invalid_or_unrepresentable_windows_versions_are_rejected(self):
        for version in ('v1.1.1', '1.2.3.4.5', '1.65536.0'):
            with self.subTest(version=version), patch.object(windows_version, 'APP_VERSION', version):
                with self.assertRaises(ValueError):
                    windows_version.version_resource()
