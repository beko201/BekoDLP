"""Source-notice collection must preserve evidence without inventing clearance."""
from pathlib import Path, PurePosixPath
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools.audit_release_licenses import (extract_notices, notice_name, cargo_notice_gaps,
    code_identity, validate_identity, verify_package_folder, normalized_code, verify_release_archive, preflight)


class ReleaseLicenseAuditTests(unittest.TestCase):
    def test_release_zip_hashes_are_verified_beyond_manifest_sizes(self):
        from tests.package_fixture import package_payload
        from package_layout import MANIFEST_NAME
        payload = package_payload()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'package.zip'
            path.write_bytes(payload)
            with zipfile.ZipFile(path) as archive:
                manifest = archive.read('BekoDLP/' + MANIFEST_NAME)
            with patch('tools.audit_release_licenses.ROOT', root):
                self.assertTrue(verify_release_archive(path, manifest)['matches_assessed_folder'])
                with zipfile.ZipFile(io.BytesIO(payload)) as original, zipfile.ZipFile(path, 'w') as changed:
                    for info in original.infolist():
                        data = b'changed' if info.filename == 'BekoDLP/tools/deno.exe' else original.read(info)
                        changed.writestr(info, data)
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    verify_release_archive(path, manifest)

    def test_preflight_keeps_baseline_version_and_never_claims_a_target_binary_review(self):
        from app_info import APP_VERSION
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / 'build/license-audit'
            base.mkdir(parents=True)
            baseline = {'version': '1.0', 'scope': {'exe_sha256': 'a' * 64},
                        'deno_package_notices_unverified': [], 'open_items': []}
            source = json.dumps(baseline)
            (base / 'review.json').write_text(source)
            with patch('tools.audit_release_licenses.ROOT', root):
                report = preflight()
            self.assertEqual(report['version'], APP_VERSION)
            self.assertEqual(report['baseline']['version'], '1.0')
            self.assertFalse(report['target_binary_review_complete'])
            self.assertEqual(report['phase'], 'prebuild')
            self.assertEqual((base / 'review.json').read_text(), source)
            self.assertFalse((root / 'dist').exists())

    def test_evidence_packaging_rejects_an_old_review_before_creating_output(self):
        from tools import package_license_evidence
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / 'build/license-audit'
            base.mkdir(parents=True)
            (base / 'review.json').write_text(json.dumps({'application': 'BekoDLP', 'version': '0.0.0'}))
            with patch('tools.package_license_evidence.ROOT', root), self.assertRaisesRegex(ValueError, 'does not match'):
                package_license_evidence.main()
            self.assertFalse((root / 'dist').exists())

    def test_identity_is_read_without_executing_application_code(self):
        code = compile("APP_NAME='BekoDLP'\nAPP_VERSION='1.1'\nAPP_EXECUTABLE='BekoDLP.exe'\n"
                       "raise AssertionError('Application code must not execute')", '<fixture>', 'exec')
        identity = code_identity(code)
        self.assertEqual(identity['APP_VERSION'], '1.1')
        validate_identity({'app': 'BekoDLP', 'version': '1.1'}, identity, '1.1')
        with self.assertRaisesRegex(ValueError, 'executable is 1.1'):
            validate_identity({'app': 'BekoDLP', 'version': '1.1'}, identity, '1.1.1')
        with self.assertRaisesRegex(ValueError, 'do not match'):
            validate_identity({'app': 'BekoDLP', 'version': '1.1.1'}, identity, '1.1.1')

    def test_computed_identity_is_not_guessed_or_executed(self):
        code = compile("APP_NAME='BekoDLP'\nAPP_VERSION=input()\nAPP_EXECUTABLE='BekoDLP.exe'", '<fixture>', 'exec')
        with self.assertRaisesRegex(ValueError, 'literal'):
            code_identity(code)

    def test_source_comparison_ignores_paths_but_preserves_nested_constants(self):
        first = compile('def value():\n    return 1\n', 'original/path.py', 'exec')
        other_path = compile('def value():\n    return 1\n', 'different/path.py', 'exec')
        changed = compile('def value():\n    return 2\n', 'different/path.py', 'exec')
        self.assertEqual(normalized_code(first), normalized_code(other_path))
        self.assertNotEqual(normalized_code(first), normalized_code(changed))

    def test_package_file_hashes_detect_same_size_changes_and_extra_managed_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'LICENSE'
            path.write_bytes(b'original')
            manifest = {'files': [{'path': 'LICENSE', 'size': 8,
                                  'sha256': hashlib.sha256(b'original').hexdigest()}]}
            verify_package_folder(root, manifest)
            (root / 'personal-notes.txt').write_text('User file')
            verify_package_folder(root, manifest)
            path.write_bytes(b'modified')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                verify_package_folder(root, manifest)
            path.write_bytes(b'original')
            (root / 'tools').mkdir()
            (root / 'tools/extra.dll').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'file list'):
                verify_package_folder(root, manifest)

    def record(self, notice, **extra):
        return dict(name='example', version='1.0', url='https://static.crates.io/crates/example/example-1.0.crate',
                    sha256='a' * 64, notices=[notice], **extra)

    def test_nested_dependency_notice_cannot_clear_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'example-1.0/vendor/LICENSE')
            path.parent.mkdir(parents=True)
            path.write_text('Nested dependency license')
            self.assertEqual(len(cargo_notice_gaps([self.record('example-1.0/vendor/LICENSE')], folder)), 1)

    def test_missing_notice_file_does_not_clear_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(len(cargo_notice_gaps([self.record('example-1.0/LICENSE')], folder)), 1)

    def test_upstream_notice_requires_matching_digest(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'example-1.0/upstream-LICENSE')
            path.parent.mkdir()
            path.write_bytes(b'Original license')
            record = self.record('example-1.0/upstream-LICENSE', upstream_notice_sources=[{'url': 'https://upstream/LICENSE', 'sha256': '0' * 64}])
            self.assertEqual(len(cargo_notice_gaps([record], folder)), 1)
            record['upstream_notice_sources'][0]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(cargo_notice_gaps([record], folder), [])

    def test_dirty_base_notice_stays_unverified_but_archive_notice_is_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'example-1.0/upstream-LICENSE')
            path.parent.mkdir()
            path.write_bytes(b'Base commit license')
            record = self.record('example-1.0/upstream-LICENSE', vcs={'git': {'dirty': True}})
            missing = cargo_notice_gaps([record], folder)
            self.assertIn('dirty', missing[0]['reason'])
            (path.parent / 'LICENSE').write_bytes(b'Published archive license')
            record['notices'].append('example-1.0/LICENSE')
            self.assertEqual(cargo_notice_gaps([record], folder), [])

    def test_notice_path_cannot_escape_or_borrow_another_package(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('../LICENSE', 'other-1.0/LICENSE'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    cargo_notice_gaps([self.record(name)], folder)

    def test_algorithm_source_is_not_mistaken_for_a_license(self):
        self.assertFalse(notice_name(PurePosixPath('v8/src/compiler/turboshaft/copying-phase.cc')))
        self.assertFalse(notice_name(PurePosixPath('v8/src/compiler/turboshaft/copying-phase.h')))
        self.assertTrue(notice_name(PurePosixPath('crate/LICENSE-MIT')))
        self.assertTrue(notice_name(PurePosixPath('qt/LICENSES/BSD-3-Clause.txt')))
        self.assertTrue(notice_name(PurePosixPath('qt/src/3rdparty/qt_attribution.json')))

    def test_exact_notice_bytes_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'source.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('component/LICENSE', b'Original copyright and license text\n')
                archive.writestr('component/source.c', b'not a standalone license')
            result = extract_notices(source, base / 'notices')
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].read_bytes(), b'Original copyright and license text\n')

    def test_notice_paths_cannot_escape_the_output_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'source.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('../LICENSE', 'outside')
            with self.assertRaises(ValueError):
                extract_notices(source, base / 'notices')
            self.assertFalse((base / 'LICENSE').exists())

    def test_oversized_notice_entries_are_not_unboundedly_extracted(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'source.zip'
            with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('component/LICENSE', b'x' * (4 * 1024 * 1024 + 1))
            self.assertEqual(extract_notices(source, base / 'notices'), [])
