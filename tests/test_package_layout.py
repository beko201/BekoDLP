import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from tests.package_fixture import package_payload
from package_layout import extract_package, inspect_package, parse_manifest, safe_relative, MANIFEST_NAME


class PackageLayoutTests(unittest.TestCase):
    def rewrite(self, *, extra=None, transform=None):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(package_payload())) as source, zipfile.ZipFile(output, 'w') as target:
            for info in source.infolist():
                content = source.read(info)
                if transform: content = transform(info.filename, content)
                target.writestr(info, content)
            if extra:
                for name, content in extra:
                    target.writestr(name, content)
        return output.getvalue()

    def reject_zip(self, content):
        with zipfile.ZipFile(io.BytesIO(content)) as archive, self.assertRaises(ValueError):
            inspect_package(archive)

    def test_rejects_user_files_and_windows_path_ambiguities(self):
        for path in ('../file', '/file', '_internal/../file', 'yd_settings.json', 'downloads/a.mp4',
                     '_internal/C:stream', '_internal/CON.txt', '_internal/a.', '_internal/a//b',
                     '_internal\\outside', 'package-manifest.json/child'):
            with self.subTest(path=path), self.assertRaises(ValueError): safe_relative(path)

    def test_rejects_extra_files_case_collisions_and_symlinks(self):
        for name in ('BekoDLP/yd_settings.json', 'BekoDLP/_internal/PYTHON313.dll', '../outside'):
            self.reject_zip(self.rewrite(extra=[(name, b'evil')]))
        link = zipfile.ZipInfo('BekoDLP/_internal/link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.reject_zip(self.rewrite(extra=[(link, b'../../outside')]))

    def test_missing_runtime_manifest_and_duplicate_json_keys(self):
        with zipfile.ZipFile(io.BytesIO(package_payload())) as archive:
            manifest = json.loads(archive.read('BekoDLP/' + MANIFEST_NAME))
        manifest['files'] = [row for row in manifest['files'] if row['path'] != '_internal/python313.dll']
        with self.assertRaises(ValueError): parse_manifest(json.dumps(manifest).encode())
        with self.assertRaises(ValueError): parse_manifest(b'{"schema":1,"schema":1}')

    def test_valid_package_extracts_and_checks_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'package.zip'
            archive.write_bytes(package_payload())
            extract_package(archive, root / 'payload', expected_version='v1.1.0')
            self.assertTrue((root / 'payload/tools/deno.exe').exists())
            self.assertFalse((root / 'payload/yd_settings.json').exists())
            with self.assertRaises(ValueError): extract_package(archive, root / 'wrong', expected_version='1.2')
            self.assertFalse((root / 'wrong').exists())

    def test_missing_ytdlp_rejected_even_when_removed_from_manifest_and_archive(self):
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(package_payload())) as source, zipfile.ZipFile(output, 'w') as target:
            for member in source.infolist():
                if member.filename == 'BekoDLP/tools/yt-dlp.exe':
                    continue
                content = source.read(member)
                if member.filename == 'BekoDLP/' + MANIFEST_NAME:
                    manifest = json.loads(content)
                    manifest['files'] = [row for row in manifest['files'] if row['path'] != 'tools/yt-dlp.exe']
                    content = json.dumps(manifest).encode()
                target.writestr(member, content)
        self.reject_zip(output.getvalue())

    def test_hash_mismatch_is_rejected_during_extraction(self):
        content = self.rewrite(transform=lambda name, content: b'xxxxxxx' if name.endswith('deno.exe') else content)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'package.zip'
            archive.write_bytes(content)
            with self.assertRaisesRegex(ValueError, 'SHA-256'): extract_package(archive, root / 'payload')

    def test_cancel_and_low_disk_space_prevent_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'package.zip'
            archive.write_bytes(package_payload())
            cancel = threading.Event()
            cancel.set()
            with self.assertRaisesRegex(ValueError, 'cancelled'): extract_package(archive, root / 'payload', cancel_event=cancel)
            with patch('shutil.disk_usage') as usage:
                usage.return_value.free = 0
                with self.assertRaises(OSError): extract_package(archive, root / 'full')

    def test_unpacked_size_and_file_directory_collisions_are_rejected(self):
        with zipfile.ZipFile(io.BytesIO(package_payload())) as archive:
            manifest = json.loads(archive.read('BekoDLP/' + MANIFEST_NAME))
        manifest['files'][0]['size'] = 5 * 1024 ** 3
        with self.assertRaises(ValueError): parse_manifest(json.dumps(manifest).encode())
        self.reject_zip(package_payload(extra={'_internal': b'file'}))
