"""Compare local one-file and folder builds until their main window is visible."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from process_utils import NO_WINDOW


def main():
    legacy = ROOT / 'build/legacy-onefile/BekoDLP-Windows-x64.exe'
    if not legacy.is_file(): raise SystemExit('No retained one-file build to compare.')
    folder = ROOT / 'build/startup-comparison' / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True)
    old = folder / 'onefile'
    old.mkdir()
    os.link(legacy, old / legacy.name)
    new = folder / 'onedir'
    shutil.copytree(ROOT / 'dist/BekoDLP', new)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    samples = {'onefile': [], 'onedir': []}
    for _ in range(3):
        for profile, executable in (('onefile', old / legacy.name), ('onedir', new / 'BekoDLP.exe')):
            env = dict(os.environ)
            env.pop('QT_QPA_PLATFORM', None)
            start = time.perf_counter()
            process = subprocess.Popen([str(executable)], cwd=executable.parent, env=env, **NO_WINDOW)
            owner = psutil.Process(process.pid)
            handle = None
            try:
                while time.perf_counter() - start < 60:
                    if process.poll() is not None: raise RuntimeError('Application exited before creating a window.')
                    pids = {process.pid} | {p.pid for p in owner.children(recursive=True)}
                    found = []
                    @callback_type
                    def inspect(window, unused):
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
                        if pid.value in pids and user32.IsWindowVisible(window):
                            title = ctypes.create_unicode_buffer(256)
                            user32.GetWindowTextW(window, title, 256)
                            if title.value == 'BekoDLP': found.append(window)
                        return True
                    user32.EnumWindows(inspect, 0)
                    if found:
                        handle = found[0]
                        break
                    time.sleep(.025)
                if handle is None: raise RuntimeError('No visible main window within 60 seconds.')
                duration = round(time.perf_counter() - start, 3)
                samples[profile].append(duration)
                print(profile, duration, 'seconds', flush=True)
                user32.PostMessageW(handle, 0x0010, 0, 0)
                if process.wait(timeout=25) != 0: raise RuntimeError('Application did not shut down cleanly.')
            finally:
                if process.poll() is None:
                    for child in owner.children(recursive=True):
                        try: child.terminate()
                        except psutil.NoSuchProcess: pass
                    process.terminate()
    report = {'window_visible_seconds': samples,
              'median_seconds': {name: statistics.median(values) for name, values in samples.items()},
              'method': 'Three alternating launches per profile, same machine. Filesystem cache not cleared; not a cold-start benchmark.'}
    (folder / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
