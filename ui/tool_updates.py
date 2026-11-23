"""Manual per-tool checks and cancellable downloads outside the GUI thread."""
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout)

import tool_updates as backend
from ui.i18n import register, tr
from utils import DownloadCancelled, which_executable, log_warning

LABELS = {'ytdlp': 'yt-dlp', 'ffmpeg': 'FFmpeg / FFprobe', 'deno': 'Deno'}


class ToolUpdatesDialog(QDialog):
    finished_job = Signal(str, object, object)
    download_progress = Signal(int)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle('Tool updates')
        self.setMinimumWidth(620)
        self.busy = False
        self.closed = False
        self.cancel = threading.Event()
        self.stage = None
        self.releases = {}
        self.versions = {}
        self.rows = {}
        layout = QVBoxLayout(self)
        hint = QLabel('Update yt-dlp, FFmpeg/FFprobe and Deno separately. Downloads are verified with SHA-256. Installation requires an idle queue. Existing tools remain available if an update fails.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        grid = QGridLayout()
        for column, title in enumerate(('Tool', 'Installed version', 'Latest version')):
            grid.addWidget(QLabel(title, self), 0, column)
        for index, (component, label) in enumerate(LABELS.items(), 1):
            current, latest = QLabel('Unknown', self), QLabel('Not checked', self)
            current.setProperty('_i18n_user_text', True)
            latest.setProperty('_i18n_user_text', True)
            check, update = QPushButton('Check', self), QPushButton('Download and install', self)
            update.setEnabled(False)
            check.clicked.connect(lambda _, key=component: self.check(key))
            update.clicked.connect(lambda _, key=component: self.update_tool(key))
            for column, widget in enumerate((QLabel(label, self), current, latest, check, update)):
                grid.addWidget(widget, index, column)
            self.rows[component] = (current, latest, check, update)
        layout.addLayout(grid)
        self.status = QLabel('Checks run only when requested.', self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setProperty('_i18n_user_text', True)
        layout.addWidget(self.status)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.cancel_button = QPushButton('Cancel download', self)
        self.cancel_button.clicked.connect(self.cancel_job)
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.cancel_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished_job.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.download_progress.connect(self.progress.setValue, Qt.ConnectionType.QueuedConnection)
        register(self, self._retranslate)
        if not backend.supported():
            self.status.setText(tr('Tool updates currently support Windows x64.'))
            for row in self.rows.values():
                row[2].setEnabled(False)

    def _controls(self):
        for component, row in self.rows.items():
            row[2].setEnabled(not self.busy and backend.supported() and self.stage is None)
            row[3].setText(tr('Install downloaded update' if self.stage and self.stage.release.component == component else 'Download and install'))
            row[3].setEnabled(not self.busy and backend.supported() and (
                self.stage is not None and self.stage.release.component == component or
                self.stage is None and component in self.releases and backend.is_newer(
                    self.releases[component].version, self.versions.get(component, 'Unknown'))))
        self.cancel_button.setEnabled(self.busy or self.stage is not None)

    def _retranslate(self):
        for component, row in self.rows.items():
            row[0].setText(tr(self.versions.get(component, 'Unknown')))
            row[1].setText(self.releases[component].version if component in self.releases else tr('Not checked'))
        self._controls()

    def _job(self, kind, action):
        if self.closed or self.busy:
            return
        self.busy = True
        self.cancel = self.stage.cancel if self.stage else threading.Event()
        self.progress.setValue(0)
        self._controls()
        def run():
            result, error = None, None
            try:
                result = action()
            except DownloadCancelled:
                error = 'Tool update cancelled.'
            except Exception as failure:
                error = str(failure)
                log_warning('Tool update failed', failure, component='tool_updater')
            try:
                self.finished_job.emit(kind, result, error)
            except RuntimeError:
                self._discard_stage()
        threading.Thread(target=run, name='tool-update-' + kind, daemon=True).start()

    def inspect(self):
        self._job('installed', lambda: {key: backend.installed_version(key) for key in LABELS})

    def check(self, component):
        if self.stage is not None:
            return
        self.status.setText(tr('Checking tool release…'))
        self._job('check', lambda: (component, backend.installed_version(component),
                                  backend.latest_release(component, self.cancel)))

    def update_tool(self, component):
        if self.busy or self.closed:
            return
        if self.stage is not None:
            if self.stage.release.component == component:
                self._activate()
            return
        release = self.releases.get(component)
        if release is None:
            return
        try:
            self.stage = backend.StagedToolUpdate(release)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.status.setText(tr('Downloading and verifying tool update…'))
        def download():
            self.stage.prepare(lambda count, total: self.download_progress.emit(int(count * 100 / total) if total else 0))
            return component
        self._job('download', download)

    def _activate(self):
        try:
            manager = self.window.controller.manager
            if not manager.activate_tool_update(self.stage.activate):
                self.status.setText(tr('Finish or cancel queued downloads before installing the tool update. The verified download is ready.'))
                self._controls()
                return
            component = self.stage.release.component
            self.stage = None
            from ytdlp_process import executable_version
            from diagnostics import invalidate_runtime_info
            executable_version.cache_clear()
            invalidate_runtime_info()
            if component == 'ffmpeg':
                engine = manager.engine
                engine.ffmpeg = which_executable(['ffmpeg'])
                engine.ffprobe = which_executable(['ffprobe'])
                with engine._encoder_lock:
                    engine._encoders = None
                self.window.controller.load_encoders()
            self.versions[component] = self.releases[component].version
            self.rows[component][0].setText(self.versions[component])
            self.status.setText(tr('Tool updated successfully. New downloads use the updated tool.'))
        except (OSError, ValueError) as error:
            self.status.setText(tr('Tool installation failed. Existing tools were preserved.') + '\n' + str(error))
        self._controls()

    def _finished(self, kind, result, error):
        self.busy = False
        if self.closed or error:
            self._discard_stage()
            if self.closed:
                # The worker has finished using this dialog and its signals.
                self.deleteLater()
                return
            self.status.setText(tr(error) if error == 'Tool update cancelled.' else
                tr('Tool update failed. Existing tools were preserved.') + '\n' + str(error))
            self._controls()
            return
        if kind == 'installed':
            self.versions.update(result)
            for key, version in result.items():
                self.rows[key][0].setText(tr(version))
        elif kind == 'check':
            component, version, release = result
            self.versions[component], self.releases[component] = version, release
            self.rows[component][0].setText(tr(version))
            self.rows[component][1].setText(release.version)
            self.status.setText(tr('A tool update is available.' if backend.is_newer(release.version, version) else 'This tool is up to date.'))
        elif kind == 'download':
            self._activate()
        self._controls()

    def _discard_stage(self):
        # A failed filesystem cleanup must not leave an installable stage or
        # interrupt dialog shutdown, including the worker-completion path.
        stage, self.stage = self.stage, None
        if stage is not None:
            try:
                stage.discard()
            except (OSError, ValueError) as error:
                log_warning('Could not remove temporary tool update files', error,
                            [('folder', stage.folder)], component='tool_updater')

    def cancel_job(self):
        self.cancel.set()
        if self.stage:
            self.stage.cancel.set()
            if not self.busy:
                self._discard_stage()
                self.status.setText(tr('Tool update cancelled.'))
                self._controls()

    def reject(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.cancel_job()
        finally:
            super().reject()
            # Keep the signal receiver alive until an active worker reports back.
            if not self.busy:
                self.deleteLater()


class ToolUpdateController:
    def __init__(self, window, menu):
        self.window, self.dialog = window, None
        self.action = QAction('Update download tools…', window)
        self.action.triggered.connect(self.open)
        menu.addAction(self.action)

    def open(self):
        if self.dialog is None or self.dialog.closed:
            self.dialog = ToolUpdatesDialog(self.window)
            dialog = self.dialog
            dialog.finished.connect(lambda result: self._release_dialog(dialog))
            self.dialog.show()
            self.dialog.inspect()
        else:
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()

    def _release_dialog(self, dialog):
        if self.dialog is dialog:
            self.dialog = None

    def close(self):
        if self.dialog:
            self.dialog.reject()
