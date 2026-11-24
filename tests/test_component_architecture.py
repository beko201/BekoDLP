from dataclasses import FrozenInstanceError
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import download_engine
from task_manager import TaskManager, TaskOptions
import utils


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Component operation timed out')


class ComponentArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        logs = patch.object(utils, 'ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        logs.start()
        self.addCleanup(logs.stop)

    def manage(self, manager):
        def close():
            manager.close()
            wait_until(manager.is_idle)
            self.assertTrue(manager.finish_shutdown())
        self.addCleanup(close)
        return manager

    def test_cancelled_task_cleanup_preserves_readded_url_reservation(self):
        manager = self.manage(TaskManager())
        url = 'https://example.com/video'
        options = TaskOptions(folder=str(self.folder))
        old_tid = manager.add_urls([url], options)[0]
        self.assertTrue(manager.cancel_task(old_tid))
        new_tid = manager.add_urls([url], options)[0]

        with patch('engine_metadata.fetch_ytinfo_with_retry') as fetch:
            manager._process_task(manager.task_queue.get_nowait(), 1)
            manager.task_queue.task_done()
        fetch.assert_not_called()
        self.assertEqual(manager.get_task(old_tid)['status'], 'Cancelled')
        self.assertEqual(manager.get_task(new_tid)['status'], 'Queued')
        self.assertEqual(manager.add_urls([url], options), [])
        self.assertEqual(manager.task_queue.qsize(), 1)
        self.assertTrue(manager.cancel_task(new_tid))
        self.assertEqual(len(manager.add_urls([url], options)), 1)

    def test_clearing_cancelled_task_preserves_readded_url_reservation(self):
        manager = self.manage(TaskManager())
        url = 'https://example.com/video'
        options = TaskOptions(folder=str(self.folder))
        old_tid = manager.add_urls([url], options)[0]
        manager.cancel_task(old_tid)
        new_tid = manager.add_urls([url], options)[0]

        self.assertEqual(manager.clear_list(), [old_tid])
        self.assertIsNotNone(manager.get_task(new_tid))
        self.assertEqual(manager.add_urls([url], options), [])

    def test_engine_and_manager_import_and_run_without_tkinter(self):
        code = """
import sys
sys.modules['tkinter'] = None
from task_manager import TaskManager, TaskOptions
manager = TaskManager()
assert manager.is_idle()
assert manager.add_urls(['https://example.com/a'], TaskOptions(folder='.')) == ['T1']
manager.close()
assert manager.finish_shutdown()
assert 'gui' not in sys.modules
"""
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=15, **utils.NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_required_packages_fail_clearly_when_missing(self):
        for module, package, entry in [('psutil', 'psutil', 'utils'),
                                       ('unidecode', 'Unidecode', 'utils')]:
            with self.subTest(package=package):
                code = f"""
import builtins
original_import = builtins.__import__
def import_without_package(name, *args, **kwargs):
    if name == {module!r} or name.startswith({module!r} + '.'):
        raise ModuleNotFoundError('simulated missing package')
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_package
try:
    __import__({entry!r})
except ImportError as error:
    assert {package!r} in str(error), str(error)
    assert 'required' in str(error), str(error)
else:
    raise AssertionError('Application silently continued without a required package')
"""
                result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                        capture_output=True, text=True, timeout=15, **utils.NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_ffmpeg_distribution_requires_both_executables(self):
        for missing in ({'ffmpeg'}, {'ffprobe'}, {'ffmpeg', 'ffprobe'}):
            with self.subTest(missing=missing):
                def discover(names):
                    return None if names[0] in missing else str(self.folder / names[0])
                with patch.object(download_engine, 'which_executable', side_effect=discover):
                    with self.assertRaisesRegex(RuntimeError, 'FFmpeg is required') as caught:
                        TaskManager()
                for tool in missing:
                    self.assertIn(tool, str(caught.exception))


    def test_task_settings_and_event_snapshots_are_independent(self):
        manager = self.manage(TaskManager())
        options = TaskOptions(folder=str(self.folder), format='MP3', mp3_bitrate='128k')
        with self.assertRaises(FrozenInstanceError):
            options.mp3_bitrate = '320k'
        manager.add_urls(['https://example.com/a'], options)
        manager.add_warning('T1', 'test warning')
        snapshot = manager.get_task('T1')
        snapshot['mp3_bitrate'] = '320k'
        snapshot['warnings'].append('external change')
        self.assertEqual(manager.get_task('T1')['mp3_bitrate'], '128k')
        self.assertEqual(manager.get_task('T1')['warnings'], ['test warning'])


    def test_headless_worker_resize_preserves_unique_ids_and_task_cancellation(self):
        class BlockingEngine:
            def __init__(self, manager):
                self.manager = manager

            def process_task(self, task, worker_id):
                tid, url = task
                self.manager.update_task(tid, status=f'Worker{worker_id}: processing')
                while not self.manager.is_cancelled(tid):
                    time.sleep(0.01)
                self.manager.update_task(tid, status='Cancelled')

        manager = self.manage(TaskManager(engine_factory=BlockingEngine))
        options = TaskOptions(folder=str(self.folder))
        manager.set_worker_count(3)
        manager.add_urls([f'https://example.com/{i}' for i in range(3)], options)
        wait_until(lambda: len(manager.tid_to_workerid) == 3)
        manager.set_worker_count(1)
        self.assertEqual(manager.retiring_workers, [])
        manager.set_worker_count(3)
        self.assertEqual([worker.worker_id for worker in manager.workers], [1, 2, 3])
        manager.add_urls(['https://example.com/3', 'https://example.com/4'], options)
        self.assertEqual(set(manager.tid_to_workerid.values()), {1, 2, 3})
        old_tid = next(tid for tid, wid in manager.tid_to_workerid.items() if wid == 2)
        new_tid = next(tid for tid, wid in manager.tid_to_workerid.items() if wid == 3)
        self.assertTrue(manager.cancel_task(old_tid))
        wait_until(lambda: manager.get_task(old_tid)['status'] == 'Cancelled')
        wait_until(lambda: any(tid not in {'T1', 'T2', 'T3'} for tid in manager.tid_to_workerid))
        self.assertFalse(manager.is_cancelled(new_tid))


    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe are required')
    def test_headless_manager_runs_real_mp3_conversion_trim_and_publication(self):
        source = self.folder / 'input.wav'
        target = self.folder / 'downloads'
        target.mkdir()
        subprocess.run([shutil.which('ffmpeg'), '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **utils.NO_WINDOW)
        manager = self.manage(TaskManager())
        info = download_engine.YTInfo('offline', {'title': 'Offline audio', 'duration': 1,
                'formats': [{'format_id': 'audio', 'ext': 'wav', 'vcodec': 'none',
                             'acodec': 'pcm_s16le', 'filesize': source.stat().st_size}]})

        def download(tid, key, url, stream, filename, *, folder):
            destination = Path(folder, filename)
            shutil.copyfile(source, destination)
            return str(destination)

        manager.engine._download_format = download
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info):
            manager.set_worker_count(1)
            manager.add_urls(['https://example.com/offline'], TaskOptions(
                folder=str(target), format='MP3', mp3_bitrate='128k', trim_start='0.1', trim_end='0.6'))
            wait_until(lambda: manager.get_task('T1')['status'].startswith(('Completed', 'Error'))
                       and not manager.tid_to_workerid)
        task = manager.get_task('T1')
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        output = Path(task['output_path'])
        self.assertEqual(list(target.iterdir()), [output])
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(manager.engine.current_process_pids, {})


if __name__ == '__main__':
    unittest.main()
