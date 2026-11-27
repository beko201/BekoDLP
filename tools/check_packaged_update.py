"""Reinstall the actual folder ZIP through its detached helper in an isolated copy."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_VERSION, APP_NAME, APP_EXECUTABLE
from package_layout import MANIFEST_NAME, file_sha256
from updates import Release, StagedUpdate
from process_utils import NO_WINDOW


def main():
    folder = ROOT / 'build/packaged-update-check' / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True)
    try:
        run_check(folder)
    finally:
        # Confine failure cleanup to the disposable installation created above.
        import psutil
        target = folder / APP_NAME / APP_EXECUTABLE
        for process in psutil.process_iter(['exe']):
            if process.info['exe'] and Path(process.info['exe']) == target:
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    pass


def run_check(folder):
    install = folder / APP_NAME
    shutil.copytree(ROOT / 'dist' / APP_NAME, install)
    target = install / APP_EXECUTABLE
    (install / '_internal/stale-test.dll').write_bytes(b'old DLL must disappear')
    (install / 'notes.txt').write_text('User file must survive', encoding='utf-8')
    downloads = install / 'downloads'
    downloads.mkdir()
    (downloads / 'keep.mp4').write_bytes(b'USER DOWNLOAD')
    (install / 'yd_settings.json').write_text(json.dumps({'schema_version': 1, 'language': 'tr',
        'last_folder': str(downloads)}), encoding='utf-8')
    archive = ROOT / 'dist' / f'{APP_NAME}-{APP_VERSION}-Windows-x64.zip'
    stage = StagedUpdate(Release(APP_VERSION, '', archive.stat().st_size, file_sha256(archive)), target)
    with archive.open('rb') as stream:
        while chunk := stream.read(256 * 1024): stage.write(chunk)
    stage.finish()
    shutil.copy2(install / 'tools/apply_update.ps1', stage.folder / 'apply.ps1')
    config = stage.folder / 'update.json'
    config.write_text(json.dumps({'pid': 2147483647, 'target': str(target),
        'sha256': stage.release.sha256, 'manifest_sha256': file_sha256(stage.payload / MANIFEST_NAME),
        'original_sha256': file_sha256(target), 'health_token': secrets.token_hex(32)}), encoding='utf-8')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = dict(os.environ, QT_ACCESSIBILITY='1')
    env.pop('QT_QPA_PLATFORM', None)
    started = time.perf_counter()
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(stage.folder / 'apply.ps1'), '-Config', str(config), '-NoDialog'],
        cwd=folder, env=env, capture_output=True, text=True, timeout=150, **NO_WINDOW)
    if result.returncode:
        error = stage.folder / 'update-error.txt'
        raise RuntimeError(result.stderr + (error.read_text(encoding='utf-8-sig') if error.exists() else ''))
    assert not stage.folder.exists(), 'Successful update staging was not cleaned.'
    assert not (install / '_internal/stale-test.dll').exists()
    assert (install / 'notes.txt').read_text(encoding='utf-8') == 'User file must survive'
    assert (downloads / 'keep.mp4').read_bytes() == b'USER DOWNLOAD'
    settings = json.loads((install / 'yd_settings.json').read_text(encoding='utf-8'))
    assert settings['language'] == 'tr' and settings['last_folder'] == str(downloads)
    # Close only this test application's exact path using Windows UI Automation.
    ui_config = folder / 'close.json'
    ui_config.write_text(json.dumps({'executable': str(target), 'language': 'tr', 'dump_only': True,
        'close_after': True, 'report': str(folder / 'controls.json')}), encoding='utf-8')
    closed = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(ROOT / 'tools/check_windows_ui.ps1'), '-Config', str(ui_config)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=85, **NO_WINDOW)
    if closed.returncode: raise RuntimeError(closed.stdout + closed.stderr)
    import psutil
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        running = [p for p in psutil.process_iter(['exe']) if p.info['exe'] and Path(p.info['exe']) == target]
        if not running: break
        time.sleep(.1)
    assert not running, 'The updated test app did not exit cleanly.'
    report = {'profile': 'windows-x64-onedir', 'version': APP_VERSION,
              'actual_package_reinstall': True, 'startup_acknowledged': True,
              'user_files_preserved': True, 'stale_dll_removed': True,
              'helper_and_startup_seconds': round(time.perf_counter() - started, 2),
              'note': 'Reinstalls the same local version; does not test a public GitHub version transition.'}
    (folder / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
