"""Offline Qt diagnostic menu, clipboard, export and error-dialog flows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

import utils
from diagnostics import mask_personal_paths
from task_manager import TaskManager, TaskOptions, PlaylistFetcher
from errors import MediaOperationError
from ui.main_window import MainWindow
from ui import diagnostics as qt_diagnostics


class QtDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.primary = self.folder / 'primary' / 'errors.log'
        self.fallback = self.folder / 'fallback' / 'errors.log'
        self.primary.parent.mkdir()
        self.fallback.parent.mkdir()
        utils._WARNING_STATES.clear()
        self.addCleanup(utils._WARNING_STATES.clear)
        for target, value in (('utils.ERROR_LOG_FILE', str(self.primary)),
                              ('utils.FALLBACK_ERROR_LOG_FILE', str(self.fallback)),
                              ('utils._LOG_STATE', None)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ('ui.task_controller.TaskController.start',
                       'ui.task_controller.TaskController.load_encoders'):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window._closing = True
        for box in list(self.window._message_boxes):
            box.done(QMessageBox.StandardButton.Ok)
        self.window.controller.timer.stop()
        self.window.preferences.close()
        self.window.thumbnails.close()
        self.window._allow_close = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.app.clipboard().clear()

    def force_fallback(self):
        write_log = utils._write_log
        def write(path, block):
            if path == str(self.primary):
                raise PermissionError('locked primary')
            write_log(path, block)
        with patch.object(utils, '_write_log', side_effect=write), \
                patch.object(utils.sys, 'stderr', None), patch.object(utils.sys, '__stderr__', None):
            utils.log_error('Sample fallback failure')

    def test_error_dialog_and_help_copy_details_show_actual_fallback_path(self):
        self.force_fallback()
        self.assertFalse(self.window.copy_error_action.isEnabled())
        self.window._show_message('critical', 'Sample error', 'Operation failed.',
                                  'https://example.com/?token=clipboard-secret')
        box = next(iter(self.window._message_boxes))
        self.assertIn(str(self.fallback), box.informativeText())
        labels = {button.text() for button in box.buttons()}
        self.assertTrue({'Copy error details', 'Open log folder', 'Export diagnostic report…'} <= labels)
        self.window.copy_error_action.trigger()
        text = self.app.clipboard().text()
        self.assertIn('Sample error', text)
        self.assertIn(str(self.fallback), text)
        self.assertNotIn('clipboard-secret', text)
        self.assertIn('[REDACTED]', text)

    def test_open_log_folder_uses_fallback_and_handles_desktop_failure(self):
        self.force_fallback()
        with patch('ui.diagnostics.open_folder') as open_folder:
            self.window.open_log_action.trigger()
        open_folder.assert_called_once_with(str(self.fallback.parent))
        with patch('ui.diagnostics.open_folder', side_effect=OSError('desktop unavailable')):
            self.window.open_log_action.trigger()
        box = next(iter(self.window._message_boxes))
        self.assertEqual(box.windowTitle(), 'Could not open log folder')

    def test_export_menu_saves_report_and_cancel_does_not_write(self):
        self.force_fallback()
        destination = self.folder / 'diagnostics.txt'
        with patch('ui.diagnostics.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
            self.window.export_report_action.trigger()
        report = destination.read_text(encoding='utf-8')
        self.assertIn('Sample fallback failure', report)
        self.assertIn(mask_personal_paths(str(self.fallback)), report)
        with patch('ui.diagnostics.QFileDialog.getSaveFileName', return_value=('', '')), \
                patch('ui.diagnostics.save_diagnostic_report') as save:
            self.window.export_report_action.trigger()
        save.assert_not_called()

    def test_task_diagnostics_copy_button_includes_task_attempt_and_stage(self):
        tid = self.manager.add_urls(['https://example.com/task'], TaskOptions(folder=str(self.folder)))[0]
        self.manager.begin_task_attempt(tid)
        self.manager.set_log_stage(tid, 'convert')
        self.manager.update_task(tid, status='Error: conversion failed')
        self.window.show_task_details(tid)
        box = next(iter(self.window._message_boxes))
        next(button for button in box.buttons() if button.text() == 'Copy error details').click()
        text = self.app.clipboard().text()
        self.assertIn(f'Task: {tid}', text)
        self.assertIn('Attempt: 1', text)
        self.assertIn('Stage: convert', text)

    def test_export_failure_is_displayed_and_preserves_existing_file(self):
        destination = self.folder / 'diagnostics.txt'
        destination.write_text('Existing report', encoding='utf-8')
        with patch('ui.diagnostics.QFileDialog.getSaveFileName', return_value=(str(destination), '')), \
                patch('diagnostics.os.replace', side_effect=PermissionError('locked report')):
            self.window.export_report_action.trigger()
        self.assertEqual(destination.read_text(encoding='utf-8'), 'Existing report')
        box = next(iter(self.window._message_boxes))
        self.assertEqual(box.windowTitle(), 'Could not export diagnostic report')

    def test_random_input_without_download_folder_creates_warning_log(self):
        self.window.folder = ''
        self.window.url_edit.setText('random text instead of a URL')
        self.assertFalse(self.primary.exists())
        self.window.enqueue_button.click()
        log = self.primary.read_text(encoding='utf-8')
        self.assertIn('[WARNING]', log)
        self.assertIn('[event=input_validation_failed]', log)
        self.assertIn('Choose an existing download folder first.', log)
        self.assertIn('random text', self.window.url_edit.text())
        self.assertEqual(next(iter(self.window._message_boxes)).windowTitle(), 'Download folder')

    def test_missing_url_recreates_a_log_deleted_while_application_is_running(self):
        self.window.enqueue_button.click()
        self.primary.unlink()
        self.window.enqueue_button.click()
        log = self.primary.read_text(encoding='utf-8')
        self.assertIn('[event=input_validation_failed]', log)
        self.assertIn('Missing URL', log)
        self.assertIn('repeated 1 times', log)

    def test_random_input_with_folder_records_resolution_failure_offline(self):
        self.window.folder = str(self.folder)
        self.window.url_edit.setText('not-a-real-video-url')

        def resolve(url, options, expand_playlist=False):
            PlaylistFetcher(self.manager, url, options, expand_playlist).run()
            return True

        with patch.object(self.manager, 'submit_link', side_effect=resolve), \
                patch('download_engine._extract_urls_from_input',
                      side_effect=MediaOperationError('Unsupported URL')):
            self.window.enqueue_button.click()
            self.window.controller.poll()
        log = self.primary.read_text(encoding='utf-8')
        self.assertIn('[ERROR]', log)
        self.assertIn('[event=link_resolution_failed]', log)
        self.assertIn('not-a-real-video-url', log)
        self.assertEqual(next(iter(self.window._message_boxes)).windowTitle(), 'Link resolution failed')
