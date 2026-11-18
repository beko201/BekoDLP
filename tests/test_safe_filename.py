import ntpath
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


class SafeFilenameTests(unittest.TestCase):
    def assert_windows_safe(self, name):
        self.assertTrue(name)
        self.assertLessEqual(len(name), 200)
        self.assertFalse(name.endswith((' ', '.')))
        if hasattr(ntpath, 'isreserved'):
            self.assertFalse(ntpath.isreserved(name), name)

    def test_reserved_names_are_prefixed_even_with_extensions_and_spaces(self):
        names = ['CON', 'PRN', 'AUX', 'NUL']
        names += [f'{prefix}{digit}' for prefix in ('COM', 'LPT') for digit in '123456789¹²³']
        with patch.object(downloader, 'unidecode', SimpleNamespace(unidecode=lambda text: text)):
            for name in names:
                for spelling in (name, name.lower(), name.capitalize()):
                    for suffix in ('', '.txt', '.mp3', '.tar.gz', ' .txt'):
                        value = spelling + suffix
                        with self.subTest(value=value):
                            result = downloader.safe_filename(value)
                            self.assertEqual(result, '_' + value)
                            self.assert_windows_safe(result)

    def test_similarly_named_regular_files_are_unchanged(self):
        for name in ('CONcert', 'CON_file', '_CON', 'COM10', 'LPT10', 'COM1backup',
                     'video.AUX', 'normal.mp4', 'Example video (2026)'):
            with self.subTest(name=name):
                self.assertEqual(downloader.safe_filename(name), name)

    def test_sanitization_and_length_limit_remain_safe(self):
        cases = ['', ' . ', 'CON  . ', 'CON' + ' ' * 197 + 'truncated',
                 'CON.' + 'a' * 194 + '.a', 'a' * 201,
                 'name<>:"/\\|?*\x00', 'CONIN$', 'CONOUT$']
        for value in cases:
            with self.subTest(value=value):
                self.assert_windows_safe(downloader.safe_filename(value))
        self.assertEqual(downloader.safe_filename(' . '), 'video')

    def test_result_is_safe_before_and_after_transliteration(self):
        for transliterator in (SimpleNamespace(unidecode=lambda text: text), downloader.unidecode):
            with patch.object(downloader, 'unidecode', transliterator):
                for value in ('COM¹.mp4', 'lpt².txt', 'LPT³', 'AUX.txt'):
                    with self.subTest(value=value, transliterator=type(transliterator).__name__):
                        result = downloader.safe_filename(value)
                        self.assertTrue(result.startswith('_'))
                        self.assert_windows_safe(result)
                        self.assertEqual(downloader.safe_filename(result), result)

    def test_safe_names_create_real_regular_files(self):
        with tempfile.TemporaryDirectory() as folder:
            for title in ('CON', 'NUL', 'AUX.txt', 'PRN', 'COM1', 'lpt9', 'COM¹'):
                name = downloader.safe_filename(title) + '.mp3'
                self.assert_windows_safe(name)
                path = Path(folder, name)
                path.write_bytes(b'test audio')
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_bytes(), b'test audio')


if __name__ == '__main__':
    unittest.main()
