"""Credential persistence uses real user DPAPI; failures never become plaintext."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import proxy_secrets
from proxy_secrets import SecretProtectionError, protect_document, unprotect_document
from settings import SettingsStore
from queue_store import QueueStore
from task_manager import TaskOptions
from saved_profiles import capture_profile
from utils import _log_text

URL = 'socks5h://test-user:private-proxy-password-791@127.0.0.1:1080'


def queue_document(folder, url=URL):
    return {'schema_version': 1, 'tasks': [{'id': 'T1', 'url': 'https://example.com/video',
        'state': 'pending', 'options': asdict(TaskOptions(folder=str(folder),
        proxy_mode='custom', proxy_url=url))}]}


class ProxyProtectionPolicyTests(unittest.TestCase):
    def test_secret_free_data_does_not_require_windows_or_change_format(self):
        document = {'proxy_url': 'http://localhost:8080', 'tasks': []}
        with patch('proxy_secrets._crypt', side_effect=AssertionError('Must not encrypt')):
            self.assertEqual(protect_document(document), document)
            self.assertEqual(unprotect_document(document), document)

    def test_unsupported_or_malformed_secret_record_is_not_reset_to_direct_connection(self):
        for value in ({'protection': 'unknown', 'data': 'abc'},
                      {'protection': 'windows-dpapi-v1', 'data': 'invalid@base64'},
                      {'protection': 'windows-dpapi-v1', 'data': ''}):
            with self.subTest(value=value), self.assertRaises(SecretProtectionError):
                unprotect_document({'proxy_url': value})

    def test_no_plaintext_fallback_when_encryption_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'settings.json')
            with patch('proxy_secrets._crypt', side_effect=SecretProtectionError('Unavailable')):
                with self.assertRaises(SecretProtectionError):
                    SettingsStore(path).save({'proxy_mode': 'custom', 'proxy_url': URL})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_profiles_do_not_capture_connection_credentials(self):
        self.assertNotIn('proxy_url', capture_profile({'proxy_url': URL, 'proxy_mode': 'custom'}))

    def test_logs_mask_username_only_percent_encoded_and_embedded_at_credentials(self):
        for value in ('http://private-token@host:8080', 'socks5://user:p@ssword@host:1080',
                      'https://encoded%3Asecret@host:8080'):
            with self.subTest(value=value):
                self.assertEqual(_log_text(value), value.split('://')[0] + '://[REDACTED]@' + value.rsplit('@', 1)[1])


@unittest.skipUnless(os.name == 'nt', 'Windows current-user DPAPI')
class WindowsProxySecretTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            sample = proxy_secrets._crypt(b'profile availability check')
            assert proxy_secrets._crypt(sample, decrypt=True) == b'profile availability check'
        except SecretProtectionError as error:
            raise unittest.SkipTest('The current test token cannot access its Windows DPAPI profile.') from error

    def assert_protected(self, path):
        text = path.read_text(encoding='utf-8')
        self.assertNotIn('private-proxy-password-791', text)
        self.assertNotIn('test-user', text)
        self.assertIn('windows-dpapi-v1', text)

    def test_native_round_trip_preserves_nested_urls_and_does_not_mutate_input(self):
        original = {'proxy_url': URL, 'nested': [{'proxy_url': URL}], 'other': 'keep'}
        before = deepcopy(original)
        protected = protect_document(original)
        self.assertEqual(original, before)
        self.assertEqual(unprotect_document(protected), original)
        self.assertNotIn(URL, json.dumps(protected))

    def test_repeated_task_credentials_use_one_crypto_call_per_document(self):
        data = {'tasks': [{'proxy_url': URL} for _ in range(50)]}
        with patch('proxy_secrets._crypt', wraps=proxy_secrets._crypt) as crypt:
            protected = protect_document(data)
            self.assertEqual(crypt.call_count, 1)
        with patch('proxy_secrets._crypt', wraps=proxy_secrets._crypt) as crypt:
            self.assertEqual(unprotect_document(protected), data)
            self.assertEqual(crypt.call_count, 1)

    def test_unicode_credentials_reopen_in_a_fresh_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'settings.json')
            url = 'http://kullanıcı:şifre%40deneme@127.0.0.1:8080'
            SettingsStore(path).save({'proxy_mode': 'custom', 'proxy_url': url})
            command = ('import sys; from settings import SettingsStore; '
                       'data, warnings = SettingsStore(sys.argv[1]).load(); '
                       'assert data["proxy_url"] == sys.argv[2]; print("OK")')
            result = subprocess.run([sys.executable, '-c', command, str(path), url],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'OK')

    def test_settings_primary_backup_reload_and_unchanged_save(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder, 'settings.json'))
            store.save({'proxy_mode': 'custom', 'proxy_url': URL})
            store.save({'workers': 4})
            self.assert_protected(store.path)
            self.assert_protected(store.backup)
            loaded = SettingsStore(store.path)
            self.assertEqual(loaded.load()[0]['proxy_url'], URL)
            before = store.path.read_bytes()
            self.assertFalse(loaded.save({}))
            self.assertEqual(store.path.read_bytes(), before)

    def test_legacy_settings_and_backup_migrate_without_other_field_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder, 'settings.json'))
            legacy = {'proxy_mode': 'custom', 'proxy_url': URL, 'custom': {'keep': [1, 2]}}
            for path in (store.path, store.backup):
                path.write_text(json.dumps(legacy), encoding='utf-8')
            data, warnings = store.load()
            self.assertEqual(data['proxy_url'], URL)
            self.assertEqual(warnings, [])
            for path in (store.path, store.backup):
                self.assert_protected(path)
                self.assertEqual(unprotect_document(json.loads(path.read_text())), legacy)

    def test_queue_primary_backup_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = QueueStore(Path(folder, 'queue.json'))
            document = queue_document(folder)
            for path in (store.path, store.backup):
                path.write_text(json.dumps(document), encoding='utf-8')
            loaded, warnings = store.load()
            self.assertEqual(warnings, [])
            self.assertEqual(loaded['tasks'][0]['options']['proxy_url'], URL)
            for path in (store.path, store.backup):
                self.assert_protected(path)
            loaded['tasks'][0]['title'] = 'New title'
            store.save(loaded)
            self.assert_protected(store.path)
            self.assert_protected(store.backup)
            self.assertEqual(QueueStore(store.path).load()[0]['tasks'][0]['title'], 'New title')

    def test_unreadable_user_credentials_preserve_files_and_disable_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SettingsStore(Path(folder, 'settings.json'))
            settings.save({'proxy_mode': 'custom', 'proxy_url': URL})
            queue = QueueStore(Path(folder, 'queue.json'))
            queue.save(queue_document(folder))
            for store in (settings, queue):
                original = store.path.read_bytes()
                with patch('proxy_secrets._crypt', side_effect=SecretProtectionError('Other user')):
                    with self.assertRaises(SecretProtectionError):
                        store.load()
                self.assertFalse(store.writable)
                self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(list(Path(folder).glob('*.corrupt-*')), [])

    def test_encryption_failure_does_not_change_existing_primary_or_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SettingsStore(Path(folder, 'settings.json'))
            store.save({'proxy_mode': 'custom', 'proxy_url': URL})
            store.save({'workers': 4})
            before = store.path.read_bytes(), store.backup.read_bytes()
            original_crypt = proxy_secrets._crypt
            def fail_encrypt(data, *, decrypt=False):
                if not decrypt:
                    raise SecretProtectionError('Unavailable')
                return original_crypt(data, decrypt=True)
            with patch('proxy_secrets._crypt', side_effect=fail_encrypt):
                with self.assertRaises(SecretProtectionError):
                    store.save({'workers': 5})
            self.assertEqual((store.path.read_bytes(), store.backup.read_bytes()), before)
