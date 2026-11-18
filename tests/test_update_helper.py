"""Execute the Windows swap helper against isolated disposable executable files."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from utils import NO_WINDOW


@unittest.skipUnless(sys.platform == 'win32', 'Windows update helper')
class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yd-update-helper-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.target = self.folder / "Beko's app.exe"
        self.target.write_bytes(b'original executable')
        self.stage = self.folder / '.bekodlp-update-test'
        self.stage.mkdir()
        self.source = self.stage / 'update.exe'
        self.script = self.stage / 'apply.ps1'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'tools/apply_update.ps1', self.script)
        self.powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'

    def run_helper(self, *, expected=None):
        content = self.source.read_bytes()
        config = self.stage / 'update.json'
        config.write_text(json.dumps({'pid': 2147483647, 'target': str(self.target),
            'sha256': expected or hashlib.sha256(content).hexdigest(),
            'original_sha256': hashlib.sha256(self.target.read_bytes()).hexdigest()}), encoding='utf-8')
        return subprocess.run([str(self.powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                               '-File', str(self.script), '-Config', str(config), '-NoDialog'],
                              capture_output=True, text=True, timeout=35, **NO_WINDOW)

    def test_checksum_failure_preserves_original_and_records_failure(self):
        self.source.write_bytes(b'changed executable')
        result = self.run_helper(expected='0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_bytes(), b'original executable')
        self.assertTrue((self.stage / 'update-error.txt').exists())

    def test_invalid_executable_is_rolled_back_when_launch_fails(self):
        self.source.write_bytes(b'not an executable')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        detail = (self.stage / 'update-error.txt').read_text(encoding='utf-8-sig')
        self.assertEqual(self.target.read_bytes(), b'original executable', detail)
        self.assertTrue((self.stage / 'update-error.txt').exists())

    def test_real_executable_swap_restart_and_cleanup(self):
        compiler = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.exists():
            self.skipTest('The Windows .NET Framework C# compiler is unavailable.')
        code = self.folder / 'UpdateFixture.cs'
        code.write_text('using System; using System.IO; public class UpdateFixture { public static void Main() { '
                        'File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "update-started.txt"), "new"); } }',
                        encoding='utf-8')
        result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                                 '/out:' + str(self.source), str(code)],
                                capture_output=True, text=True, timeout=20, **NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = self.source.read_bytes()
        result = self.run_helper()
        error_file = self.stage / 'update-error.txt'
        details = error_file.read_text(encoding='utf-8-sig') if error_file.exists() else ''
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + details)
        deadline = time.monotonic() + 8
        marker = self.folder / 'update-started.txt'
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(marker.exists(), 'The updated GUI executable did not start.')
        self.assertEqual(self.target.read_bytes(), expected)
        self.assertFalse(self.stage.exists())
