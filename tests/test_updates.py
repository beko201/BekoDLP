"""Real staging checks and isolated GitHub/Qt update flows; no internet required."""
import hashlib
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QIODevice, QTimer
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QApplication, QMessageBox

from app_info import UPDATE_ASSET_NAME, UPDATE_REPOSITORY
from updates import Release, StagedUpdate, can_self_update, launch_update, parse_release, version_key
from ui.i18n import set_language, tr
from ui.updates import UpdateController


def executable_payload():
    data = bytearray(128)
    data[:2] = b'MZ'
    data[60:64] = (64).to_bytes(4, 'little')
    data[64:70] = b'PE\x00\x00\x64\x86'
    return bytes(data)


def release_data(version='v1.1', payload=None):
    payload = executable_payload() if payload is None else payload
    return {'tag_name': version, 'draft': False, 'prerelease': False, 'assets': [{
        'name': UPDATE_ASSET_NAME, 'state': 'uploaded', 'size': len(payload),
        'digest': 'sha256:' + hashlib.sha256(payload).hexdigest(),
        'browser_download_url': f'https://github.com/{UPDATE_REPOSITORY}/releases/download/{version}/{UPDATE_ASSET_NAME}'}]}


class UpdateValidationTests(unittest.TestCase):
    def test_stable_versions_and_no_downgrade(self):
        self.assertEqual(version_key('v1.0'), version_key('1.0.0'))
        self.assertGreater(version_key('1.10'), version_key('1.9'))
        self.assertIsNone(parse_release(release_data('v1.0'), '1.0.0'))
        self.assertIsNone(parse_release(release_data('v0.9'), '1.0'))
        for value in ('v1.1-beta', '../../1.1', '', None, '1.2.3.4.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                version_key(value)
        data = release_data()
        data['prerelease'] = True
        self.assertIsNone(parse_release(data, '1.0'))

    def test_rejects_foreign_assets_missing_hash_and_invalid_sizes(self):
        for changes in ({'digest': None}, {'browser_download_url': 'https://evil.example/update.exe'},
                        {'size': True}, {'size': 2 ** 40}, {'name': 'other.exe'}, {'state': 'new'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                data = release_data()
                data['assets'][0].update(changes)
                parse_release(data, '1.0')
        data = release_data()
        data['assets'] *= 2
        with self.assertRaises(ValueError):
            parse_release(data, '1.0')

    def test_streamed_staging_checks_size_hash_and_executable_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, 'Beko üser.exe')
            target.write_bytes(b'original')
            payload = executable_payload()
            release = parse_release(release_data(), '1.0')
            stage = StagedUpdate(release, target)
            stage.write(payload[:50])
            stage.write(payload[50:])
            stage.finish()
            self.assertTrue(stage.verified)
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(stage.path.read_bytes(), payload)
            stage.discard()
            self.assertFalse(stage.folder.exists())
            for bad in (payload[:-1], payload[:-1] + b'X'):
                stage = StagedUpdate(release, target)
                stage.write(bad)
                with self.assertRaises(ValueError):
                    stage.finish()
                stage.discard()
            stage = StagedUpdate(release, target)
            with self.assertRaises(ValueError):
                stage.write(payload + b'x')
            stage.discard()
            invalid = b'Z' * 128
            stage = StagedUpdate(parse_release(release_data(payload=invalid), '1.0'), target)
            stage.write(invalid)
            with self.assertRaises(ValueError):
                stage.finish()
            stage.discard()

    def test_source_and_one_folder_builds_never_replace_python(self):
        self.assertFalse(can_self_update())
        with patch('updates.sys', SimpleNamespace(platform='win32', frozen=True, executable='C:/App/Beko.exe', _MEIPASS='C:/App/_internal')):
            self.assertFalse(can_self_update())
        with self.assertRaises(ValueError):
            launch_update(SimpleNamespace(verified=True))


class FakeReply(QNetworkReply):
    def __init__(self, data, status=200, parent=None):
        super().__init__(parent)
        self.data = bytearray(data)
        self.setAttribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute, status)
        self.open(QIODevice.OpenModeFlag.ReadOnly)
        QTimer.singleShot(0, self.deliver)

    def deliver(self):
        if self.isFinished():
            return
        self.readyRead.emit()
        if not self.isFinished():
            self.setFinished(True)
            self.finished.emit()

    def readData(self, maximum):
        data = bytes(self.data[:maximum])
        del self.data[:maximum]
        return data

    def bytesAvailable(self):
        return len(self.data) + super().bytesAvailable()

    def abort(self):
        if not self.isFinished():
            self.setError(QNetworkReply.NetworkError.OperationCanceledError, 'Cancelled')
            self.setFinished(True)
            self.finished.emit()


class QtUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtWidgets import QMainWindow, QMenu
        self.window = QMainWindow()
        self.window._open_message = Mock()
        self.window._show_message = Mock()
        self.window.controller = SimpleNamespace(manager=Mock())
        self.updater = UpdateController(self.window, QMenu(self.window))
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.updater.close)
        self.addCleanup(set_language, 'en')

    def respond(self, data, *, manual=True, status=200):
        reply = FakeReply(json.dumps(data).encode(), status, self.updater)
        with patch.object(self.updater.network, 'get', return_value=reply):
            self.updater.check(manual=manual)
            for _ in range(5):
                self.app.processEvents()

    def test_update_notification_never_downloads_without_acceptance(self):
        self.assertFalse(self.updater.timer.isActive())
        with patch('ui.updates.can_self_update', return_value=True), patch.object(self.updater, 'download') as download:
            self.respond(release_data())
            box = self.window._open_message.call_args.args[0]
            self.assertIn('1.1', box.text())
            self.assertIsNone(self.updater.stage)
            download.assert_not_called()
            box.finished.emit(QMessageBox.StandardButton.No)
            download.assert_not_called()
            box.finished.emit(QMessageBox.StandardButton.Yes)
            download.assert_called_once()

    def test_missing_release_is_silent_in_background_and_manual_check_reports_status(self):
        self.respond({}, manual=False, status=404)
        self.window._show_message.assert_not_called()
        self.respond({}, status=404)
        self.assertIn('No published release', self.window._show_message.call_args.args[2])

    def test_no_update_and_active_tasks_block_installation(self):
        self.respond(release_data('v1.0'))
        self.assertIn('latest', self.window._show_message.call_args.args[2])
        self.window.controller.manager.has_update_work.return_value = True
        with patch.object(self.window, 'close') as close:
            self.updater._install_requested()
            close.assert_not_called()
        self.assertIn('Finish or cancel', self.window._show_message.call_args.args[2])

    def test_actual_qt_download_is_staged_verified_and_requires_second_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, 'Beko.exe')
            target.write_bytes(b'old exe')
            release = parse_release(release_data(), '1.0')
            reply = FakeReply(executable_payload(), parent=self.updater)
            with patch('ui.updates.sys.executable', str(target)), patch('ui.updates.can_self_update', return_value=True), \
                    patch.object(self.updater.network, 'get', return_value=reply):
                self.updater.download(release)
                for _ in range(8):
                    self.app.processEvents()
            self.assertIsNotNone(self.updater.pending_install)
            self.assertTrue(self.updater.pending_install.verified)
            self.assertEqual(target.read_bytes(), b'old exe')
            self.window._open_message.assert_called_once()
            self.updater.close()
            self.assertEqual(list(Path(temp).iterdir()), [target])

    def test_cancel_discards_partial_download_and_turkish_strings_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, 'Beko.exe')
            target.write_bytes(b'old exe')
            reply = FakeReply(executable_payload(), parent=self.updater)
            with patch('ui.updates.sys.executable', str(target)), patch('ui.updates.can_self_update', return_value=True), \
                    patch.object(self.updater.network, 'get', return_value=reply):
                self.updater.download(parse_release(release_data(), '1.0'))
                self.updater._cancel()
                self.app.processEvents()
            self.assertIsNone(self.updater.stage)
            self.assertIsNone(self.updater.pending_install)
            self.assertEqual(list(Path(temp).iterdir()), [target])
            self.window._show_message.assert_not_called()
        set_language('tr')
        self.assertEqual(tr('Software update'), 'Uygulama güncellemesi')

    def test_corrupt_download_never_offers_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, 'Beko.exe')
            target.write_bytes(b'original')
            reply = FakeReply(executable_payload()[:-1] + b'X', parent=self.updater)
            with patch('ui.updates.sys.executable', str(target)), patch('ui.updates.can_self_update', return_value=True), \
                    patch.object(self.updater.network, 'get', return_value=reply):
                self.updater.download(parse_release(release_data(), '1.0'))
                for _ in range(5):
                    self.app.processEvents()
            self.assertIsNone(self.updater.pending_install)
            self.window._open_message.assert_not_called()
            self.window._show_message.assert_called_once()
            self.assertEqual(list(Path(temp).iterdir()), [target])
            self.assertEqual(target.read_bytes(), b'original')

    def test_closing_aborts_download_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, 'Beko.exe')
            target.write_bytes(b'original')
            reply = FakeReply(executable_payload(), parent=self.updater)
            with patch('ui.updates.sys.executable', str(target)), patch('ui.updates.can_self_update', return_value=True), \
                    patch.object(self.updater.network, 'get', return_value=reply):
                self.updater.download(parse_release(release_data(), '1.0'))
                self.updater.close()
            self.assertIsNone(self.updater.reply)
            self.assertIsNone(self.updater.stage)
            self.assertEqual(list(Path(temp).iterdir()), [target])
            self.window._show_message.assert_not_called()
