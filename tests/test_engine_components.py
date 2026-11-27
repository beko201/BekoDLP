"""The component extraction preserves public imports and headless operation."""
from pathlib import Path
import subprocess
import sys
import unittest

from process_utils import NO_WINDOW


class EngineComponentTests(unittest.TestCase):
    def test_legacy_imports_still_expose_the_canonical_models_and_resolvers(self):
        import download_engine
        import engine_metadata
        import task_manager
        import task_types
        import task_workers

        for name in ('YTInfo', 'YTFormat', 'fetch_ytinfo_with_retry', '_extract_urls_from_input'):
            self.assertIs(getattr(download_engine, name), getattr(engine_metadata, name))
        for name in ('TaskOptions', 'TaskSummary', 'TaskEvent', 'TaskEventQueue'):
            self.assertIs(getattr(task_manager, name), getattr(task_types, name))
        self.assertIs(task_manager.Worker, task_workers.Worker)
        self.assertIs(task_manager.PlaylistFetcher, task_workers.PlaylistFetcher)

    def test_components_run_without_gui_or_embedded_downloader_imports(self):
        code = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'PySide6', 'PyQt6', 'tkinter', 'yt_dlp', 'yt_dlp_ejs', 'curl_cffi'}:
        raise AssertionError('Unexpected core dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from task_manager import TaskManager, TaskOptions
manager = TaskManager(engine_factory=lambda manager: object())
ids = manager.add_urls(['https://example.com/video'], TaskOptions(folder='.'))
assert ids == ['T1']
assert manager.cancel_task(ids[0])
manager.close()
assert manager.finish_shutdown()
'''
        result = subprocess.run([sys.executable, '-c', code],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, timeout=15, **NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
