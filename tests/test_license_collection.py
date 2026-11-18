"""Validate collected notice content and reject unsafe build inputs."""
from importlib.metadata import distribution
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.collect_licenses import collect_distribution, collect_ffmpeg, digest, runtime_distributions


class LicenseCollectionTests(unittest.TestCase):
    def test_copies_actual_license_bytes_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            record = collect_distribution(distribution('Unidecode'), output)
            self.assertEqual(record['name'], 'Unidecode')
            self.assertTrue(record['notices'])
            for notice in record['notices']:
                target = output / notice['file']
                self.assertEqual(digest(target), notice['sha256'])
                self.assertIn('GNU GENERAL PUBLIC LICENSE', target.read_text(encoding='utf-8'))

    def test_ejs_embedded_library_copyright_and_permissions_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            record = collect_distribution(distribution('yt-dlp-ejs'), output)
            entry = next(item for item in record['notices'] if item['file'].endswith('bundled-javascript-notices.txt'))
            text = (output / entry['file']).read_text(encoding='utf-8')
            self.assertIn('meriyah', text)
            self.assertIn('ISC License', text)
            self.assertIn('Copyright', text)
            self.assertIn('Permission to use', text)

    def test_inventory_includes_required_transitive_packages(self):
        requirements = Path(__file__).resolve().parents[1] / 'requirements.txt'
        names = {package.metadata['Name'].lower().replace('_', '-') for package in runtime_distributions(requirements)}
        self.assertTrue({'cffi', 'certifi', 'pycparser', 'shiboken6', 'pyside6-essentials'}.issubset(names))
        self.assertNotIn('pytest', names)

    def test_rejects_nonfree_ffmpeg_before_copying_or_distributing(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch('tools.collect_licenses.which_executable', return_value=str(Path(temp, 'ffmpeg.exe'))), \
                patch('tools.collect_licenses.command_text', return_value='ffmpeg\nconfiguration: --enable-nonfree'):
            with self.assertRaisesRegex(ValueError, 'nonfree'):
                collect_ffmpeg(Path(temp))

    def test_unsafe_distribution_record_paths_are_not_copied(self):
        class FakeMetadata(dict):
            def get_all(self, key):
                return None
        fake = SimpleNamespace(metadata=FakeMetadata(Name='Example', License='MIT'), version='1.0',
                               files=['../../LICENSE', 'C:/outside/NOTICE'],
                               locate_file=lambda entry: self.fail('Unsafe source path was used'))
        with tempfile.TemporaryDirectory() as temp:
            record = collect_distribution(fake, Path(temp))
            self.assertEqual(record['notices'], [])
