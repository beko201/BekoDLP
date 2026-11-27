"""Application dependency checks."""
from pathlib import Path
import subprocess
import sys
import unittest

import process_utils


class RuntimeDependencyTests(unittest.TestCase):
    def test_application_needs_neither_tk_nor_pillow(self):
        code = """
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('tkinter', 'PIL', 'yt_dlp', 'yt_dlp_ejs', 'curl_cffi'):
        raise AssertionError('retired UI dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import main
assert callable(main.main)
assert 'gui' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15, **process_utils.NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
