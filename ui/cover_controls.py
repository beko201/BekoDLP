"""Opt-in cover art for supported output containers."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QVBoxLayout

from ui.i18n import register


class CoverControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Cover artwork', parent)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox('Embed the source thumbnail as cover artwork', self)
        self.enabled.setAccessibleName('Embed cover artwork')
        layout.addWidget(self.enabled)
        hint = QLabel('Supports MP3, M4A, FLAC, MP4 and MKV. A missing image or unsupported format keeps the media and adds a warning. Audio and video are not re-encoded for the cover.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.enabled.toggled.connect(lambda *_: self.changed.emit())
        register(self)

    def values(self):
        return {'embed_cover': 'on' if self.enabled.isChecked() else 'off'}

    def restore(self, data):
        blocker = QSignalBlocker(self.enabled)
        self.enabled.setChecked(data.get('embed_cover', 'off') == 'on')
        del blocker
