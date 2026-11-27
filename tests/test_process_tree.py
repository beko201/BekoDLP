"""Containment starts before execution, with failure cleanup and idempotent kill."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import process_tree
from process_tree import spawn_owned_process
from process_utils import NO_WINDOW, psutil


class ProcessTreeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')
    def test_assignment_failure_kills_suspended_process_before_user_code_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'executed.txt'
            processes = []
            popen = subprocess.Popen
            def launch(*args, **kwargs):
                proc = popen(*args, **kwargs)
                processes.append(proc)
                self.assertTrue(kwargs['creationflags'] & 4)
                return proc
            with patch.object(process_tree.subprocess, 'Popen', side_effect=launch), \
                    patch.object(process_tree.WindowsJob, 'assign', side_effect=OSError('assignment failed')):
                with self.assertRaisesRegex(OSError, 'assignment failed'):
                    spawn_owned_process([sys.executable, '-c', 'from pathlib import Path; import sys; '
                        'Path(sys.argv[1]).write_text("escaped")', str(marker)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
            self.assertFalse(marker.exists())
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdout.closed)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')
    def test_closing_job_kills_child_after_parent_exits_without_enumeration(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / 'child.pid'
            script = ('import subprocess,sys; from pathlib import Path; '
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                'Path(sys.argv[1]).write_text(str(p.pid))')
            proc = spawn_owned_process([sys.executable, '-c', script, str(pidfile)],
                stdout=subprocess.PIPE, **NO_WINDOW)
            child = None
            try:
                proc.wait(5)
                child = psutil.Process(int(pidfile.read_text()))
                self.assertTrue(child.is_running())
                proc._owned_process_tree.close()
                child.wait(3)
                self.assertFalse(child.is_running())
                self.assertEqual(proc.stdout.read(), b'')
                proc._owned_process_tree.terminate()
                proc._owned_process_tree.close()
            finally:
                proc._owned_process_tree.close()
                proc.stdout.close()
                if child is not None:
                    try:
                        child.kill()
                        child.wait(3)
                    except psutil.NoSuchProcess:
                        pass

    def test_posix_branch_creates_session_and_kills_group_even_after_parent_exit(self):
        fake = Mock(pid=54321)
        with patch.object(process_tree, 'IS_WINDOWS', False), \
                patch.object(process_tree.subprocess, 'Popen', return_value=fake) as popen, \
                patch.object(process_tree.os, 'killpg', create=True) as killpg, \
                patch.object(signal, 'SIGKILL', 9, create=True):
            proc = spawn_owned_process(['fixture'], stdout=subprocess.PIPE)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            proc.poll.return_value = 0
            proc._owned_process_tree.terminate()
            proc._owned_process_tree.terminate()
            proc._owned_process_tree.close()
            killpg.assert_called_once_with(54321, 9)

    def test_posix_missing_group_is_already_cleaned_up(self):
        with patch.object(process_tree.os, 'killpg', create=True, side_effect=ProcessLookupError), \
                patch.object(signal, 'SIGKILL', 9, create=True):
            group = process_tree.PosixProcessGroup(54321)
            group.terminate()
            self.assertTrue(group.terminated)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')
    def test_force_cleanup_includes_job_whose_original_parent_already_exited(self):
        import threading
        from engine_runtime import EngineRuntime
        from engine_processes import MediaProcesses
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / 'child.pid'
            script = ('import subprocess,sys; from pathlib import Path; '
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                'Path(sys.argv[1]).write_text(str(p.pid))')
            proc = spawn_owned_process([sys.executable, '-c', script, str(pidfile)],
                stdout=subprocess.PIPE, **NO_WINDOW)
            child = None
            try:
                proc.wait(5)
                child = psutil.Process(int(pidfile.read_text()))
                runtime = EngineRuntime(ffmpeg=None, ffprobe=None, managed_processes={proc})
                engine = MediaProcesses(runtime, Mock(), Mock())
                engine.terminate_managed_processes()
                child.wait(3)
                self.assertFalse(child.is_running())
            finally:
                proc._owned_process_tree.close()
                proc.stdout.close()
                if child is not None:
                    try:
                        child.kill()
                        child.wait(3)
                    except psutil.NoSuchProcess:
                        pass
