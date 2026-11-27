"""Diagnostic destination tracking, bounded report snapshots and safe export."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import app_logging
import app_paths


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.primary = self.folder / 'primary' / 'errors.log'
        self.primary.parent.mkdir()
        self.fallback = self.folder / 'fallback' / 'errors.log'
        self.fallback.parent.mkdir()
        for name, value in (('ERROR_LOG_FILE', str(self.primary)),
                            ('FALLBACK_ERROR_LOG_FILE', str(self.fallback)), ('_LOG_STATE', None)):
            patcher = patch.object(app_logging, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fail_primary(self, path, block):
        if path == str(self.primary):
            raise PermissionError('primary unavailable')
        return self.write_log(path, block)

    def test_destination_tracks_fallback_and_returns_to_primary_after_recovery(self):
        self.write_log = app_logging._write_log
        with patch.object(app_logging, '_write_log', side_effect=self.fail_primary), \
                patch.object(app_logging.sys, 'stderr', None), patch.object(app_logging.sys, '__stderr__', None):
            self.assertTrue(app_logging.log_error('fallback diagnostic'))
        self.assertEqual(app_logging.get_log_status()['path'], str(self.fallback))
        self.assertIn(f'Fallback log file: {self.fallback}', app_logging.log_location_text())
        self.assertTrue(app_logging.log_error('primary recovered'))
        self.assertEqual(app_logging.get_log_status()['path'], str(self.primary))
        self.assertFalse(app_logging.get_log_status()['write_failed'])

    def test_no_working_sink_is_reported_without_claiming_the_error_was_saved(self):
        with patch.object(app_logging, '_write_log', side_effect=PermissionError('no writes')), \
                patch.object(app_logging.sys, 'stderr', None), patch.object(app_logging.sys, '__stderr__', None):
            self.assertFalse(app_logging.log_error('unsaved diagnostic'))
        self.assertTrue(app_logging.get_log_status()['write_failed'])
        self.assertIsNone(app_logging.get_log_status()['path'])
        self.assertIn('could not be saved', app_logging.log_location_text())
        self.assertIn('No log could be saved', diagnostics.build_diagnostic_report())

    def test_report_uses_actual_fallback_and_redacts_legacy_log_and_details(self):
        self.primary.write_text('STALE PRIMARY', encoding='utf-8')
        self.write_log = app_logging._write_log
        with patch.object(app_logging, '_write_log', side_effect=self.fail_primary), \
                patch.object(app_logging.sys, 'stderr', None), patch.object(app_logging.sys, '__stderr__', None):
            app_logging.log_error('CURRENT FALLBACK')
        with self.fallback.open('a', encoding='utf-8') as stream:
            stream.write('https://example.com/?token=legacy-secret\nCookie: legacy-cookie\n')
        report = diagnostics.build_diagnostic_report('Authorization: Bearer detail-secret',
                                                     [{'id': 'T1', 'attempt': 2, '_log_stage': 'convert',
                                                       'url': 'https://example.com/?sig=task-secret'}])
        self.assertIn('CURRENT FALLBACK', report)
        self.assertNotIn('STALE PRIMARY', report)
        self.assertIn('attempt: 2', report)
        self.assertIn('_log_stage: convert', report)
        for secret in ('legacy-secret', 'legacy-cookie', 'detail-secret', 'task-secret'):
            self.assertNotIn(secret, report)
        self.assertIn('[REDACTED]', report)

    def test_report_reads_only_recent_log_and_handles_missing_file(self):
        self.primary.write_text('OLD RECORD\n' + 'x' * diagnostics.LOG_TAIL_BYTES + '\nLATEST RECORD',
                                encoding='utf-8')
        report = diagnostics.build_diagnostic_report()
        self.assertNotIn('OLD RECORD', report)
        self.assertIn('LATEST RECORD', report)
        self.assertLess(len(report), diagnostics.LOG_TAIL_BYTES + 4096)
        self.primary.unlink()
        self.assertIn('Log unavailable:', diagnostics.build_diagnostic_report())

    def test_atomic_export_and_failed_replace_preserve_existing_report(self):
        destination = self.folder / 'report.txt'
        diagnostics.save_diagnostic_report(destination, 'First report\n')
        self.assertEqual(destination.read_text(encoding='utf-8'), 'First report\n')
        with patch.object(diagnostics.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                diagnostics.save_diagnostic_report(destination, 'Replacement report\n')
        self.assertEqual(destination.read_text(encoding='utf-8'), 'First report\n')
        self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_export_cannot_overwrite_runtime_logs_or_archives(self):
        self.primary.write_text('Keep original', encoding='utf-8')
        for path in (self.primary, self.fallback, Path(str(self.primary) + '.1'), app_paths.SETTINGS_FILE,
                     app_logging.DEBUG_LOG_FILE, app_logging.FALLBACK_DEBUG_LOG_FILE, app_logging.DEBUG_LOG_FILE + '.1'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                diagnostics.save_diagnostic_report(path, 'Report')
        self.assertEqual(self.primary.read_text(encoding='utf-8'), 'Keep original')

    def test_report_masks_home_paths_in_details_tasks_and_legacy_log(self):
        paths = [r'C:\Users\Alice Smith\Downloads\clip.mp4',
                 'c:/users/BobPrivate/Downloads/clip.mp4',
                 r'C:\\Users\\CarolPrivate\\Downloads\\clip.mp4',
                 '/home/dave-private/video.mp4', '/Users/EvePrivate/Movies/video.mp4',
                 '/root/private/video.mp4']
        raw_log = '\n'.join(paths)
        self.primary.write_text(raw_log, encoding='utf-8')
        report = diagnostics.build_diagnostic_report(paths[0], [{'id': 'T1', 'status': paths[1]}])
        for name in ('Alice Smith', 'BobPrivate', 'CarolPrivate', 'dave-private', 'EvePrivate'):
            self.assertNotIn(name, report)
        self.assertNotIn('/root/', report)
        self.assertIn('<USER_HOME>', report)
        self.assertIn('clip.mp4', report)
        self.assertEqual(self.primary.read_text(encoding='utf-8'), raw_log)
        self.assertEqual(app_logging.get_log_status()['path'], str(self.primary))

    def test_custom_home_path_variants_are_masked_without_changing_web_urls(self):
        with patch.object(diagnostics.Path, 'home', return_value=Path(r'D:\Profiles\Jane Doe')):
            text = diagnostics.mask_personal_paths(
                '"D:\\Profiles\\Jane Doe\\clip.mp4"\n'
                'd:/profiles/Jane Doe/other.mp4\n'
                'https://example.com/home/channel/video\n')
        self.assertNotIn('Jane Doe', text)
        self.assertIn('<USER_HOME>\\clip.mp4', text)
        self.assertIn('<USER_HOME>/other.mp4', text)
        self.assertIn('https://example.com/home/channel/video', text)

    def test_report_masks_known_output_folders_outside_home(self):
        raw_path = r'D:\Private Client\Downloads\clip.mp4'
        self.primary.write_text(raw_path, encoding='utf-8')
        report = diagnostics.build_diagnostic_report(raw_path,
                                                     [{'id': 'T1', 'folder': r'D:\Private Client\Downloads',
                                                       'output_path': r'E:\Secret Results\audio.mp3',
                                                       'status': r'E:\Secret Results\audio.mp3'}])
        self.assertNotIn('Private Client', report)
        self.assertNotIn('Secret Results', report)
        self.assertIn('<OUTPUT_FOLDER>\\clip.mp4', report)
        self.assertIn('<OUTPUT_FOLDER>\\audio.mp3', report)
        self.assertEqual(self.primary.read_text(encoding='utf-8'), raw_path)
