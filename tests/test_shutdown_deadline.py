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
from test_shutdown import wait_until
import utils


class ShutdownDeadlineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        log_patch = patch.object(utils, 'ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.engine = download_engine.DownloadEngine(
            self.manager, ffmpeg=shutil.which('ffmpeg') or sys.executable,
            ffprobe=shutil.which('ffprobe') or sys.executable)
        self.manager.engine = self.engine
        self.tid = self.manager.add_urls(['https://example.com/video'], TaskOptions(folder=str(self.folder)))[0]
        self.clock = 0
        self.manager._shutdown_clock = lambda: self.clock
        self.stuck = Mock()
        self.stuck.is_alive.return_value = True
        self.stuck.join.side_effect = AssertionError('shutdown must not join a stuck thread')
        self.manager.workers = [self.stuck]

    def escalate(self):
        self.clock = 5
        self.manager.finish_shutdown()
        self.manager._shutdown_force_thread.join(1)


    def test_shutdown_wait_limits_must_be_finite(self):
        for value in (-1, float('inf'), float('nan')):
            for option in ('shutdown_grace_seconds', 'shutdown_force_seconds'):
                with self.subTest(value=value, option=option), self.assertRaises(ValueError):
                    TaskManager(engine_factory=lambda manager: Mock(), **{option: value})

    def test_managed_capture_failure_preserves_diagnostics_and_unregisters_process(self):
        processes = []

        def register(proc):
            processes.append(proc)
            self.engine._register_process(proc)

        with self.assertRaises(subprocess.CalledProcessError) as error:
            utils.run_cmd_capture([sys.executable, '-c', "import sys; print('diagnostic'); sys.exit(3)"],
                                  on_start=register, on_finish=self.engine._unregister_process)
        self.assertIn('diagnostic', error.exception.output)
        self.assertEqual(self.engine.managed_processes, set())
        self.assertEqual(processes[0].returncode, 3)

    def test_capture_timeout_kills_and_unregisters_process(self):
        processes = []

        def register(proc):
            processes.append(proc)
            self.engine._register_process(proc)

        with self.assertRaises(subprocess.TimeoutExpired):
            utils.run_cmd_capture([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.1,
                                  on_start=register, on_finish=self.engine._unregister_process)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertEqual(self.engine.managed_processes, set())

    def test_second_phase_kills_a_real_managed_capture_process(self):
        entered = threading.Event()
        results = []
        processes = []

        def register(proc):
            self.engine._register_process(proc)
            processes.append(proc)
            entered.set()

        def run():
            try:
                utils.run_cmd_capture([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=60,
                                      on_start=register, on_finish=self.engine._unregister_process)
            except BaseException as error:
                results.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.stop = Mock()
        self.manager.workers = [thread]
        self.addCleanup(self.engine.terminate_managed_processes)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.manager.close()
        self.escalate()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(self.engine.managed_processes, set())
        self.assertIsInstance(results[0], subprocess.CalledProcessError)
        self.manager.finish_shutdown()
        self.assertTrue(self.manager.finish_shutdown())

    def test_process_started_during_forced_shutdown_is_killed_and_unregistered(self):
        self.engine.terminate_managed_processes()
        processes = []

        def register(proc):
            processes.append(proc)
            self.engine._register_process(proc)

        with self.assertRaises(utils.DownloadCancelled):
            utils.run_cmd_capture([sys.executable, '-c', 'import time; time.sleep(60)'],
                                  on_start=register, on_finish=self.engine._unregister_process)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(self.engine.managed_processes, set())

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
    def test_forced_phase_kills_suspended_ffmpeg_even_when_output_pump_is_stuck(self):
        release = threading.Event()
        entered = threading.Event()
        results = []
        self.addCleanup(release.set)

        def stuck_pump(*args, **kwargs):
            entered.set()
            release.wait(5)
            return []

        def run():
            try:
                self.engine._run_ffmpeg(self.tid, 1, [self.engine.ffmpeg, '-loglevel', 'quiet',
                    '-re', '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '60', '-f', 'null', '-'])
            except BaseException as error:
                results.append(error)

        with patch.object(self.engine, '_pump_ffmpeg_progress', side_effect=stuck_pump):
            thread = threading.Thread(target=run, daemon=True)
            thread.stop = Mock()
            self.manager.workers = [thread]
            self.addCleanup(self.engine.terminate_managed_processes)
            thread.start()
            self.assertTrue(entered.wait(2))
            with self.engine.resource_lock:
                process = next(iter(self.engine.managed_processes))
            utils.psutil.Process(process.pid).suspend()
            self.manager.close()
            self.escalate()
            wait_until(lambda: process.poll() is not None)
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], utils.DownloadCancelled)
        self.assertEqual(self.engine.managed_processes, set())
        self.assertEqual(self.engine.current_process_pids, {})


if __name__ == '__main__':
    unittest.main()
