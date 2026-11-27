"""Report unexpected Python callbacks and worker failures on the Qt thread."""
import sys
import threading
import traceback

from PySide6.QtCore import QObject, Signal

from errors import DownloadCancelled
from app_logging import log_error


class QtExceptionReporter(QObject):
    message = Signal(str, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._previous_sys = None
        self._previous_thread = None
        self._sys_hook = self._handle_sys
        self._thread_hook = self._handle_thread

    def install(self):
        self._previous_sys = sys.excepthook
        self._previous_thread = threading.excepthook
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook

    def restore(self):
        if sys.excepthook is self._sys_hook:
            sys.excepthook = self._previous_sys
        if threading.excepthook is self._thread_hook:
            threading.excepthook = self._previous_thread

    def _handle_sys(self, error_type, error, tb):
        if issubclass(error_type, (KeyboardInterrupt, SystemExit)):
            self._previous_sys(error_type, error, tb)
            return
        self._report(error_type, error, tb, threading.current_thread().name)

    def _handle_thread(self, args):
        if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            self._previous_thread(args)
            return
        self._report(args.exc_type, args.exc_value, args.exc_traceback, getattr(args.thread, "name", "unknown"))

    def _report(self, error_type, error, tb, thread_name):
        if issubclass(error_type, DownloadCancelled):
            return
        details = "".join(traceback.format_exception(error_type, error, tb))
        log_error("Unexpected Qt application error", [("thread", thread_name), ("error", error)], tb=details,
                  event='uncaught_exception', component='qt')
        self.message.emit("critical", "Unexpected error",
                          "An unexpected error occurred. Use the diagnostic actions for details.", details)
