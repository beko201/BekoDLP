"""Exercise the subprocess boundary, including pause, cancellation and children."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import download_engine
from utils import DownloadCancelled, psutil
from ytdlp_process import YtDlpProcess, YtDlpProcessError, kill_process_tree


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Subprocess state did not arrive')
        time.sleep(.02)


class YtDlpProcessTests(unittest.TestCase):
    def test_metadata_file_is_removed_after_failure(self):
        paths = []
        def run(args, consume):
            paths.append(Path(args[args.index('--load-info-json') + 1]))
            self.assertEqual(json.loads(paths[-1].read_text(encoding='utf-8'))['title'], 'Türkçe başlık')
            raise YtDlpProcessError('download failed', 1)
        adapter = YtDlpProcess()
        with patch.object(adapter, '_run', side_effect=run):
            with self.assertRaises(YtDlpProcessError):
                adapter.process_ie_result({'id': 'test', 'title': 'Türkçe başlık'})
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].parent.exists())

    def test_nonzero_exit_is_an_operation_error_with_bounded_tail(self):
        adapter = YtDlpProcess()
        with self.assertRaises(YtDlpProcessError) as caught:
            adapter._run([sys.executable, '-u', '-c',
                'import sys; [print("line", i) for i in range(100)]; sys.exit(7)'], lambda line: False)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertIn('line 99', str(caught.exception))
        self.assertNotIn('line 0\n', str(caught.exception))
        self.assertLess(len(str(caught.exception)), 500)

    def test_cancel_before_start_launches_no_process(self):
        def cancel():
            raise DownloadCancelled()
        with patch('ytdlp_process.subprocess.Popen') as launch:
            with self.assertRaises(DownloadCancelled):
                YtDlpProcess(check=cancel)._run(['unused'], Mock())
        launch.assert_not_called()

    def test_pause_and_cancel_stop_the_entire_owned_process_tree(self):
        cancel, paused, started = threading.Event(), threading.Event(), threading.Event()
        processes, children, failures, finished = [], [], [], []
        def check():
            if cancel.is_set():
                raise DownloadCancelled()
        def on_start(proc):
            processes.append(proc)
        def consume(line):
            if line.startswith('child:'):
                children.append(psutil.Process(int(line.partition(':')[2])))
                started.set()
            return True
        child = 'import time; time.sleep(60)'
        script = ('import subprocess,sys,time; '
                  f'p=subprocess.Popen([sys.executable,"-c",{child!r}]); '
                  'print("child:"+str(p.pid), flush=True); time.sleep(60)')
        adapter = YtDlpProcess(check=check, is_paused=paused.is_set,
                              on_start=on_start, on_finish=lambda proc: finished.append(proc))
        def run():
            try:
                adapter._run([sys.executable, '-u', '-c', script], consume)
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            paused.set()
            wait_until(lambda: all(p.status() == psutil.STATUS_STOPPED
                                   for p in [psutil.Process(processes[0].pid), *children]))
            cancel.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(failures[0], DownloadCancelled)
            self.assertIsNotNone(processes[0].poll())
            self.assertFalse(children[0].is_running())
            self.assertEqual(finished, processes)
        finally:
            cancel.set()
            paused.clear()
            if processes:
                kill_process_tree(processes[0], children)
            thread.join(5)

    def test_parent_exit_does_not_leave_child_holding_output_pipe(self):
        children = []
        script = ('import subprocess,sys,time; '
                  'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                  'print(p.pid, flush=True); time.sleep(.4)')
        def consume(line):
            children.append(psutil.Process(int(line)))
            return True
        try:
            YtDlpProcess()._run([sys.executable, '-u', '-c', script], consume)
            self.assertFalse(children[0].is_running())
        finally:
            for child in children:
                if child.is_running():
                    child.kill()
                    child.wait(3)

    def test_real_cli_html_playlist_queues_distinct_media_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'index.html').write_text('<title>Local playlist</title>'
                '<video src="a.mp4"></video><video src="b.mp4"></video>', encoding='utf-8')
            (root / 'a.mp4').write_bytes(b'a' * 20)
            (root / 'b.mp4').write_bytes(b'b' * 20)
            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=folder))
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                queued = []
                download_engine._extract_urls_from_input(base + '/index.html', True,
                    on_batch=lambda urls: queued.extend(urls))
                self.assertEqual(queued, [base + '/a.mp4', base + '/b.mp4'])
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()

    def test_immediate_parent_exit_before_first_scan_is_cancellable(self):
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                self._immediate_parent_exit(cancelled=True)

    def test_immediate_parent_exit_without_cancel_closes_inherited_stdout(self):
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                self._immediate_parent_exit(cancelled=False)

    def _immediate_parent_exit(self, *, cancelled):
        cancel, exited, release = threading.Event(), threading.Event(), threading.Event()
        processes, children, failures, finished = [], [], [], []
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / 'child.pid'
            script = ('import os,subprocess,sys; from pathlib import Path; '
                'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                'Path(sys.argv[1]).write_text(str(p.pid)); os._exit(0)')
            def check():
                if cancel.is_set():
                    raise DownloadCancelled()
            def on_start(proc):
                processes.append(proc)
                # No descendant scan or output reader can run until this hook
                # returns. The child already outlives its original parent.
                wait_until(lambda: pidfile.exists() and proc.poll() is not None)
                children.append(psutil.Process(int(pidfile.read_text())))
                exited.set()
                if not release.wait(5):
                    raise AssertionError('Test did not release the worker')
            adapter = YtDlpProcess(check=check, on_start=on_start,
                                  on_finish=lambda proc: finished.append(proc))
            def run():
                try:
                    adapter._run([sys.executable, '-u', '-c', script, str(pidfile)], lambda line: True)
                except BaseException as error:
                    failures.append(error)
            worker = threading.Thread(target=run, daemon=True)
            # An empty enumeration makes this independent of scheduler timing.
            with patch.object(psutil.Process, 'children', return_value=[]) as enumerate_children:
                worker.start()
                try:
                    self.assertTrue(exited.wait(5), failures)
                    self.assertTrue(children[0].is_running())
                    self.assertIsNotNone(processes[0].poll())
                    enumerate_children.assert_not_called()
                    if cancelled:
                        cancel.set()
                    started = time.monotonic()
                    release.set()
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), 'Worker stayed blocked by inherited stdout')
                    self.assertLess(time.monotonic() - started, 3)
                    wait_until(lambda: not children[0].is_running(), timeout=2)
                    self.assertEqual(finished, processes)
                    if cancelled:
                        self.assertEqual(len(failures), 1)
                        self.assertIsInstance(failures[0], DownloadCancelled)
                    else:
                        self.assertEqual(failures, [])
                finally:
                    cancel.set()
                    release.set()
                    for proc in processes:
                        kill_process_tree(proc)
                    for child in children:
                        try:
                            child.kill()
                            child.wait(3)
                        except psutil.NoSuchProcess:
                            pass
                    worker.join(5)
