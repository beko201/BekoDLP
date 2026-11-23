"""Wait for asynchronous window and queue cleanup before removing test files."""
import time

from PySide6.QtWidgets import QApplication, QMessageBox


def finish_window_close(window, timeout=5):
    window.close()
    deadline = time.monotonic() + timeout
    persistence = window.queue_persistence
    while not (window._allow_close and persistence._closed):
        # A few diagnostic fixtures bypass ordinary window shutdown on purpose.
        if window._allow_close and not persistence._closing:
            persistence.close()
        window.controller.poll()
        QApplication.processEvents()
        for box in list(window._message_boxes):
            box.done(QMessageBox.StandardButton.Ok)
        if time.monotonic() >= deadline:
            raise AssertionError('Window or final queue save did not finish closing')
        time.sleep(.005)
    QApplication.processEvents()
