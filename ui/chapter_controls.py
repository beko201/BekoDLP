"""Source chapter markers or separate files, with explicit format limits."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QGroupBox, QLabel, QVBoxLayout
from ui.i18n import register


class ChapterControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Chapters', parent)
        layout = QVBoxLayout(self)
        self.mode = QComboBox(self)
        self.mode.setAccessibleName('Chapter output')
        for label, value in (('Do not process chapters', 'none'), ('Embed chapter markers', 'embed'),
                             ('Save each chapter as a separate file', 'split')):
            self.mode.addItem(label, value)
        layout.addWidget(self.mode)
        hint = QLabel('Embedding supports MP3, M4A, MP4 and MKV. Separate files share one output folder and use the selected codecs. Copying video may move cuts to keyframes. Missing chapters keep the normal output with a warning.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.mode.currentIndexChanged.connect(lambda *_: self.changed.emit())
        register(self)

    def values(self):
        return {'chapter_mode': self.mode.currentData()}

    def restore(self, data):
        blocker = QSignalBlocker(self.mode)
        self.mode.setCurrentIndex(self.mode.findData(data.get('chapter_mode', 'none')))
        del blocker
