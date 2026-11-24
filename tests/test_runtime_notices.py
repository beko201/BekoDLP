import unittest
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
from unittest.mock import patch
from tools.verify_runtime_notices import (license_text, repository_name, linked_archive_license,
                                         dependency_declarations, dependency_scope)
from tools.resolve_notice_origins import manifest_identity


class RuntimeNoticeTests(unittest.TestCase):
    def test_license_body_detection_rejects_html_and_metadata_only(self):
        self.assertFalse(license_text(b'<html>404: Not Found</html>'))
        self.assertFalse(license_text(b'license = "MIT"'))
        self.assertTrue(license_text(b'MIT License\nCopyright (c) Example\nPermission is hereby granted'))
        self.assertTrue(license_text(b'Apache License\nVersion 2.0, January 2004'))

    def test_repository_is_parsed_without_using_a_current_branch(self):
        self.assertEqual(repository_name('https://github.com/owner/repo.git'), 'owner/repo')
        self.assertEqual(repository_name('https://github.com/owner/repo/tree/main/component'), 'owner/repo')
        self.assertIsNone(repository_name('https://notgithub.example/owner/repo'))

    def linked_notice(self, pointer, target='vendor/LICENSE', metadata_type='symlink', blob=None, regular=True):
        notice = b'MIT License\nCopyright (c) Example\nPermission is hereby granted'
        git_blob = hashlib.sha1(b'blob ' + str(len(pointer)).encode() + b'\0' + pointer).hexdigest()
        metadata = json.dumps({'type': metadata_type, 'target': target, 'sha': blob or git_blob}).encode()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, 'example.crate')
            with tarfile.open(source, 'w:gz') as archive:
                member = tarfile.TarInfo('example-1.0/vendor/LICENSE')
                if regular:
                    member.size = len(notice)
                    archive.addfile(member, io.BytesIO(notice))
                else:
                    member.type = tarfile.SYMTYPE
                    member.linkname = '../../outside/LICENSE'
                    archive.addfile(member)
            with patch('tools.verify_runtime_notices.fetch', return_value=(metadata, {'status': 200})):
                return linked_archive_license(pointer, 'LICENSE', 'owner/repo', 'a' * 40,
                                              source, 'example-1.0')

    def test_exact_upstream_license_symlink_can_select_archived_dependency_notice(self):
        content, proof = self.linked_notice(b'vendor/LICENSE')
        self.assertTrue(license_text(content))
        self.assertEqual(proof['crate_notice_path'], 'vendor/LICENSE')
        self.assertIn('a' * 40, proof['upstream_symlink'])

    def test_license_pointer_cannot_escape_or_trust_unproven_symlinks(self):
        for pointer in (b'../../outside/LICENSE', b'/outside/LICENSE', b'LICENSE\nextra text'):
            with self.subTest(pointer=pointer):
                self.assertIsNone(self.linked_notice(pointer))
        self.assertIsNone(self.linked_notice(b'vendor/LICENSE', metadata_type='file'))
        self.assertIsNone(self.linked_notice(b'vendor/LICENSE', blob='0' * 40))
        self.assertIsNone(self.linked_notice(b'vendor/LICENSE', target='other/LICENSE'))
        self.assertIsNone(self.linked_notice(b'vendor/LICENSE', regular=False))

    def test_workspace_identity_resolves_license_without_guessing(self):
        package = {'name': 'example', 'version': '1.0', 'license': {'workspace': True}}
        root = {'workspace': {'package': {'license': 'Apache-2.0'}}}
        self.assertEqual(manifest_identity(package, root),
                         {'name': 'example', 'version': '1.0', 'license': 'Apache-2.0'})
        self.assertIsNone(manifest_identity(package, {})['license'])

    def test_scope_preserves_target_dev_build_optional_and_renamed_dependencies(self):
        document = {'dependencies': {'renamed': {'workspace': True}},
                    'target': {'cfg(target_os = "macos")': {
                        'build-dependencies': {'example': '1'},
                        'dev-dependencies': {'example': '1'}}}}
        workspace = {'workspace': {'dependencies': {
            'renamed': {'package': 'example', 'version': '1', 'optional': True}}}}
        rows = dependency_declarations(document, {'example'}, workspace)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]['optional'])
        self.assertEqual(rows[0]['dependency'], 'example')
        self.assertIsNone(rows[0]['target'])
        self.assertEqual(rows[1]['target'], 'cfg(target_os = "macos")')
        self.assertEqual({r['kind'] for r in rows},
                         {'dependencies', 'build-dependencies', 'dev-dependencies'})

    def test_dependency_scope_rejects_tampered_crate_before_reading_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'deno-crates').mkdir()
            (base / 'deno-crates/example.crate').write_bytes(b'tampered')
            record = {'filename': 'example.crate', 'sha256': '0' * 64}
            with patch('tools.verify_runtime_notices.BASE', base), self.assertRaisesRegex(ValueError, 'checksum'):
                dependency_scope([record], {'example'})
