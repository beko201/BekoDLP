"""Serialize queue saves off the GUI thread, with one write in flight at a time."""
import threading

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from queue_store import QueueStore
from app_logging import log_warning


class QueuePersistence(QObject):
    message = Signal(str, str, str, str)
    closed = Signal(bool)
    _save_finished = Signal(object, object)

    def __init__(self, manager, path, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.store = QueueStore(path)
        self._saved_revision = None
        self._reported = set()
        self._closed = False
        self._closing = False
        self._close_result = None
        self._thread = None
        self._callbacks = []
        self._save_finished.connect(self._complete_save, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.flush)

    def load(self):
        try:
            document, warnings = self.store.load()
            count = self.manager.restore_queue(document) if not self.manager.summary().total else 0
        except (OSError, ValueError) as error:
            self.store.writable = False
            self._report('load', 'The saved queue could not be loaded. The original file will not be overwritten.', str(error))
            count = 0
        else:
            if warnings:
                self._report('load', 'The saved queue was recovered. See details for the original file.', '\n'.join(warnings))
        # Normalized interrupted tasks are already represented by the loaded document.
        # A recovered/missing primary must be rewritten even without new UI work.
        if self.store.path.exists() and not self.store._recovered:
            self._saved_revision = self.manager.queue_revision
        self.timer.start()
        return count

    @property
    def busy(self):
        return self._thread is not None

    def flush(self, on_finished=None):
        """Request the latest revision; None means it is still being saved.

        Optional callbacks run on the GUI thread once the latest requested
        state is durable, or the write fails. Repeated requests share one job.
        """
        if on_finished is not None:
            self._callbacks.append(on_finished)
        if self._closed:
            self._settled(self._close_result)
            return self._close_result
        if self.busy:
            return None
        if not self.store.writable:
            self._settled(True)
            return True  # A load warning already explained that the original is protected.
        if self.manager.queue_revision == self._saved_revision:
            self._settled(True)
            return True

        def save():
            revision, error = None, None
            try:
                revision, document = self.manager.queue_snapshot()
                self.store.save(document)
            except Exception as failure:
                error = failure
            self._save_finished.emit(revision, error)

        self._thread = threading.Thread(target=save, name='queue-save', daemon=True)
        try:
            self._thread.start()
        except RuntimeError as error:
            self._complete_save(None, error)
            return False
        return None

    def _complete_save(self, revision, error):
        self._thread = None
        if error is not None:
            self._report('save', 'Your queue could not be saved. Recent changes may not be restored next time.', str(error))
            self._settled(False)
            return
        self._saved_revision = revision
        # Changes made during the write replace pending requests with the
        # latest snapshot; older writes can never overtake newer ones.
        self.flush()

    def _settled(self, success):
        callbacks, self._callbacks = self._callbacks, []
        if self._closing and not self._closed:
            self._closed = True
            self._close_result = success
            self.closed.emit(success)
        for callback in callbacks:
            callback(success)

    def close(self):
        """Finish the final save asynchronously before allowing the window to exit."""
        self.timer.stop()
        self._closing = True
        self.flush()
        return self._close_result

    def _report(self, operation, text, details):
        log_warning(f'Queue {operation}', details, [('file', self.store.path)],
                    once_key=('queue', operation, str(self.store.path), details),
                    event=f'queue_{operation}_warning', component='queue')
        if operation not in self._reported:
            self._reported.add(operation)
            self.message.emit('warning', 'Queue warning', text, details)
