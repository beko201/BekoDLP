"""Exercise folder replacement and rollback using real Windows executables."""
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
import zipfile
import io
from tests.package_fixture import package_payload
from package_layout import MANIFEST_NAME, file_sha256
from utils import NO_WINDOW


@unittest.skipUnless(sys.platform == 'win32', 'Windows update helper')
class UpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yd-update-helper-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "Beko's app"
        self.root.mkdir()
        self.target = self.root / 'BekoDLP.exe'
        self.target.write_bytes(b'original executable')
        for name in ('_internal', 'tools', 'licenses'):
            (self.root / name).mkdir()
            (self.root / name / 'old.txt').write_text('old')
        self.user_files = ('yd_settings.json', 'debug.log', 'downloads/video.mp4', 'notes.txt')
        for name in self.user_files:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'USER DATA')
        self.stage = self.folder / '.bekodlp-update-test'
        self.stage.mkdir()
        self.script = self.stage / 'apply.ps1'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'tools/apply_update.ps1', self.script)
        self.powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'

    def prepare(self, executable=b'not an executable'):
        content = package_payload(executable=executable)
        (self.stage / 'package.zip').write_bytes(content)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                path = self.stage / 'payload' / info.filename.removeprefix('BekoDLP/')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(info))
        self.data = {'pid': 2147483647, 'target': str(self.target),
                     'sha256': hashlib.sha256(content).hexdigest(),
                     'original_sha256': file_sha256(self.target),
                     'manifest_sha256': file_sha256(self.stage / 'payload' / MANIFEST_NAME),
                     'health_token': 'a' * 64}

    def run_helper(self):
        config = self.stage / 'update.json'
        config.write_text(json.dumps(self.data), encoding='utf-8')
        return subprocess.run([str(self.powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                               '-File', str(self.script), '-Config', str(config), '-NoDialog'],
                              capture_output=True, text=True, timeout=45, **NO_WINDOW)

    def assert_preserved(self):
        self.assertEqual(self.target.read_bytes(), b'original executable')
        for name in ('_internal', 'tools', 'licenses'):
            self.assertEqual((self.root / name / 'old.txt').read_text(), 'old')
        for name in self.user_files:
            self.assertEqual((self.root / name).read_bytes(), b'USER DATA')

    def test_checksum_failure_preserves_all_original_files(self):
        self.prepare()
        self.data['sha256'] = '0' * 64
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()
        self.assertTrue((self.stage / 'update-error.txt').exists())

    def test_payload_tampering_preserves_original(self):
        self.prepare()
        (self.stage / 'payload/tools/deno.exe').write_bytes(b'tampered')
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()

    def test_invalid_executable_rolls_back_entire_folder(self):
        self.prepare()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()

    def compile_fixture(self, acknowledge):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        if not compiler.exists(): self.skipTest('C# compiler unavailable')
        source = self.folder / 'fixture.cs'
        source.write_text('using System; using System.IO; class Fixture { static void Main() { '
            + ('File.WriteAllText(Environment.GetEnvironmentVariable("BEKODLP_UPDATE_HEALTH_FILE"), '
               'Environment.GetEnvironmentVariable("BEKODLP_UPDATE_HEALTH_TOKEN"));' if acknowledge else '')
            + ' } }', encoding='utf-8')
        output = self.folder / 'fixture.exe'
        result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                                 '/out:' + str(output), str(source)], capture_output=True, text=True, timeout=20, **NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_bytes()

    def test_real_folder_swap_restart_cleanup_and_user_data_preserved(self):
        executable = self.compile_fixture(True)
        self.prepare(executable)
        result = self.run_helper()
        error = self.stage / 'update-error.txt'
        self.assertEqual(result.returncode, 0, result.stderr + (error.read_text(encoding='utf-8-sig') if error.exists() else ''))
        self.assertEqual(self.target.read_bytes(), executable)
        self.assertFalse(self.stage.exists())
        for name in ('_internal', 'tools', 'licenses'):
            self.assertFalse((self.root / name / 'old.txt').exists())
        for name in self.user_files:
            self.assertEqual((self.root / name).read_bytes(), b'USER DATA')

    def test_startup_without_acknowledgement_rolls_back(self):
        self.prepare(self.compile_fixture(False))
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()

    def test_launcher_survives_parent_exit_and_completes_installation(self):
        executable = self.compile_fixture(True)
        self.prepare(executable)
        shutil.copyfile(self.script, self.root / 'tools/apply_update.ps1')
        # Exercise the production launcher in a separate process, then let that
        # process exit just as the GUI does. The helper must run without a console.
        launcher = '''
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
from updates import launch_update
stage = SimpleNamespace(verified=True, folder=Path(sys.argv[1]),
    target=Path(sys.argv[2]), release=SimpleNamespace(sha256=sys.argv[3]),
    payload=Path(sys.argv[1]) / 'payload')
with patch('updates.can_self_update', return_value=True), \\
     patch('updates.sys.executable', str(stage.target)):
    launch_update(stage)
'''
        result = subprocess.run([sys.executable, '-c', launcher, str(self.stage),
                                 str(self.target), self.data['sha256']],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15, **NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 20
        error = self.stage / 'update-error.txt'
        while self.stage.exists() and not error.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse(self.stage.exists(),
                         error.read_text(encoding='utf-8-sig') if error.exists()
                         else 'The background helper did not complete installation after its parent exited.')
        self.assertEqual(self.target.read_bytes(), executable)
        for name in self.user_files:
            self.assertEqual((self.root / name).read_bytes(), b'USER DATA')

    def test_locked_directory_rolls_back_previously_moved_roots(self):
        import ctypes
        from ctypes import wintypes
        self.prepare()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # Deny delete/rename sharing on tools/, after EXE and _internal move.
        handle = kernel.CreateFileW(str(self.root / 'tools'), 0, 3, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            result = self.run_helper()
        finally:
            kernel.CloseHandle(handle)
        self.assertNotEqual(result.returncode, 0)
        self.assert_preserved()
        self.assertFalse((self.stage / 'rollback/BekoDLP.exe').exists(), 'Previously moved EXE was not restored.')
