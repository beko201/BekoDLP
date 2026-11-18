"""Nonblocking release checks, opt-in downloads, and restart confirmation."""
import json
import sys

from PySide6.QtCore import QObject, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from app_info import APP_NAME, APP_VERSION, UPDATE_REPOSITORY
from ui.i18n import register, tr
from updates import (MAX_RELEASE_BYTES, StagedUpdate, can_self_update, launch_update,
                     parse_release, release_api_url)
from utils import log_warning


class UpdateController(QObject):
    def __init__(self, window, menu):
        super().__init__(window)
        self.window = window
        self.network = QNetworkAccessManager(self)
        self.reply = None
        self.stage = None
        self.pending_install = None
        self.progress = None
        self.closed = False
        self.manual = False
        self.failure = ''
        self.data = bytearray()
        self.action = QAction('Check for updates…', self)
        self.action.triggered.connect(lambda: self.check(manual=True))
        menu.addSeparator()
        menu.addAction(self.action)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.check)
        # Source runs and tests never contact GitHub automatically.
        if getattr(sys, 'frozen', False):
            self.timer.start(5000)

    def _request(self, url):
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b'User-Agent', f'{APP_NAME}/{APP_VERSION}'.encode('ascii'))
        request.setRawHeader(b'Accept', b'application/vnd.github+json')
        request.setTransferTimeout(30000)
        request.setMaximumRedirectsAllowed(5)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self.reply = self.network.get(request)
        self.reply.setReadBufferSize(256 * 1024)
        self.action.setEnabled(False)

    def check(self, *, manual=False):
        if self.closed or self.reply is not None:
            return
        if self.pending_install is not None:
            self._offer_install()
            return
        self.manual = manual
        self.failure = ''
        self.data.clear()
        self._request(release_api_url())
        self.reply.readyRead.connect(self._read_metadata)
        self.reply.finished.connect(self._checked)

    def _read_metadata(self):
        if self.reply is None or self.failure:
            return
        chunk = bytes(self.reply.readAll())
        if len(self.data) + len(chunk) > MAX_RELEASE_BYTES:
            self.failure = 'The release response exceeds the size limit.'
            self.reply.abort()
        else:
            self.data.extend(chunk)

    def _take_reply(self):
        reply, self.reply = self.reply, None
        self.action.setEnabled(not self.closed)
        reply.deleteLater()
        return reply

    def _checked(self):
        if self.reply is None:
            return
        self._read_metadata()
        reply = self._take_reply()
        if self.closed:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status == 404:
            if self.manual:
                self._info('No published release is available yet.')
            return
        if self.failure or reply.error() != QNetworkReply.NetworkError.NoError or status != 200:
            self._error(self.failure or reply.errorString(), notify=self.manual)
            return
        try:
            release = parse_release(json.loads(self.data.decode('utf-8')), APP_VERSION)
        except (ValueError, TypeError, RecursionError) as error:
            self._error(str(error), notify=self.manual)
            return
        finally:
            self.data.clear()
        if release is None:
            if self.manual:
                self._info('You are using the latest version.')
            return
        box = QMessageBox(self.window)
        box.setWindowTitle(tr('Software update'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setIcon(QMessageBox.Icon.Information)
        if can_self_update():
            box.setText(tr('Version {version} is available. Download it now?', version=release.version))
            box.setInformativeText(tr('Your current downloads will continue. Installation requires a separate confirmation.'))
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.finished.connect(lambda result: self.download(release) if result == QMessageBox.StandardButton.Yes else None)
        else:
            box.setText(tr('Version {version} is available. Open the release page?', version=release.version))
            box.setInformativeText(tr('Automatic installation is available in the single-file Windows build.'))
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.finished.connect(lambda result: QDesktopServices.openUrl(QUrl(
                f'https://github.com/{UPDATE_REPOSITORY}/releases/latest')) if result == QMessageBox.StandardButton.Yes else None)
        self.window._open_message(box)

    def download(self, release):
        if self.closed or self.reply is not None or not can_self_update():
            return
        try:
            self.stage = StagedUpdate(release, sys.executable)
        except (OSError, ValueError) as error:
            self._error(str(error))
            return
        self.failure = ''
        self._request(release.url)
        self.progress = QProgressDialog(tr('Downloading update…'), tr('Cancel'), 0, 100, self.window)
        self.progress.setWindowTitle(tr('Software update'))
        self.progress.setWindowModality(Qt.WindowModality.NonModal)
        self.progress.setMinimumDuration(0)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        register(self.progress)
        self.progress.canceled.connect(self._cancel)
        self.progress.show()
        self.reply.readyRead.connect(self._read_download)
        self.reply.finished.connect(self._downloaded)

    def _cancel(self):
        self.failure = 'cancelled'
        if self.reply is not None:
            self.reply.abort()

    def _read_download(self):
        if self.reply is None or self.failure:
            return
        try:
            while self.reply.bytesAvailable():
                self.stage.write(bytes(self.reply.read(256 * 1024)))
            self.progress.setValue(min(99, self.stage.size * 100 // self.stage.release.size))
        except (OSError, ValueError) as error:
            self.failure = str(error)
            self.reply.abort()

    def _downloaded(self):
        if self.reply is None:
            return
        self._read_download()
        reply = self._take_reply()
        self.progress.reset()
        self.progress.deleteLater()
        self.progress = None
        stage, self.stage = self.stage, None
        try:
            if self.closed or self.failure == 'cancelled':
                stage.discard()
                return
            if self.failure or reply.error() != QNetworkReply.NetworkError.NoError:
                raise ValueError(self.failure or reply.errorString())
            if reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) != 200:
                raise ValueError('The update server did not return a complete executable.')
            stage.finish()
        except (OSError, ValueError) as error:
            stage.discard()
            self._error(str(error))
            return
        self.pending_install = stage
        self._offer_install()

    def _offer_install(self):
        if self.closed or self.pending_install is None:
            return
        box = QMessageBox(self.window)
        box.setWindowTitle(tr('Software update'))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(tr('The update is downloaded and verified. Restart and install now?'))
        box.setInformativeText(tr('Installation waits until all queued and active downloads finish. You can also install later from Help → Check for updates.'))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.finished.connect(lambda result: self._install_requested() if result == QMessageBox.StandardButton.Yes else None)
        self.window._open_message(box)

    def _install_requested(self):
        if self.closed:
            return
        if self.window.controller.manager.has_update_work():
            self._info('Finish or cancel pending downloads before installing the update. The verified update is ready for this session.')
            return
        self.window._update_on_close = True
        self.window.close()

    def apply_on_exit(self):
        if self.pending_install is None:
            return False
        launch_update(self.pending_install)
        self.pending_install = None  # The detached helper now owns the staging folder.
        return True

    def _info(self, text):
        self.window._show_message('information', 'Software update', text, '')

    def _error(self, detail, *, notify=True):
        log_warning('Software update failed', detail, event='update_failed', component='updater')
        if notify and not self.closed:
            self.window._show_message('warning', 'Software update', 'The update could not be completed. Your current version is unchanged.', detail)

    def close(self, *, installing=False):
        self.closed = True
        self.timer.stop()
        self.action.setEnabled(False)
        if self.reply is not None:
            self._cancel()
        if self.pending_install is not None and not installing:
            self.pending_install.discard()
            self.pending_install = None
