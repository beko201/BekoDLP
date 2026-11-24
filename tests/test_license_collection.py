"""Validate collected notice content and reject unsafe build inputs."""
from importlib.metadata import distribution
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.collect_licenses import (collect_distribution, collect_ffmpeg, collect_ytdlp, digest,
                                   runtime_distributions, reset_stale_review)


class LicenseCollectionTests(unittest.TestCase):
    def test_old_review_is_retained_and_replaced_with_an_explicit_prebuild_record(self):
        from app_info import APP_VERSION
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            previous = b'{"application": "BekoDLP", "version": "0.0.0", "status": "REVIEW_BLOCKED"}'
            (output / 'RELEASE_REVIEW.json').write_bytes(previous)
            (output / 'RELEASE_REVIEW.md').write_text('Old release review')
            reset_stale_review(output)
            current = json.loads((output / 'RELEASE_REVIEW.json').read_text())
            self.assertEqual(current['version'], APP_VERSION)
            self.assertEqual(current['phase'], 'prebuild')
            self.assertEqual(current['status'], 'REVIEW_REQUIRED')
            saved = list((output / 'verification').glob('previous-review-*.json'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), previous)
            current_bytes = (output / 'RELEASE_REVIEW.json').read_bytes()
            reset_stale_review(output)
            self.assertEqual((output / 'RELEASE_REVIEW.json').read_bytes(), current_bytes)

    def test_standalone_notices_are_bound_to_the_exact_binary_and_release(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            executable = output / 'yt-dlp.exe'
            executable.write_bytes(b'official binary fixture')
            with patch('tools.collect_licenses.require_executable', return_value=str(executable)), \
                    patch('tools.collect_licenses.executable_version', return_value='2026.10.26'), \
                    patch('tools.collect_licenses.urlopen', side_effect=lambda *a, **kw: BytesIO(b'License text')):
                first = collect_ytdlp(output, fetch=True)
                self.assertTrue(first['license_collected'])
                self.assertEqual(first['sha256'], digest(executable))
                self.assertEqual(len(first['notices']), 2)
                self.assertTrue(collect_ytdlp(output)['license_collected'])
                executable.write_bytes(b'changed executable')
                self.assertFalse(collect_ytdlp(output)['license_collected'])

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

    def test_inventory_includes_required_transitive_packages(self):
        requirements = Path(__file__).resolve().parents[1] / 'requirements.txt'
        names = {package.metadata['Name'].lower().replace('_', '-') for package in runtime_distributions(requirements)}
        self.assertTrue({'shiboken6', 'pyside6-essentials'}.issubset(names))
        self.assertNotIn('pytest', names)
        self.assertTrue({'yt-dlp', 'yt-dlp-ejs', 'curl-cffi'}.isdisjoint(names))

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
