"""Debounced Qt persistence using the existing versioned settings store."""
from PySide6.QtCore import QObject, QTimer, Signal
from copy import deepcopy

from settings import DEFAULTS, SettingsStore
from utils import configure_logging, log_warning


class Preferences(QObject):
    message = Signal(str, str, str, str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.store = SettingsStore(path)
        self._pending = None
        self._closed = False
        self._reported = set()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.flush)

    def load(self):
        try:
            data, warnings = self.store.load()
            configure_logging(data["log_max_bytes"], data["log_backup_count"],
                              repeat_seconds=data['log_repeat_seconds'])
        except (OSError, ValueError) as error:
            self.store.writable = False
            configure_logging(DEFAULTS['log_max_bytes'], DEFAULTS['log_backup_count'],
                              repeat_seconds=DEFAULTS['log_repeat_seconds'])
            self._report("load", "Preferences could not be loaded. Default values are in use. "
                         "The original settings file will not be overwritten.", str(error))
            return dict(DEFAULTS)
        if warnings:
            self._report("load", "Some preferences were invalid or recovered. "
                         "Valid settings or defaults were loaded.", "\n".join(warnings))
        return data

    def schedule(self, values):
        if not self._closed and self.store.writable:
            self._pending = {**(self._pending or {}), **deepcopy(values)}
            self.timer.start()

    def flush(self):
        self.timer.stop()
        if self._pending is None:
            return True
        try:
            if not self.store.writable:
                raise PermissionError('Settings writes are disabled to preserve the original file.')
            self.store.save(self._pending)
        except (OSError, ValueError) as error:
            self._report("save", "Your preferences could not be saved. "
                         "They will still apply for this session.", str(error))
            return False
        self._pending = None
        return True

    def close(self):
        saved = self.flush()
        self._closed = True
        return saved

    def _report(self, operation, text, details):
        log_warning(f"Preferences {operation}", details, [("file", self.store.path)],
                    once_key=("qt-preferences", operation, str(self.store.path), details),
                    event=f'preferences_{operation}_warning', component='preferences')
        if operation not in self._reported:
            self._reported.add(operation)
            self.message.emit("warning", "Preferences warning", text, details)
