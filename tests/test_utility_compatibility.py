"""Legacy utility calls share the new implementations and logging context."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import app_logging
import download_support
import errors
import file_utils
import process_utils
import runtime_tools
import time_utils
import url_utils
import utils


class UtilityCompatibilityTests(unittest.TestCase):
    def test_legacy_exports_keep_function_and_exception_identity(self):
        exports = (
            (app_logging, 'log_error'), (app_logging, 'log_context'),
            (app_logging, 'configure_logging'), (app_logging, 'log_warning'),
            (file_utils, 'safe_filename'), (file_utils, 'remove_leftovers'),
            (process_utils, 'run_cmd_capture'), (runtime_tools, 'which_executable'),
            (time_utils, 'parse_timecode'), (time_utils, 'validate_trim_range'),
            (url_utils, 'normalize_url'), (errors, 'DownloadCancelled'),
            (errors, 'InsufficientDiskSpace'), (errors, 'PlaylistLinkNotAllowed'),
        )
        for owner, name in exports:
            with self.subTest(name=name):
                self.assertIs(getattr(utils, name), getattr(owner, name))

    def test_legacy_and_direct_logging_share_context_and_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'errors.log'
            with patch.object(app_logging, 'ERROR_LOG_FILE', str(path)):
                with utils.log_context(task='T1', stage='cleanup'):
                    app_logging.log_error('direct owner call')
                    utils.log_error('legacy call')
                text = path.read_text(encoding='utf-8')
                self.assertIn('direct owner call', text)
                self.assertIn('legacy call', text)
                self.assertEqual(text.count('[task=T1] [stage=cleanup]'), 2)
                self.assertEqual(utils.get_log_status()['path'], str(path))

    def test_cancellation_keeps_legacy_exception_handlers_working(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(utils.DownloadCancelled):
            download_support._network_options(cancel)['cancel_check']()
        with self.assertRaises(errors.DownloadCancelled):
            utils._check_cancelled(cancel)

    def test_url_time_and_tool_discovery_import_without_ui_or_media_dependencies(self):
        code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('PySide6', 'psutil', 'unidecode', 'utils', 'yt_dlp'):
        raise AssertionError('unexpected dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from url_utils import normalize_url
from time_utils import validate_trim_range
from runtime_tools import which_executable
assert normalize_url('youtu.be/example') == 'https://www.youtube.com/watch?v=example'
assert validate_trim_range('1:00', '1:30') == (60, 90)
assert callable(which_executable)
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=15, **process_utils.NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
