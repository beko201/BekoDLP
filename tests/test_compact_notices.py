import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app_info import APP_NAME, APP_VERSION
from tools.prepare_release_notices import consolidate


class CompactNoticeTests(unittest.TestCase):
    def source(self, root):
        source = root / 'evidence'
        source.mkdir()
        (source / 'inventory.json').write_text(json.dumps({'application': APP_NAME, 'version': APP_VERSION,
            'status': 'REVIEW_BLOCKED', 'python_packages': []}), encoding='utf-8')
        return source

    def write(self, source, name, data):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_identical_notices_share_bytes_and_keep_each_component_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.source(root)
            original = b'Copyright Example\r\nOriginal license\r\n'
            for name in ('python/first/LICENSE', 'deno/second/LICENSE'):
                self.write(source, name, original)
            index = consolidate(source, root / 'compact')
            self.assertEqual(len(index['files']), 2)
            self.assertEqual(len(index['texts']), 1)
            text = index['texts'][0]
            content = (root / 'compact/NOTICES.txt').read_bytes()
            self.assertEqual(content[text['offset']:text['offset'] + text['size']], original)
            self.assertEqual(text['sha256'], hashlib.sha256(original).hexdigest())
            self.assertEqual(index['review_status'], 'REVIEW_BLOCKED')
            self.assertEqual(len(list((root / 'compact').iterdir())), 3)

    def test_distinct_copyright_notices_and_qt_attributions_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.source(root)
            self.write(source, 'a/LICENSE', b'Copyright Alice\nMIT')
            self.write(source, 'b/LICENSE', b'Copyright Bob\nMIT')
            self.write(source, 'source-notices/qt/qt_attribution.json', b'{"Copyright": "Qt component owner"}')
            index = consolidate(source, root / 'compact')
            self.assertEqual(len(index['texts']), 3)
            self.assertEqual(len(index['files']), 3)

    def test_investigation_records_and_accidental_cache_or_sample_images_stay_out(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.source(root)
            self.write(source, 'LICENSE', b'Original GPL text')
            self.write(source, 'verification/large-report.json', b'{"evidence": []}')
            self.write(source, 'python/pkg/__pycache__/a.pyc', b'\xffcache')
            self.write(source, 'source-notices/qt/licensewizard-example.png', b'\x89PNG')
            index = consolidate(source, root / 'compact')
            self.assertEqual([r['source'] for r in index['files']], ['LICENSE'])
            self.assertTrue((source / 'verification/large-report.json').exists())

    def test_unknown_binary_and_existing_or_overlapping_output_fail_without_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.source(root)
            self.write(source, 'LICENSE', b'License text')
            with self.assertRaises(ValueError): consolidate(source, source / 'compact')
            existing = root / 'existing'
            existing.mkdir()
            (existing / 'personal.txt').write_text('keep')
            with self.assertRaises(ValueError): consolidate(source, existing)
            self.assertEqual((existing / 'personal.txt').read_text(), 'keep')
            self.write(source, 'unknown.bin', b'\xff\xfe\x80')
            with self.assertRaises(UnicodeDecodeError): consolidate(source, root / 'new')
            self.assertFalse((root / 'new').exists())
