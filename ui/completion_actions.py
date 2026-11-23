"""Session-only actions after a newly observed successful queue, with cancellable power countdowns."""
import math
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from ui.i18n import register, tr

ACTION_LABELS = {'none': 'Do nothing', 'folder': 'Open download folder', 'close': 'Close application',
                 'sleep': 'Sleep', 'hibernate': 'Hibernate', 'shutdown': 'Shut down computer'}


class CompletionActions(QObject):
    reset_requested = Signal()
    message = Signal(str, str, str, str)
    notice = Signal(str)

    def __init__(self, manager, execute, parent=None, *, countdown_seconds=30, clock=time.monotonic):
        super().__init__(parent)
        self.manager, self.execute = manager, execute
        self.clock, self.countdown_seconds = clock, countdown_seconds
        self.action = 'none'
        self.baseline, self.seen = set(), set()
        self.dialog = None
        self.deadline = None
        self.fingerprint = None
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)

    def arm(self, action):
        self.reset(notify=False)
        if self.closed or action == 'none':
            return
        if action not in ACTION_LABELS:
            raise ValueError('Unknown after-download action.')
        tasks, _, _, closing = self.manager.completion_snapshot()
        if closing:
            return
        self.action = action
        self.baseline = {tid for tid, task in tasks.items() if task['status'].startswith(('Completed', 'Error', 'Cancel'))}
        self.seen = set(tasks) - self.baseline
        self.timer.start()

    def reset(self, *, notify=True):
        had_action = self.action != 'none'
        self.action = 'none'
        self.timer.stop()
        self.deadline, self.fingerprint = None, None
        if self.dialog is not None:
            self.dialog.blockSignals(True)
            self.dialog.reject()
            self.dialog.deleteLater()
            self.dialog = None
        if had_action and notify:
            self.reset_requested.emit()

    def cancel(self):
        self.reset()
        self.notice.emit('After-download action cancelled.')

    def poll(self):
        if self.closed or self.action == 'none':
            return
        tasks, busy, paused, closing = self.manager.completion_snapshot()
        current = {tid: task for tid, task in tasks.items() if tid not in self.baseline}
        self.seen.update(current)
        if closing or self.seen - set(current) or any(t['status'].startswith(('Error', 'Cancel')) for t in current.values()):
            self.cancel()
            return
        ready = bool(current) and not busy and not paused and all(t['status'].startswith('Completed') for t in current.values())
        fingerprint = tuple((tid, task['status'], task['output_path']) for tid, task in sorted(current.items()))
        if self.deadline is not None and (not ready or fingerprint != self.fingerprint):
            self.cancel()
            return
        if not ready:
            return
        if self.action in ('sleep', 'hibernate', 'shutdown'):
            if self.deadline is None:
                self.deadline = self.clock() + self.countdown_seconds
                self.fingerprint = fingerprint
                self.dialog = QDialog(self.parent())
                self.dialog.setWindowTitle('After-download action')
                layout = QVBoxLayout(self.dialog)
                self.label = QLabel(self.dialog)
                self.label.setWordWrap(True)
                layout.addWidget(self.label)
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self.dialog)
                buttons.rejected.connect(self.dialog.reject)
                layout.addWidget(buttons)
                self.dialog.rejected.connect(self.cancel)
                register(self.dialog)
                self.dialog.show()
            remaining = max(0, math.ceil(self.deadline - self.clock()))
            self.label.setText(tr('Queue complete. {action} in {seconds} seconds.',
                                  action=tr(ACTION_LABELS[self.action]), seconds=remaining))
            if remaining:
                return
        action = self.action
        paths = [task['output_path'] for task in current.values() if task['output_path']]
        self.reset()
        try:
            self.execute(action, paths)
        except (OSError, ValueError, RuntimeError) as error:
            self.message.emit('warning', 'After-download action', 'The after-download action could not be completed.', str(error))

    def close(self):
        self.closed = True
        self.reset()
