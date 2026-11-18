import io
import itertools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import download_engine
import task_manager
from task_manager import TaskManager, TaskOptions
import utils


class ProgressTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.tid = self.manager.add_urls(['https://example.com/video'], TaskOptions(folder=temp.name))[0]
        self.engine = download_engine.DownloadEngine(self.manager, ffmpeg='ffmpeg', ffprobe='ffprobe')
        self.manager.engine = self.engine
        log_patch = patch.object(utils, 'ERROR_LOG_FILE', str(self.folder / 'errors.log'))
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def progress(self):
        return self.manager.get_task(self.tid)['progress']

    def updates(self):
        result = []
        while not self.manager.events.empty():
            event = self.manager.events.get_nowait()
            if event.kind == 'task_updated':
                result.append(event.data['task'])
        return result

    def test_stage_weights_reserve_completion_for_published_output(self):
        self.manager.report_progress(self.tid, 'video', 100, 100)
        self.assertEqual(self.progress(), 70)
        for stage, middle, end in [('processing', 80, 90), ('trimming', 92.5, 95),
                                   ('verification', 96, 97), ('publishing', 98, 99)]:
            self.manager.report_stage_progress(self.tid, stage, 0.5)
            self.assertEqual(self.progress(), middle)
            self.manager.report_stage_progress(self.tid, stage, 1)
            self.assertEqual(self.progress(), end)
        self.manager.update_task(self.tid, progress=100)
        self.assertEqual(self.progress(), 99)
        output = self.folder / 'complete.mp4'
        output.write_bytes(b'complete')
        self.manager.update_task(self.tid, progress=100, status='Completed', output_path=str(output))
        self.assertEqual(self.progress(), 100)

    def test_parallel_stream_totals_are_registered_before_first_finishes(self):
        self.manager.register_download_streams(self.tid, {'video': 100, 'audio': 100})
        with patch.object(task_manager.time, 'monotonic', side_effect=[1, 1.2, 1.4]):
            self.manager.report_progress(self.tid, 'video', 100, 100)
            self.assertEqual(self.progress(), 35)
            self.manager.report_progress(self.tid, 'audio', 50, 100)
            self.assertEqual(self.progress(), 52.5)
            self.manager.report_progress(self.tid, 'audio', 100, 100)
        self.assertEqual(self.progress(), 70)

    def test_estimate_changes_and_repeated_processing_never_move_backward(self):
        self.manager.report_progress(self.tid, 'video', 90, 100)
        self.manager.report_progress(self.tid, 'video', 100, 300)
        self.assertEqual(self.progress(), 63)
        self.manager.report_stage_progress(self.tid, 'processing', 0.8, status='Processing')
        self.manager.report_stage_progress(self.tid, 'processing', 0, status='Retrying encode')
        self.assertEqual(self.progress(), 86)
        values = [task['progress'] for task in self.updates()]
        self.assertEqual(values, sorted(values))

    def test_delayed_callbacks_cannot_replace_newer_stage_or_completed_status(self):
        self.manager.report_stage_progress(self.tid, 'trimming', 0.5, status='Trimming')
        self.manager.report_progress(self.tid, 'late-stream', 50, 100)
        self.manager.report_stage_progress(self.tid, 'processing', 1, status='Late merge')
        self.assertEqual(self.manager.get_task(self.tid)['status'], 'Trimming')
        self.assertEqual(self.progress(), 92.5)
        self.manager.update_task(self.tid, progress=100, status='Completed', output_path='published.mp4')
        self.manager.report_progress(self.tid, 'late-stream', 100, 100)
        self.assertEqual(self.manager.get_task(self.tid)['status'], 'Completed')
        self.assertEqual(self.progress(), 100)

    def test_unknown_download_size_does_not_imply_completion(self):
        self.manager.report_progress(self.tid, 'video', 100000, 0)
        self.assertEqual(self.progress(), 0)
        self.manager.report_progress(self.tid, 'video', 100000, 100000)
        self.assertEqual(self.progress(), 70)

    def test_parallel_callbacks_emit_monotonic_task_snapshots(self):
        self.manager.register_download_streams(self.tid, {'video': 100, 'audio': 100})
        with patch.object(self.manager, 'emit', wraps=self.manager.emit) as emitted, \
                ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.manager.report_progress, self.tid, key, amount, 100)
                       for amount in range(0, 101, 5) for key in ('video', 'audio')]
            for future in futures:
                future.result(timeout=2)
        values = [call.kwargs['task']['progress'] for call in emitted.call_args_list
                  if call.args[0] == 'task_updated']
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(self.progress(), 70)

    def test_parallel_callbacks_cannot_reverse_speed_samples(self):
        unit = 1024 * 1024
        self.manager.register_download_streams(self.tid, {'video': 8 * unit, 'audio': 8 * unit})
        entered, release = threading.Event(), threading.Event()
        ticks = itertools.count(20)

        def clock():
            if threading.current_thread().name == 'older-progress':
                entered.set()
                release.wait(3)
            return next(ticks)

        older = threading.Thread(target=lambda: self.manager.report_progress(
            self.tid, 'video', unit, 8 * unit), name='older-progress')
        newer = threading.Thread(target=lambda: self.manager.report_progress(
            self.tid, 'audio', unit, 8 * unit))
        # Both clock names reproduce the old wall-clock implementation too.
        with patch.object(task_manager, 'time', SimpleNamespace(monotonic=clock, time=clock)):
            try:
                older.start()
                self.assertTrue(entered.wait(2))
                newer.start()
                newer.join(0.15)
            finally:
                release.set()
                older.join(3)
                if newer.ident is not None:
                    newer.join(3)
        self.assertFalse(older.is_alive())
        self.assertFalse(newer.is_alive())
        self.assertEqual(list(self.manager.download_speed_history[self.tid]), [(20, unit), (21, 2 * unit)])
        status = self.manager.get_task(self.tid)['status']
        self.assertIn('2.0MB/16.0MB', status)
        self.assertIn('8.39 Mbps', status)
        self.assertIn('Time left: 14s', status)

    def test_delayed_publication_cannot_overwrite_newer_stream_totals(self):
        unit = 1024 * 1024
        self.manager.register_download_streams(self.tid, {'video': 8 * unit, 'audio': 8 * unit})
        entered, release = threading.Event(), threading.Event()
        report = self.manager.report_stage_progress

        def delayed_report(*args, **kwargs):
            if threading.current_thread().name == 'older-progress':
                entered.set()
                release.wait(3)
            report(*args, **kwargs)

        older = threading.Thread(target=lambda: self.manager.report_progress(
            self.tid, 'video', unit, 8 * unit), name='older-progress')
        newer = threading.Thread(target=lambda: self.manager.report_progress(
            self.tid, 'audio', unit, 8 * unit))
        with patch.object(self.manager, 'report_stage_progress', side_effect=delayed_report):
            try:
                older.start()
                self.assertTrue(entered.wait(2))
                newer.start()
                newer.join(0.15)
            finally:
                release.set()
                older.join(3)
                if newer.ident is not None:
                    newer.join(3)
        self.assertFalse(older.is_alive())
        self.assertFalse(newer.is_alive())
        self.assertIn('2.0MB/16.0MB', self.manager.get_task(self.tid)['status'])

    def test_stream_restart_resets_speed_samples_and_eta_recovers(self):
        unit = 1024 * 1024
        # Replace only this module's clock: patching time.time itself also
        # changes the wall clock used by logging elsewhere in the process.
        clock = SimpleNamespace(
            monotonic=Mock(side_effect=[10, 11, 12, 13]),
            time=Mock(side_effect=AssertionError('wall clock used')),
        )
        with patch.object(task_manager, 'time', clock):
            self.manager.report_progress(self.tid, 'video', 2 * unit, 10 * unit)
            self.manager.report_progress(self.tid, 'video', 3 * unit, 10 * unit)
            self.manager.report_progress(self.tid, 'video', unit, 10 * unit)
            self.assertEqual(list(self.manager.download_speed_history[self.tid]), [(12, unit)])
            self.assertIn('0.00 Mbps - Time left: --', self.manager.get_task(self.tid)['status'])
            self.manager.report_progress(self.tid, 'video', 2 * unit, 10 * unit)
        clock.time.assert_not_called()
        self.assertEqual(list(self.manager.download_speed_history[self.tid]), [(12, unit), (13, 2 * unit)])
        self.assertIn('8.39 Mbps - Time left: 8s', self.manager.get_task(self.tid)['status'])

    def test_unknown_total_does_not_produce_negative_eta(self):
        with patch.object(task_manager.time, 'monotonic', side_effect=[10, 11]):
            self.manager.report_progress(self.tid, 'video', 100, 0)
            self.manager.report_progress(self.tid, 'video', 200, 0)
        self.assertIn('Time left: --', self.manager.get_task(self.tid)['status'])

    def test_ffmpeg_time_uses_current_stage_and_stays_below_total_completion(self):
        for stage, expected in [('processing', 80), ('trimming', 92.5), ('verification', 96)]:
            proc = Mock()
            proc.stdout = io.StringIO('frame=1 time=00:00:05.00 speed=1x\n')
            proc.poll.return_value = 0
            self.engine._pump_ffmpeg_progress(proc, self.tid, 10, 'Working', progress_stage=stage)
            self.assertEqual(self.progress(), expected)
        self.assertTrue(all(task['progress'] < 100 for task in self.updates()))

    def test_mp3_uses_task_duration_and_probes_only_when_missing(self):
        for duration in (123, None, 'invalid'):
            with self.subTest(duration=duration):
                self.manager.update_metadata(self.tid, duration=duration)
                with patch.object(self.engine, '_run_ffmpeg') as runner, \
                        patch.object(self.engine, '_ffprobe_duration', return_value=45) as probe:
                    self.engine._ffmpeg_convert_to_mp3(self.tid, 1, 'input', 'output.mp3')
                self.assertEqual(runner.call_args.kwargs['duration'], 123 if duration == 123 else 45)
                self.assertEqual(probe.call_count, 0 if duration == 123 else 1)

    def test_selector_merge_and_mp3_use_weights_and_extracted_duration(self):
        def ydl(opts, runner, *, request_check=None):
            instance = Mock()
            instance.__enter__ = Mock(return_value=instance)
            instance.__exit__ = Mock(return_value=False)

            def extract(url, download):
                hook = opts['progress_hooks'][0]
                hook({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 100})
                hook({'status': 'downloading', 'downloaded_bytes': 100, 'total_bytes': 100})
                opts['postprocessor_hooks'][0]({'status': 'started'})
                source = opts['outtmpl'].replace('%(ext)s', 'm4a')
                Path(source).write_bytes(b'audio')
                return {'duration': 123, 'requested_downloads': [{'filepath': source}]}

            instance.extract_info.side_effect = extract
            return instance

        def convert(tid, worker, cmd, **kwargs):
            self.manager.report_stage_progress(tid, 'processing', 0.5)
            Path(cmd[-1]).write_bytes(b'mp3')
            self.manager.report_stage_progress(tid, 'processing', 1)

        with patch.object(download_engine, 'ManagedYoutubeDL', side_effect=ydl), \
                patch.object(self.engine, '_run_ffmpeg', side_effect=convert) as runner:
            self.engine._complete_via_selector(self.tid, 1, 'url', 'Example', 'Example',
                                               'MP3', None, False, '', '', folder=str(self.folder))
        self.assertEqual(runner.call_args.kwargs['duration'], 123)
        values = [task['progress'] for task in self.updates()]
        self.assertEqual(values, sorted(values))
        self.assertNotIn(95, values)
        self.assertEqual(values[-1], 100)
        self.assertTrue(all(value < 100 for value in values[:-1]))

    def test_trim_passes_output_duration_and_its_own_stage(self):
        source = self.folder / 'source.mp4'
        source.write_bytes(b'original')
        self.manager.update_metadata(self.tid, duration=10)

        def run(tid, worker, cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b'trimmed')

        for start, end, expected in [('2', '7', 5), ('2', '', 8)]:
            with self.subTest(start=start, end=end), patch.object(self.engine, '_run_ffmpeg', side_effect=run) as runner:
                self.engine._maybe_trim(self.tid, 1, str(source), start, end)
                self.assertEqual(runner.call_args.kwargs['duration'], expected)
                self.assertEqual(runner.call_args.kwargs['progress_stage'], 'trimming')

    def test_publication_keeps_total_below_100_until_caller_marks_success(self):
        source_dir = self.folder / 'private'
        source_dir.mkdir()
        source = source_dir / 'output.mp4'
        source.write_bytes(b'x' * (2 * 1024 * 1024))
        with patch.object(download_engine.os, 'link', side_effect=OSError('copy required')):
            destination = self.engine._publish_output(self.tid, str(source), str(self.folder))
        self.assertEqual(Path(destination).read_bytes(), source.read_bytes())
        self.assertEqual(self.progress(), 99)
        values = [task['progress'] for task in self.updates()]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(value < 100 for value in values))
        self.manager.update_task(self.tid, status='Completed', progress=100, output_path=destination)
        self.assertEqual(self.progress(), 100)

    def test_copy_failure_never_reaches_100_and_removes_partial_destination(self):
        private = self.folder / 'private'
        private.mkdir()
        source = private / 'output.mp4'
        source.write_bytes(b'source')
        actual_open = open

        def fail_read(path, mode='r', *args, **kwargs):
            if mode == 'rb':
                raise OSError('source read failure')
            return actual_open(path, mode, *args, **kwargs)

        with patch.object(download_engine.os, 'link', side_effect=OSError('copy required')), \
                patch('builtins.open', side_effect=fail_read):
            with self.assertRaises(OSError):
                self.engine._publish_output(self.tid, str(source), str(self.folder))
        self.assertFalse((self.folder / 'output.mp4').exists())
        self.assertLess(self.progress(), 100)

    def test_cancel_preserves_progress_and_retry_resets_stages(self):
        self.manager.report_stage_progress(self.tid, 'publishing', 0.5)
        self.manager.update_task(self.tid, status='Cancelled', progress=0)
        self.assertEqual(self.progress(), 98)
        self.manager.update_task(self.tid, status='Error: test')
        self.manager.active_urls.clear()
        self.manager.retry_failed()
        self.assertEqual(self.progress(), 0)
        self.manager.report_progress(self.tid, 'video', 50, 100)
        self.assertEqual(self.progress(), 35)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe required')
    def test_real_mp3_conversion_trim_and_copy_publication_are_monotonic(self):
        self.engine.ffmpeg = shutil.which('ffmpeg')
        self.engine.ffprobe = shutil.which('ffprobe')
        source = self.folder / 'source.wav'
        with wave.open(str(source), 'wb') as audio:
            audio.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 44100)
        output = self.folder / 'output'
        output.mkdir()
        self.manager.tasks[self.tid].update(folder=str(output), format='MP3', trim_end='0.5')
        info = download_engine.YTInfo('url', {'title': 'Example', 'duration': 1, 'formats': [
            {'format_id': 'a', 'ext': 'wav', 'vcodec': 'none', 'acodec': 'pcm_s16le'},
        ]})

        def download(tid, key, url, fmt, filename, *, folder):
            path = Path(folder, filename)
            shutil.copyfile(source, path)
            self.manager.report_progress(tid, key, 1, 2)
            self.manager.report_progress(tid, key, 2, 2)
            return str(path)

        with patch.object(download_engine, 'fetch_ytinfo_with_retry', return_value=info), \
                patch.object(self.engine, '_download_format', side_effect=download), \
                patch.object(download_engine.os, 'link', side_effect=OSError('copy required')), \
                patch.object(self.manager, 'emit', wraps=self.manager.emit) as emitted:
            self.manager._process_task((self.tid, 'url'), 1)
        task = self.manager.get_task(self.tid)
        self.assertTrue(task['status'].startswith('Completed'), task['status'])
        self.assertTrue(Path(task['output_path']).is_file())
        # Pending UI events intentionally retain only the latest state. Observe
        # emitted snapshots here to verify every real conversion/trim stage.
        updates = [call.kwargs['task'] for call in emitted.call_args_list
                   if call.args[0] == 'task_updated']
        values = [task['progress'] for task in updates]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 100)
        self.assertTrue(all(value < 100 for value in values[:-1]))
        self.assertTrue(any(70 < value <= 90 for value in values))
        self.assertTrue(any(90 <= value <= 95 for value in values))


if __name__ == '__main__':
    unittest.main()
