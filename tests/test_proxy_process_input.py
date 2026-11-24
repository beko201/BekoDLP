"""Proxy secrets cross a pipe, never the process command line or a temp file."""
import json
import shlex
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from utils import DownloadCancelled, psutil
from ytdlp_process import YtDlpProcess, YtDlpProcessError


class ProxyProcessInputTests(unittest.TestCase):
    def adapter(self, url):
        return YtDlpProcess({'proxy_mode': 'custom', 'proxy_url': url}, executable='unused')

    def test_authenticated_proxy_is_absent_from_cli_for_all_supported_schemes(self):
        for scheme in ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h'):
            with self.subTest(scheme=scheme):
                url = f'{scheme}://audit-user:private-pass@localhost:8080'
                args = self.adapter(url)._arguments()
                self.assertNotIn('--proxy', args)
                self.assertNotIn('private-pass', ' '.join(args))
                self.assertEqual(args[args.index('--config-locations') + 1], '-')
                self.assertIn('--ignore-config', args)

    def test_pipe_preserves_unicode_quotes_backslashes_and_option_like_passwords(self):
        for password in ('şifre🔒', "a'b\"c\\d", 'p%23%20--exec%20bad', 'raw@password'):
            with self.subTest(password=password):
                url = f'http://audit-user:{password}@localhost:8080'
                received = []
                # A real child reads the pipe. Its command line contains no URL.
                script = 'import sys,json; print(json.dumps(sys.stdin.read()))'
                adapter = self.adapter(url)
                with patch('ytdlp_process.tempfile.TemporaryDirectory', side_effect=AssertionError('No secret files')):
                    adapter._run([sys.executable, '-c', script], lambda line: received.append(json.loads(line)) or True)
                config = shlex.split(received[0], comments=True)
                self.assertEqual(config[0], '--proxy')
                self.assertEqual(len(config), 2)
                self.assertEqual(unquote(config[1]), unquote(url))
                self.assertTrue(config[1].isascii())

    def test_real_process_command_line_does_not_contain_proxy_credentials(self):
        url = 'http://audit-user:private-pass@localhost:8080'
        args_seen = []
        adapter = self.adapter(url)
        adapter.on_start = lambda proc: args_seen.extend(psutil.Process(proc.pid).cmdline())
        adapter._run([sys.executable, '-c', 'import sys; sys.stdin.read()'], lambda line: True)
        self.assertNotIn(url, ' '.join(args_seen))
        self.assertNotIn('private-pass', ' '.join(args_seen))

    def test_stalled_config_reader_is_cancellable_without_leaking_writer_thread(self):
        adapter = self.adapter('http://audit-user:' + 'x' * 30000 + '@localhost:8080')
        cancel, started = threading.Event(), threading.Event()
        failures = []
        processes = []
        def check():
            if cancel.is_set():
                raise DownloadCancelled()
        def on_start(proc):
            processes.append(proc)
            started.set()
        adapter.check, adapter.on_start = check, on_start
        def run():
            try:
                adapter._run([sys.executable, '-c', 'import time; time.sleep(60)'], lambda line: True)
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            time.sleep(.1)
            cancel.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(failures[0], DownloadCancelled)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdin.closed)
            self.assertFalse(any(t.name == 'yt-dlp-config' for t in threading.enumerate()))
        finally:
            cancel.set()
            thread.join(5)

    def test_failed_start_does_not_retry_with_plaintext_cli(self):
        adapter = self.adapter('http://audit-user:private-pass@localhost:8080')
        with patch('ytdlp_process.spawn_owned_process', side_effect=OSError('start failed')) as spawn:
            with self.assertRaises(OSError):
                adapter._run([*adapter._arguments(), '--', 'https://example.invalid/'], lambda line: True)
        spawn.assert_called_once()
        self.assertNotIn('private-pass', ' '.join(spawn.call_args.args[0]))

    def test_child_failure_closes_pipe_and_retains_original_exit_code(self):
        adapter = self.adapter('http://audit-user:' + 'x' * 30000 + '@localhost:8080')
        processes = []
        adapter.on_start = processes.append
        with self.assertRaises(YtDlpProcessError) as caught:
            adapter._run([sys.executable, '-c', 'import sys; sys.exit(7)'], lambda line: True)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertTrue(processes[0].stdin.closed)

    def test_config_delivery_failure_is_reported_even_when_child_exits_successfully(self):
        adapter = self.adapter('http://audit-user:' + 'x' * 30000 + '@localhost:8080')
        with self.assertRaises(YtDlpProcessError) as caught:
            adapter._run([sys.executable, '-c', 'pass'], lambda line: True)
        self.assertIn('could not be delivered', str(caught.exception))
        self.assertNotIn('audit-user', str(caught.exception))

    def test_normal_proxy_modes_leave_stdin_disconnected(self):
        for options in ({}, {'proxy_mode': 'direct'},
                        {'proxy_mode': 'custom', 'proxy_url': 'http://localhost:8080'}):
            with self.subTest(options=options):
                processes = []
                adapter = YtDlpProcess(options, on_start=processes.append)
                adapter._run([sys.executable, '-c', 'pass'], lambda line: True)
                self.assertIsNone(processes[0].stdin)
