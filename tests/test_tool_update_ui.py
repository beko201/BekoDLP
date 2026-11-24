"""Manual Qt tool checks, idle installation, cancellation and existing menu flow."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from shiboken6 import isValid
import app_paths
import tool_updates as backend
from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from ui.tool_updates import ToolUpdatesDialog
from ui.i18n import set_language
from test_tool_updates import release_fixture


class ToolUpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.patchers = [patch('app_paths.tool_updates_root', return_value=self.folder / 'tool-updates'),
                         patch('tool_updates.supported', return_value=True)]
        for patcher in self.patchers:
            patcher.start()
        with patch.object(self.manager, 'set_worker_count'):
            self.window = MainWindow(self.manager, settings_path=self.folder / 'settings.json')
        self.dialog = ToolUpdatesDialog(self.window)

    def tearDown(self):
        self.dialog.reject()
        self.wait(lambda: not self.dialog.busy)
        for box in list(self.window._message_boxes):
            box.done(QMessageBox.StandardButton.Ok)
        finish_window_close(self.window)
        self.window.controller.poll()
        self.app.processEvents()
        set_language('en')
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def check_and_download(self, *, before_install=None):
        release, payload, data = release_fixture('deno')
        def fetch(url, *, output=None, progress=None, **kwargs):
            if output is None:
                return json.dumps(data).encode()
            output.write(payload)
            if progress:
                progress(len(payload), len(payload))
            if before_install:
                before_install()
            return hashlib.sha256(payload).hexdigest()
        with patch.object(backend, 'read_url', side_effect=fetch) as network, \
                patch.object(backend, 'installed_version', return_value='2.9.8'), \
                patch.object(backend, 'run_version', return_value=release.version):
            self.dialog.check('deno')
            self.wait(lambda: not self.dialog.busy)
            self.assertTrue(self.dialog.rows['deno'][3].isEnabled())
            self.assertEqual(self.dialog.rows['deno'][1].text(), release.version)
            self.dialog.update_tool('deno')
            self.wait(lambda: not self.dialog.busy)
            self.assertEqual(network.call_count, 2)

    def test_no_startup_network_and_manual_check_install_works(self):
        with patch.object(backend, 'read_url') as network:
            self.app.processEvents()
            network.assert_not_called()
        self.check_and_download()
        self.assertIsNone(self.dialog.stage)
        self.assertEqual(self.dialog.versions['deno'], '2.9.9')
        self.assertIsNotNone(app_paths.managed_tool_path('deno'))
        self.assertFalse(self.dialog.rows['deno'][3].isEnabled())

    def test_task_arriving_during_download_defers_install_without_cancelling_task(self):
        ids = []
        def enqueue():
            ids.extend(self.manager.add_urls(['https://example.com/new'], TaskOptions(folder=str(self.folder))))
        self.check_and_download(before_install=enqueue)
        self.assertIsNone(app_paths.managed_tool_path('deno'))
        self.assertTrue(self.dialog.stage.ready)
        self.assertFalse(self.manager.cancel_events[ids[0]].is_set())
        self.assertIn('verified download is ready', self.dialog.status.text())
        self.assertFalse(self.window._closing)
        self.manager.update_task(ids[0], status='Completed', progress=100)
        self.dialog.update_tool('deno')
        self.assertIsNotNone(app_paths.managed_tool_path('deno'))
        self.assertIsNone(self.dialog.stage)

    def test_cancel_verified_pending_update_removes_only_staging(self):
        tid = self.manager.add_urls(['https://example.com/pending'], TaskOptions(folder=str(self.folder)))[0]
        self.check_and_download()
        folder = self.dialog.stage.folder
        self.dialog.cancel_job()
        self.assertIsNone(self.dialog.stage)
        self.assertFalse(folder.exists())
        self.assertFalse(self.manager.cancel_events[tid].is_set())
        self.assertIsNone(app_paths.managed_tool_path('deno'))

    def test_closing_during_download_cancels_without_install(self):
        started, release = threading.Event(), threading.Event()
        package, _, _ = release_fixture('deno')
        self.dialog.releases['deno'] = package
        def preparing(stage, *args):
            started.set()
            release.wait(3)
            backend.check_cancel(stage.cancel)
        with patch.object(backend.StagedToolUpdate, 'prepare', preparing):
            self.dialog.update_tool('deno')
            self.wait(started.is_set)
            folder = self.dialog.stage.folder
            self.dialog.reject()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(isValid(self.dialog))
            release.set()
            self.wait(lambda: not self.dialog.busy)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.dialog))
        self.assertFalse(folder.exists())
        self.assertIsNone(app_paths.managed_tool_path('deno'))

    def test_repeated_open_close_releases_dialogs(self):
        controller = self.window.tool_updater
        with patch.object(backend, 'installed_version', return_value='test'):
            for _ in range(30):
                controller.open()
                dialog = controller.dialog
                self.wait(lambda: not dialog.busy)
                dialog.close()
                self.assertIsNone(controller.dialog)
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(isValid(dialog))
        # Only the separate dialog created by this test fixture remains.
        self.assertEqual(self.window.findChildren(ToolUpdatesDialog), [self.dialog])

    def test_cleanup_permission_error_does_not_block_idle_dialog_close(self):
        controller = self.window.tool_updater
        with patch.object(backend, 'installed_version', return_value='test'):
            controller.open()
            dialog = controller.dialog
            self.wait(lambda: not dialog.busy)
        stage = Mock(cancel=threading.Event(), folder=self.folder / 'locked')
        stage.discard.side_effect = PermissionError('Staging folder locked')
        dialog.stage = stage
        self.assertTrue(dialog.isVisible())
        with patch('ui.tool_updates.log_warning') as warning:
            dialog.reject()
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(controller.dialog)
        self.assertIsNone(dialog.stage)
        self.assertTrue(stage.cancel.is_set())
        stage.discard.assert_called_once()
        stage.activate.assert_not_called()
        warning.assert_called_once()
        self.assertIsInstance(warning.call_args.args[1], PermissionError)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dialog))

    def test_worker_cleanup_permission_error_preserves_close_and_error_handling(self):
        for close_during_job in (False, True):
            with self.subTest(close_during_job=close_during_job):
                controller = self.window.tool_updater
                with patch.object(backend, 'installed_version', return_value='test'):
                    controller.open()
                    dialog = controller.dialog
                    self.wait(lambda: not dialog.busy)
                stage = Mock(cancel=threading.Event(), folder=self.folder / 'locked',
                             release=release_fixture('deno')[0])
                stage.discard.side_effect = PermissionError('Staging folder locked')
                dialog.stage = stage
                started, release = threading.Event(), threading.Event()
                def fail_download():
                    started.set()
                    release.wait(3)
                    backend.check_cancel(stage.cancel)
                    raise OSError('Download failed')
                with patch('ui.tool_updates.log_warning') as warning:
                    try:
                        dialog._job('download', fail_download)
                        self.wait(started.is_set)
                        if close_during_job:
                            dialog.reject()
                            self.assertFalse(dialog.isVisible())
                            self.assertIsNone(controller.dialog)
                            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                            self.assertTrue(isValid(dialog))
                        release.set()
                        self.wait(lambda: not dialog.busy)
                        self.assertIsNone(dialog.stage)
                        stage.discard.assert_called_once()
                        stage.activate.assert_not_called()
                        cleanup_warnings = [call for call in warning.call_args_list
                                            if call.args[0] == 'Could not remove temporary tool update files']
                        self.assertEqual(len(cleanup_warnings), 1)
                        if not close_during_job:
                            self.assertTrue(dialog.isVisible())
                            self.assertIn('Download failed', dialog.status.text())
                            self.assertFalse(dialog.cancel_button.isEnabled())
                            dialog.reject()
                    finally:
                        release.set()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(isValid(dialog))
                self.assertIsNone(controller.dialog)

    def test_old_worker_completion_preserves_reopened_dialog(self):
        controller = self.window.tool_updater
        started, release = threading.Event(), threading.Event()
        def inspect_version(component):
            started.set()
            release.wait(3)
            return 'test'
        with patch.object(backend, 'installed_version', side_effect=inspect_version):
            controller.open()
            old = controller.dialog
            self.wait(started.is_set)
            old.reject()
            self.assertIsNone(controller.dialog)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(isValid(old))
            controller.open()
            current = controller.dialog
            self.assertIsNot(current, old)
            release.set()
            self.wait(lambda: not old.busy and not current.busy)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(old))
        self.assertIs(controller.dialog, current)
        self.assertTrue(isValid(current))
        current.reject()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(current))

    def test_translation_keeps_component_ids_and_menu_action(self):
        action = self.window.tool_updater.action
        self.assertIn(action, self.window.help_menu.actions())
        set_language('tr')
        self.assertEqual(action.text(), 'İndirme araçlarını güncelle…')
        self.assertEqual(self.dialog.windowTitle(), 'Araç güncellemeleri')
        self.assertEqual(set(self.dialog.rows), {'ytdlp', 'ffmpeg', 'deno'})

    def test_ffmpeg_activation_refreshes_engine_paths_and_encoder_cache(self):
        component, release = 'ffmpeg', release_fixture('ffmpeg')[0]
        self.dialog.releases[component] = release
        stage = Mock(release=release)
        self.dialog.stage = stage
        engine = self.manager.engine
        engine._encoder_lock = threading.Lock()
        engine._encoders = {'old-encoder'}
        with patch('ui.tool_updates.which_executable', side_effect=['new/ffmpeg.exe', 'new/ffprobe.exe']), \
                patch.object(self.window.controller, 'load_encoders') as reload_encoders:
            self.dialog._activate()
        stage.activate.assert_called_once()
        self.assertEqual((engine.ffmpeg, engine.ffprobe), ('new/ffmpeg.exe', 'new/ffprobe.exe'))
        self.assertIsNone(engine._encoders)
        reload_encoders.assert_called_once()
