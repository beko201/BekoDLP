from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import utils
import yt_dlp
from task_manager import TaskManager, TaskOptions
from download_engine import DownloadEngine
from errors import MediaOperationError

import test_task_cleanup as cleanup_fixture


downloader = cleanup_fixture.downloader


class ErrorReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.log = self.folder / 'errors.log'
        self.fallback = self.folder / 'fallback.log'
        for name, value in [('ERROR_LOG_FILE', str(self.log)),
                            ('DEBUG_LOG_FILE', str(self.folder / 'debug.log')),
                            ('FALLBACK_DEBUG_LOG_FILE', str(self.folder / 'debug-fallback.log')),
                            ('FALLBACK_ERROR_LOG_FILE', str(self.fallback))]:
            patcher = patch.object(downloader, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        utils._WARNING_STATES.clear()
        self.addCleanup(utils._WARNING_STATES.clear)
        self.app = cleanup_fixture.TaskCleanupTests.make_app(self, self.folder)
        self.app.current_process_pids = {}
        self.app.paused = False

    def text(self):
        return self.log.read_text(encoding='utf-8') if self.log.exists() else ''

    def debug_text(self):
        path = self.folder / 'debug.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def test_configured_repeat_interval_controls_summary_timing(self):
        with patch.object(utils, '_WARNING_REPEAT_SECONDS', 60), \
                patch.object(utils, '_LOG_MAX_BYTES', 5 * 1024 * 1024), \
                patch.object(utils, '_LOG_BACKUP_COUNT', 3), \
                patch.object(utils.time, 'monotonic', return_value=100) as timer:
            utils.configure_logging(5 * 1024 * 1024, 3, repeat_seconds=15)
            utils.log_warning('Configured interval', 'sample', once_key='interval')
            timer.return_value = 114
            utils.log_warning('Configured interval', 'sample', once_key='interval')
            self.assertEqual(self.text().count('[WARNING]'), 1)
            timer.return_value = 115
            utils.log_warning('Configured interval', 'sample', once_key='interval')
            self.assertIn('repeated 2 times', self.text())
            self.assertIn('15.0s', self.text())
            for invalid in (True, 0, 3601, '60'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    utils.configure_logging(65536, 1, repeat_seconds=invalid)
                self.assertEqual(utils._WARNING_REPEAT_SECONDS, 15)
                self.assertEqual(utils._LOG_MAX_BYTES, 5 * 1024 * 1024)

    def test_keyed_warning_summaries_count_repeats_at_interval_boundaries(self):
        with patch.object(utils.time, 'monotonic', return_value=100) as timer:
            utils.log_warning('Recurring warning', 'sample', once_key='recurring')
            for instant in (110, 120, 159):
                timer.return_value = instant
                utils.log_warning('Recurring warning', 'sample', once_key='recurring')
            self.assertEqual(self.text().count('[WARNING]'), 1)
            timer.return_value = 160
            utils.log_warning('Recurring warning', 'sample', once_key='recurring')
            self.assertIn('repeated 4 times', self.text())
            timer.return_value = 161
            utils.log_warning('Recurring warning', 'sample', once_key='recurring')
            timer.return_value = 220
            utils.log_warning('Recurring warning', 'sample', once_key='recurring')
        self.assertEqual(self.text().count('[WARNING]'), 3)
        self.assertIn('repeated 2 times', self.text())
        self.assertIn('60.0s', self.text())

    def test_final_summary_keeps_original_context_and_masks_secrets(self):
        with patch.object(utils.time, 'monotonic', return_value=100):
            for tid, attempt in (('T1', 1), ('T2', 2)):
                with utils.log_context(task=tid, attempt=attempt, stage='download'):
                    for _ in range(3):
                        utils.log_warning('Secret warning', 'https://example.com/?token=private-token',
                                          once_key='same', event='extractor_warning', component='yt-dlp')
            for _ in range(2):
                utils.log_warning('Global warning', 'sample', once_key='global')
            utils.flush_warning_summaries(task='T1', attempt=1)
            self.assertEqual(self.text().count('repeated 2 times'), 1)
            with utils.log_context(task='UNRELATED', attempt=99, stage='trim'):
                utils.flush_warning_summaries()
            utils.flush_warning_summaries()  # Completed summaries must not be emitted twice.
        summaries = [line for line in self.text().splitlines() if '(repeated' in line]
        self.assertEqual(len(summaries), 3)
        self.assertIn('[task=T1] [attempt=1] [stage=download]', summaries[0])
        self.assertIn('[task=T2] [attempt=2] [stage=download]', summaries[1])
        self.assertNotIn('[task=', summaries[2])
        self.assertNotIn('UNRELATED', self.text())
        self.assertNotIn('private-token', self.text())
        self.assertIn('[REDACTED]', self.text())

    def test_concurrent_repeats_are_counted_exactly(self):
        with patch.object(utils.time, 'monotonic', return_value=100):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: utils.log_warning('Concurrent warning', 'sample', once_key='same'),
                              range(80)))
            utils.flush_warning_summaries()
        self.assertEqual(self.text().count('[WARNING]'), 2)
        self.assertIn('repeated 79 times', self.text())

    def test_cache_eviction_flushes_oldest_summary_without_resetting_other_keys(self):
        with patch.object(utils, '_WARNING_CACHE_LIMIT', 2), \
                patch.object(utils.time, 'monotonic', return_value=100):
            for key in ('first', 'second'):
                for _ in range(2):
                    utils.log_warning(key, 'sample', once_key=key)
            utils.log_warning('third', 'sample', once_key='third')
            self.assertIn('WARNING first (repeated 1 times)', self.text())
            utils.log_warning('second', 'sample', once_key='second')
            utils.flush_warning_summaries()
        self.assertIn('WARNING second (repeated 2 times)', self.text())
        self.assertEqual(self.text().count('[WARNING]'), 5)

    def test_failed_first_write_does_not_suppress_next_warning(self):
        with patch.object(utils, 'log_error', side_effect=[False, True]) as write:
            utils.log_warning('Warning', 'sample', once_key='key')
            utils.log_warning('Warning', 'sample', once_key='key')
        self.assertEqual(write.call_count, 2)

    def test_failed_summary_write_preserves_pending_count_for_retry(self):
        with patch.object(utils.time, 'monotonic', return_value=100):
            for _ in range(3):
                utils.log_warning('Warning', 'sample', once_key='key')
            with patch.object(utils, 'log_error', return_value=False):
                utils.flush_warning_summaries()
            utils.flush_warning_summaries()
        self.assertIn('repeated 2 times', self.text())
        self.assertEqual(self.text().count('[WARNING]'), 2)

    def test_task_exit_flushes_pending_repeats_even_when_execution_raises(self):
        manager = self.make_logging_manager()
        url = 'https://example.com/flush'
        tid = manager.add_urls([url], TaskOptions(folder=str(self.folder)))[0]

        def execute(*_):
            for _ in range(2):
                utils.log_warning('Task warning', 'sample', once_key='key')
            raise RuntimeError('unexpected task failure')

        with patch.object(manager.engine, '_process_task_attempt', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, 'unexpected task failure'):
                manager.engine.process_task((tid, url), 1)
        summary = next(line for line in self.text().splitlines() if '(repeated' in line)
        self.assertIn('[task=T1] [attempt=1] [stage=resolve]', summary)
        self.assertFalse(utils._WARNING_STATES)

    def test_unexpected_runtime_and_value_errors_keep_tracebacks(self):
        self.assertIsNone(utils.unexpected_traceback())
        for error_type in (RuntimeError, ValueError):
            with self.subTest(error_type=error_type):
                try:
                    raise error_type('unexpected internal failure')
                except error_type:
                    details = utils.unexpected_traceback()
                self.assertIn('Traceback (most recent call last)', details)
                self.assertIn('test_unexpected_runtime_and_value_errors_keep_tracebacks', details)
                self.assertIn(f'{error_type.__name__}: unexpected internal failure', details)

    def test_only_explicit_operation_and_ytdlp_errors_omit_tracebacks(self):
        for error_type in (MediaOperationError, utils.InsufficientDiskSpace, yt_dlp.utils.DownloadError):
            with self.subTest(error_type=error_type):
                self.assertTrue(issubclass(error_type, RuntimeError) or error_type is yt_dlp.utils.DownloadError)
                try:
                    raise error_type('expected operation failure')
                except error_type:
                    self.assertIsNone(utils.unexpected_traceback())
        utils.log_warning('External warning', 'plain warning text')
        self.assertNotIn('traceback:', self.text())

    def test_warning_uses_supplied_exception_traceback_outside_its_handler(self):
        def fail_originally():
            raise RuntimeError('original diagnostic')

        try:
            fail_originally()
        except RuntimeError as error:
            saved_error = error
        try:
            raise ValueError('unrelated handler')
        except ValueError:
            utils.log_warning('Deferred failure', saved_error)
        text = self.text()
        self.assertIn('fail_originally', text)
        self.assertIn('RuntimeError: original diagnostic', text)
        self.assertNotIn('ValueError: unrelated handler', text)

    def test_task_failure_preserves_unexpected_runtime_traceback_and_context(self):
        for error_type, has_traceback in ((RuntimeError, True), (MediaOperationError, False)):
            with self.subTest(error_type=error_type):
                self.log.unlink(missing_ok=True)
                manager = self.make_logging_manager()
                url = 'https://example.com/failure'
                tid = manager.add_urls([url], TaskOptions(folder=str(self.folder)))[0]
                with patch('download_engine.fetch_ytinfo_with_retry',
                           side_effect=error_type('metadata operation failed')):
                    manager._process_task((tid, url), 1)
                text = self.text()
                self.assertEqual('traceback:' in text, has_traceback)
                self.assertIn('[event=task_failed]', text)
                self.assertIn(f'[task={tid}] [attempt=1] [stage=resolve]', text)
                self.assertIn('metadata operation failed', text)
                if has_traceback:
                    self.assertIn('_process_task_attempt', text)
                    self.assertIn('RuntimeError: metadata operation failed', text)

    def make_logging_manager(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        manager.engine = DownloadEngine(manager, ffmpeg='ffmpeg', ffprobe='ffprobe')
        return manager

    @patch.object(utils, '_LOG_DEBUG', True)
    def test_retry_keeps_task_identity_and_increments_attempt_without_context_leak(self):
        manager = self.make_logging_manager()
        url = 'https://example.com/retry'
        tid = manager.add_urls([url], TaskOptions(folder=str(self.folder)))[0]

        def execute(task, worker):
            manager.set_log_stage(tid, 'download')
            utils.log_warning('retry warning', RuntimeError('sample'), once_key='same-warning')
            manager.fail_task(tid, 'sample failure')

        with patch.object(manager.engine, '_process_task_attempt', side_effect=execute):
            manager._process_task((tid, url), 1)
            manager.retry_failed([tid])
            self.assertEqual(manager.get_task(tid)['attempt'], 1)  # Queuing is not execution.
            manager._process_task((tid, url), 2)
        utils.log_error('outside task')
        headers = [line for line in (self.debug_text() + self.text()).splitlines() if line.startswith('[')]
        for attempt in (1, 2):
            records = [line for line in headers if f'[attempt={attempt}]' in line]
            self.assertTrue(records)
            self.assertTrue(all(f'[task={tid}]' in line for line in records))
            self.assertTrue(any('[stage=resolve]' in line for line in records))
            self.assertTrue(any('[stage=download]' in line and 'retry warning' in line for line in records))
            self.assertTrue(any('[event=task_failed]' in line and '[stage=download]' in line for line in records))
        self.assertNotIn('[task=', headers[-1])
        self.assertNotIn('[attempt=', headers[-1])

    def test_parallel_stream_threads_inherit_only_their_own_task_context(self):
        manager = self.make_logging_manager()
        tids = manager.add_urls(['https://example.com/one', 'https://example.com/two'],
                                TaskOptions(folder=str(self.folder)))
        barrier = threading.Barrier(4)

        def download(tid, key, *args, **kwargs):
            barrier.wait(timeout=5)
            utils.log_warning(f'stream-{tid}-{key}', RuntimeError('stream diagnostic'))
            return str(self.folder / f'{tid}-{key}')

        def execute(tid):
            attempt = manager.begin_task_attempt(tid)
            manager.set_log_stage(tid, 'download')
            with utils.log_context(lambda: manager.task_log_fields(tid), attempt=attempt):
                manager.engine._download_streams_parallel(tid, 'url', Mock(), Mock(),
                                                          'video', 'audio', folder=str(self.folder))

        with patch.object(manager.engine, '_download_format', side_effect=download):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(execute, tids))
        headers = [line for line in self.text().splitlines() if 'WARNING stream-' in line]
        self.assertEqual(len(headers), 4)
        for tid in tids:
            for key in ('video', 'audio'):
                header = next(line for line in headers if f'stream-{tid}-{key}' in line)
                self.assertIn(f'[task={tid}]', header)
                self.assertIn('[attempt=1]', header)
                self.assertIn('[stage=download]', header)

    @patch.object(utils, '_LOG_DEBUG', True)
    def test_stage_transitions_ignore_delayed_progress_and_log_once(self):
        manager = self.make_logging_manager()
        tid = manager.add_urls(['https://example.com/stages'], TaskOptions(folder=str(self.folder)))[0]
        manager.begin_task_attempt(tid)
        for stage in ('downloading', 'processing', 'trimming', 'verification', 'publishing'):
            manager.report_stage_progress(tid, stage)
            manager.report_stage_progress(tid, stage, 0.5)
        manager.report_stage_progress(tid, 'downloading', 1)
        self.assertEqual(manager.task_log_fields(tid)['stage'], 'publish')
        self.assertEqual(self.debug_text().count('[event=task_stage_changed]'), 5)
        self.assertNotIn('[event=task_stage_changed]', self.text())

    def test_cleanup_scope_restores_failure_stage_even_after_exception(self):
        with utils.log_context(task='T1', attempt=3, stage='convert'):
            with self.assertRaises(RuntimeError):
                with utils.log_context(stage='cleanup'):
                    utils.log_error('cleanup failure')
                    raise RuntimeError('cleanup failed')
            utils.log_error('conversion failure')
        headers = [line for line in self.text().splitlines() if line.startswith('[')]
        self.assertIn('[stage=cleanup]', headers[0])
        self.assertIn('[stage=convert]', headers[1])
        self.assertTrue(all('[task=T1]' in line and '[attempt=3]' in line for line in headers))

    @patch.object(utils, '_LOG_DEBUG', True)
    def test_severity_is_explicit_and_independent_of_message(self):
        downloader.log_error('WARNING is only message text')
        for level in ('debug', 'info', 'warning', 'error', 'critical'):
            downloader.log_error('plain message', level=level)
        headers = [line for line in self.text().splitlines() if line.startswith('[')]
        self.assertIn('[ERROR]', headers[0])
        for header, level in zip(headers[1:], ('WARNING', 'ERROR', 'CRITICAL')):
            self.assertIn(f'[{level}]', header)
        debug_headers = [line for line in self.debug_text().splitlines() if line.startswith('[')]
        self.assertEqual(len(debug_headers), 2)
        self.assertIn('[DEBUG]', debug_headers[0])
        self.assertIn('[INFO]', debug_headers[1])
        self.assertNotIn('[INFO]', self.text())

    @patch.object(utils, '_LOG_DEBUG', False)
    def test_explicitly_disabled_debug_skips_context_and_file_writes(self):
        with utils.log_context(Mock(side_effect=AssertionError('filtered debug must not read context'))), \
                patch.object(utils, '_write_log', side_effect=AssertionError('filtered debug must not write')):
            self.assertTrue(utils.log_error('Task stage changed', level='DEBUG'))
        self.assertFalse(self.log.exists())
        utils.log_warning('Visible warning', 'sample')
        utils.log_error('Visible error')
        self.assertIn('[WARNING]', self.text())
        self.assertIn('[ERROR]', self.text())

    def test_debug_failure_does_not_change_the_error_log_destination(self):
        utils.log_error('error destination')
        before = utils.get_log_status()
        original = utils._write_log
        def fail_debug_primary(path, block):
            if path == str(self.folder / 'debug.log'):
                raise PermissionError('debug unavailable')
            return original(path, block)
        with patch.object(utils, '_write_log', side_effect=fail_debug_primary), \
                patch.object(sys, 'stderr', io.StringIO()):
            self.assertTrue(utils.log_error('debug fallback', level='INFO'))
        self.assertEqual(utils.get_log_status(), before)
        self.assertIn('debug fallback', (self.folder / 'debug-fallback.log').read_text(encoding='utf-8'))
        self.assertNotIn('debug fallback', self.text())

    def test_invalid_level_is_rejected_without_writing(self):
        for level in ('WARN', '', None, 40):
            with self.subTest(level=level), self.assertRaises(ValueError):
                downloader.log_error('invalid', level=level)
        self.assertFalse(self.log.exists())

    def test_warning_helper_forwards_context_and_explicit_severity(self):
        utils.log_warning('Plain warning', RuntimeError('failure'),
                          event='metadata_failed', component='download_engine')
        text = self.text()
        self.assertIn('[WARNING]', text)
        self.assertIn('[event=metadata_failed]', text)
        self.assertIn('[component=download_engine]', text)
        self.assertIn('failure', text)

    def test_context_is_redacted_and_cannot_inject_header_lines(self):
        downloader.log_error('failure', event='https://example.com/?token=private-event',
                             component='worker\r\n[CRITICAL]')
        text = self.text()
        self.assertNotIn('private-event', text)
        self.assertIn('[event=https://example.com/?token=(REDACTED)]', text)
        self.assertIn('[component=worker  (CRITICAL)]', text)
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_ytdlp_warnings_are_logged_once_and_secrets_are_redacted(self):
        message = 'TikTok request failed: https://example.com/video?token=private-token'
        with patch.object(utils, 'ERROR_LOG_FILE', str(self.log)), \
                patch.object(sys, 'stderr', io.StringIO()) as stderr:
            for _ in range(2):
                with yt_dlp.YoutubeDL({'quiet': True, **utils._network_options(None)}) as ydl:
                    ydl.report_warning(message)
            self.assertEqual(stderr.getvalue(), '')
        text = self.text()
        self.assertEqual(text.count('WARNING yt-dlp'), 1)
        self.assertIn('TikTok request failed', text)
        self.assertIn('[REDACTED]', text)
        self.assertNotIn('private-token', text)

    def test_failed_primary_log_uses_stderr_and_fallback_file(self):
        real_open = open
        stderr = io.StringIO()

        def open_file(path, *args, **kwargs):
            if str(path) == str(self.log):
                raise PermissionError('primary log locked')
            return real_open(path, *args, **kwargs)

        with patch('builtins.open', side_effect=open_file), patch.object(downloader.sys, 'stderr', stderr):
            self.assertTrue(downloader.log_error('sample failure', details=[('task', 'T1')],
                                                level='WARNING', event='sample_failed', component='engine'))
        for text in (stderr.getvalue(), self.fallback.read_text(encoding='utf-8')):
            self.assertIn('primary log locked', text)
            self.assertIn('sample failure', text)
            self.assertIn('T1', text)
            self.assertIn('[WARNING]', text)
            self.assertIn('[event=sample_failed]', text)
            self.assertIn('[component=engine]', text)

    def test_logger_does_not_recurse_when_all_sinks_are_unavailable(self):
        with patch('builtins.open', side_effect=PermissionError('no writes')), \
                patch.object(downloader.sys, 'stderr', None), \
                patch.object(downloader.sys, '__stderr__', None):
            self.assertFalse(downloader.log_error('original failure'))

    def test_parallel_log_records_remain_intact(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: downloader.log_error(f'failure-{i}', details=[('task', f'T{i}')]), range(20)))
        blocks = self.text().strip().split('\n\n')
        self.assertEqual(len(blocks), 20)
        for index in range(20):
            block = next(b for b in blocks if b.splitlines()[0].endswith(f'failure-{index}'))
            self.assertEqual(len(block.splitlines()), 2)
            self.assertTrue(block.splitlines()[1].endswith(f'T{index}'))

    def test_rotation_bounds_archives_and_preserves_latest_record(self):
        for level, path in (('ERROR', self.log), ('INFO', self.folder / 'debug.log')):
            with self.subTest(level=level), patch.object(utils, '_LOG_MAX_BYTES', 1500), \
                    patch.object(utils, '_LOG_BACKUP_COUNT', 2):
                for index in range(40):
                    downloader.log_error(f'failure-{index}', level=level, details=[('program', 'x' * 200)])
                self.assertIn('failure-39', path.read_text(encoding='utf-8'))
                self.assertTrue(Path(str(path) + '.1').exists())
                self.assertTrue(Path(str(path) + '.2').exists())
                self.assertFalse(Path(str(path) + '.3').exists())

    def test_secrets_are_masked_in_message_details_and_traceback(self):
        url = 'https://alice:secretpass@example.com/video?v=123&token=secretvalue&sig=secretsig'
        downloader.log_error(url, details=[('headers', 'Authorization: Bearer secretbearer\nCookie: secretcookie')],
                             tb=url)
        text = self.text()
        for secret in ('secretpass', 'secretvalue', 'secretsig', 'secretbearer', 'secretcookie'):
            self.assertNotIn(secret, text)
        self.assertIn('v=123', text)
        self.assertIn('[REDACTED]', text)
        self.assertIn('session=', text)

    def test_unhandled_thread_and_main_errors_are_logged_and_hooks_chain(self):
        with patch.object(sys, 'excepthook') as original_sys, \
                patch.object(threading, 'excepthook') as original_thread:
            utils.install_exception_hooks()
            try:
                raise ValueError('unhandled failure')
            except ValueError as error:
                sys.excepthook(type(error), error, error.__traceback__)
                threading.excepthook(SimpleNamespace(exc_type=type(error), exc_value=error,
                                                     exc_traceback=error.__traceback__, thread=threading.current_thread()))
            original_sys.assert_called_once()
            original_thread.assert_called_once()
        self.assertEqual(self.text().count('ERROR uncaught exception'), 2)


    def test_probe_fallback_keeps_original_program_output(self):
        error = subprocess.CalledProcessError(1, ['ffprobe'], output='decoder diagnostic')
        with patch.object(downloader, 'run_cmd_capture', side_effect=error), \
                patch.object(self.app.engine, '_ffprobe_media', side_effect=error):
            self.assertEqual(downloader.ComponentHarness._ffprobe_duration(self.app, 'video.mp4', 'ffprobe'), 0)
            self.assertEqual(self.app._ffprobe_vcodec('video.mp4'), '')
        self.assertIn('decoder diagnostic', self.text())
        self.assertIn('video.mp4', self.text())
        self.app._task_error.assert_not_called()


    def test_failed_trim_preserves_original_and_marks_completion_as_warning(self):
        source = self.folder / 'video.mp4'
        source.write_bytes(b'original')
        self.app._run_ffmpeg = Mock(side_effect=RuntimeError('trim failed'))
        self.app._maybe_trim('T1', 1, str(source), '0', '1')
        self.assertEqual(source.read_bytes(), b'original')
        self.app._update_global_progress = Mock()
        downloader.ComponentHarness._task_update(self.app, 'T1', status='Completed (MP4)', output_path=str(source))
        self.assertIn('Warning:', self.app.tasks['T1']['status'])
        self.assertIn('Trimming could not be applied', self.app.tasks['T1']['status'])
        self.assertIn('trim failed', self.text())

    def test_persistent_cleanup_failure_records_filename_and_reason(self):
        source = self.folder / 'partial.mp4'
        source.write_bytes(b'partial')
        with patch.object(downloader.os, 'remove', side_effect=PermissionError('locked by another process')), \
                patch.object(downloader.time, 'sleep'):
            self.assertFalse(downloader.remove_leftovers(str(self.folder), lambda n: n == source.name, retries=2))
        self.assertIn('partial.mp4', self.text())
        self.assertIn('locked by another process', self.text())


    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required')
    def test_ffmpeg_check_false_still_logs_failed_attempt_output(self):
        command = [shutil.which('ffmpeg'), '-invalid-error-reporting-test']
        code = self.app._run_ffmpeg('T1', 1, command, check=False)
        self.assertNotEqual(code, 0)
        self.assertIn('ERROR FFmpeg process', self.text())
        self.assertIn('invalid-error-reporting-test', self.text())
        self.assertIn('T1', self.text())

    def test_normal_cancellation_is_not_logged_as_error(self):
        self.app.cancel_events['T1'].set()
        with patch.object(downloader.subprocess, 'Popen') as popen:
            with self.assertRaises(downloader.DownloadCancelled):
                self.app._run_ffmpeg('T1', 1, ['ffmpeg', 'output.mp4'])
        popen.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_pipe_reader_failure_reaches_caller_and_process_is_closed(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.stdout = Mock()
        process.stdout.__iter__ = Mock(side_effect=OSError('pipe reader failed'))
        process.kill.side_effect = lambda: setattr(process.poll, 'return_value', -9)
        with patch.object(downloader.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'pipe reader failed'):
                self.app._run_ffmpeg('T1', 1, ['ffmpeg', 'output.mp4'])
        process.kill.assert_called_once()
        process.stdout.close.assert_called_once()
        self.assertEqual(self.app.current_process_pids, {})


if __name__ == '__main__':
    unittest.main()
