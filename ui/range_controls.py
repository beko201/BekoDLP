"""One source-time interval per line; values remain stable across translation."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QGroupBox, QLabel, QPlainTextEdit, QVBoxLayout
from ui.i18n import register


class RangeControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Multiple time ranges', parent)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(self)
        self.editor.setAccessibleName('Time ranges, one per line')
        self.editor.setPlaceholderText('0:30 - 1:10\n2:00 - 2:45')
        self.editor.setMaximumHeight(90)
        layout.addWidget(self.editor)
        hint = QLabel('Enter start - end on each line, using seconds, mm:ss or hh:mm:ss. Up to 100 ranges, in the entered order. Each range becomes a separate numbered file. This replaces the single trim fields. Section mode downloads each range; full mode downloads the source once. Chapter embedding is supported; separate chapter files cannot be combined with ranges.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.editor.textChanged.connect(self.changed.emit)
        register(self)

    def values(self):
        return {'trim_ranges': self.editor.toPlainText().strip()}

    def restore(self, data):
        blocker = QSignalBlocker(self.editor)
        self.editor.setPlainText(data.get('trim_ranges', ''))
        del blocker
