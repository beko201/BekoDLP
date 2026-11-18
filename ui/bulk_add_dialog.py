"""Bulk entry dialog for video and playlist URLs."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)
from ui.i18n import register, tr


class BulkAddDialog(QDialog):
    links_submitted = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add multiple links")
        self.resize(580, 430)
        layout = QVBoxLayout(self)
        label = QLabel("Paste one video or playlist URL per line.", self)
        label.setWordWrap(True)
        layout.addWidget(label)
        self.links_edit = QPlainTextEdit(self)
        self.links_edit.setPlaceholderText("https://www.youtube.com/watch?v=…\nhttps://www.youtube.com/playlist?list=…")
        layout.addWidget(self.links_edit)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("mutedLabel")
        layout.addWidget(self.count_label)
        self.buttons = QDialogButtonBox(self)
        self.submit_button = self.buttons.addButton("Add to queue", QDialogButtonBox.ButtonRole.AcceptRole)
        self.submit_button.setEnabled(False)
        self.buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.buttons.accepted.connect(lambda: self.links_submitted.emit(self.links()))
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.links_edit.textChanged.connect(self._update_count)
        self._update_count()
        register(self, self._update_count)

    def links(self):
        return [line.strip() for line in self.links_edit.toPlainText().splitlines() if line.strip()]

    def _update_count(self):
        count = len(self.links())
        self.count_label.setText(tr('{count} link' if count == 1 else '{count} links', count=count))
        self.submit_button.setEnabled(count > 0)
