import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from settings import SettingsStore, DEFAULTS, validate_settings, SettingsFileTooLarge


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name, 'settings.json')

    def test_old_preferences_migrate_and_unknown_fields_survive(self):
        self.path.write_text(json.dumps({'workers': 5, 'mp3_bitrate': '320k', 'custom': 'keep'}))
        store = SettingsStore(self.path)
        data, warnings = store.load()
        self.assertFalse(warnings)
        self.assertEqual(data['workers'], 5)
        store.save({'format': 'MP3'})
        self.assertEqual(json.loads(self.path.read_text())['custom'], 'keep')

    def test_invalid_types_and_choices_use_defaults(self):
        data, issues = validate_settings({'workers': True, 'format': [], 'quality': '8K',
                                         'last_folder': None, 'log_backup_count': -1})
        self.assertEqual(len(issues), 5)
        for key in issues:
            self.assertEqual(data[key], DEFAULTS[key])

    def test_historical_codec_preferences_migrate_without_losing_selection(self):
        for legacy, expected in (("Orijinal (Otomatik)", "Original (Automatic)"),
                                 ("H.264 (Uyumluluk)", "H.264 (Compatibility)")):
            with self.subTest(codec=legacy):
                self.path.write_text(json.dumps({'codec_mode': legacy, 'custom': 'keep'}))
                store = SettingsStore(self.path)
                data, warnings = store.load()
                self.assertFalse(warnings)
                self.assertEqual(data['codec_mode'], expected)
                self.assertTrue(store.save({}))
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved['codec_mode'], expected)
                self.assertEqual(saved['custom'], 'keep')
                self.assertFalse(SettingsStore(self.path).load()[1])

    def test_unchanged_legacy_file_still_gets_schema_on_save(self):
        self.path.write_text('{"workers": 3}')
        store = SettingsStore(self.path)
        store.load()
        self.assertTrue(store.save({}))
        self.assertEqual(json.loads(self.path.read_text())['schema_version'], 1)

    def test_failed_replace_preserves_original_and_removes_temporary_file(self):
        store = SettingsStore(self.path)
        store.save({'workers': 4})
        original = self.path.read_bytes()
        with patch('settings.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                store.save({'workers': 5})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_corrupt_primary_is_preserved_and_backup_recovers(self):
        store = SettingsStore(self.path)
        store.save({'workers': 4})
        store.save({'workers': 5})
        self.path.write_text('{broken')
        recovered = SettingsStore(self.path)
        data, warnings = recovered.load()
        self.assertEqual(data['workers'], 4)
        self.assertTrue(warnings)
        self.assertEqual(next(self.path.parent.glob('*.corrupt-*')).read_text(), '{broken')
        recovered.save({'format': 'MP3'})
        self.assertEqual(json.loads(self.path.read_text())['workers'], 4)

    def test_future_schema_is_never_overwritten(self):
        self.path.write_text('{"schema_version": 99, "workers": 5}')
        before = self.path.read_bytes()
        store = SettingsStore(self.path)
        with self.assertRaises(ValueError):
            store.load()
        self.assertFalse(store.save({'workers': 3}))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_primary_recovers_from_backup(self):
        store = SettingsStore(self.path)
        store.save({'workers': 4})
        store.save({'workers': 5})
        self.path.unlink()
        self.assertEqual(SettingsStore(self.path).load()[0]['workers'], 4)

    def test_unchanged_settings_do_not_replace_file(self):
        store = SettingsStore(self.path)
        store.save({})
        with patch('settings.os.replace') as replace:
            self.assertFalse(store.save({}))
        replace.assert_not_called()

    def test_corruption_after_load_is_preserved_before_save(self):
        store = SettingsStore(self.path)
        store.save({'workers': 4})
        store.load()
        self.path.write_text('[]')
        store.save({'workers': 5})
        self.assertEqual(next(self.path.parent.glob('*.corrupt-*')).read_text(), '[]')
        self.assertEqual(json.loads(self.path.read_text())['workers'], 5)

    def test_invalid_root_recovers_and_read_failure_blocks_save(self):
        self.path.write_text('[]')
        store = SettingsStore(self.path)
        self.assertTrue(store.load()[1])
        self.assertEqual(store.data['workers'], DEFAULTS['workers'])
        with patch('settings._read', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                store.load()
        self.assertFalse(store.save({'workers': 5}))

    def test_failure_replacing_primary_keeps_complete_old_file(self):
        import os
        store = SettingsStore(self.path)
        store.save({'workers': 4})
        replace = os.replace

        def fail_primary(source, target):
            if Path(target) == self.path:
                raise PermissionError('primary locked')
            return replace(source, target)

        with patch('settings.os.replace', side_effect=fail_primary):
            with self.assertRaises(PermissionError):
                store.save({'workers': 5})
        self.assertEqual(json.loads(self.path.read_text())['workers'], 4)
        self.assertEqual(json.loads(store.backup.read_text())['workers'], 4)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_duplicate_keys_and_nonfinite_numbers_preserve_original_and_recover_backup(self):
        documents = ['{"workers": 4, "workers": 9}',
                     '{"custom": {"key": 1, "key": 2}}',
                     '{"custom": NaN}', '{"custom": Infinity}', '{"custom": -Infinity}',
                     '{"custom": 1e999}', '{"custom": -1e999}']
        for document in documents:
            with self.subTest(document=document):
                self.path.with_suffix('.json.bak').write_text('{"workers": 6}', encoding='utf-8')
                self.path.write_text(document, encoding='utf-8')
                store = SettingsStore(self.path)
                data, warnings = store.load()
                self.assertEqual(data['workers'], 6)
                self.assertTrue(warnings)
                self.assertEqual(self.path.read_text(encoding='utf-8'), document)
                self.assertTrue(any(path.read_text(encoding='utf-8') == document
                                    for path in self.path.parent.glob('*.corrupt-*')))

    def test_oversized_file_remains_untouched_and_blocks_save(self):
        content = '{"custom": "' + 'x' * 1024 + '"}'
        self.path.write_text(content, encoding='utf-8')
        store = SettingsStore(self.path)
        with patch('settings.MAX_SETTINGS_BYTES', 256):
            with self.assertRaises(SettingsFileTooLarge):
                store.load()
        self.assertFalse(store.writable)
        self.assertFalse(store.save({'workers': 4}))
        self.assertEqual(self.path.read_text(encoding='utf-8'), content)
        self.assertFalse(list(self.path.parent.glob('*.corrupt-*')))

    def test_deeply_nested_json_recovers_instead_of_crashing(self):
        self.path.write_text('{"custom":' + '[' * 2000 + '0' + ']' * 2000 + '}', encoding='utf-8')
        store = SettingsStore(self.path)
        data, warnings = store.load()
        self.assertTrue(warnings)
        self.assertEqual(data, DEFAULTS)
        self.assertTrue(list(self.path.parent.glob('*.corrupt-*')))

    def test_nested_custom_values_are_not_shared_with_callers(self):
        self.path.write_text('{"custom": {"items": [1]}}', encoding='utf-8')
        store = SettingsStore(self.path)
        data, _ = store.load()
        data['custom']['items'].append(2)
        self.assertEqual(store.data['custom']['items'], [1])
        changes = {'custom': {'items': [3]}}
        store.save(changes)
        changes['custom']['items'].append(4)
        self.assertEqual(store.data['custom']['items'], [3])

    def test_external_edits_to_unchanged_fields_survive_partial_save(self):
        store = SettingsStore(self.path)
        store.save({'workers': 4, 'custom': {'setting': 'original'}})
        external = json.loads(self.path.read_text(encoding='utf-8'))
        external.update(quality='720p', custom={'setting': 'external edit'}, new_key='keep')
        self.path.write_text(json.dumps(external), encoding='utf-8')
        store.save({'workers': 5})
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['workers'], 5)
        self.assertEqual(saved['quality'], '720p')
        self.assertEqual(saved['custom'], {'setting': 'external edit'})
        self.assertEqual(saved['new_key'], 'keep')

    def test_unserializable_or_oversized_updates_leave_primary_and_backup_intact(self):
        store = SettingsStore(self.path)
        store.save({'workers': 4})
        store.save({'workers': 5})
        before = self.path.read_bytes(), store.backup.read_bytes()
        for value in (float('nan'), float('inf'), {1, 2}, 'x' * (1024 * 1024)):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                store.save({'custom': value})
            self.assertEqual((self.path.read_bytes(), store.backup.read_bytes()), before)

    def test_explicit_reload_enables_saving_after_file_is_fixed(self):
        self.path.write_text('{"schema_version": 99}', encoding='utf-8')
        store = SettingsStore(self.path)
        with self.assertRaises(ValueError):
            store.load()
        self.path.write_text('{"workers": 6}', encoding='utf-8')
        self.assertEqual(store.load()[0]['workers'], 6)
        self.assertTrue(store.writable)
        self.assertTrue(store.save({'workers': 7}))

    def test_repeat_interval_range_and_malformed_legacy_codec_are_validated(self):
        for value in (True, 0, 3601, '60', None):
            with self.subTest(value=value):
                data, issues = validate_settings({'log_repeat_seconds': value})
                self.assertIn('log_repeat_seconds', issues)
                self.assertEqual(data['log_repeat_seconds'], 60)
        for value in (1, 30, 3600):
            self.assertEqual(validate_settings({'log_repeat_seconds': value})[0]['log_repeat_seconds'], value)
        data, issues = validate_settings({'container': 'ogg', 'audio_codec': []})
        self.assertIn('audio_codec', issues)
        self.assertEqual(data['container'], 'auto')
