"""Preview panel for the selected task thumbnail and title."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget
from ui.i18n import register, tr


class PreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tid = None
        self._image = None
        self.setMinimumWidth(230)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        heading = QLabel("Preview", self)
        heading.setStyleSheet("font-weight: bold; font-size: 12pt;")
        layout.addWidget(heading)
        self.image_label = QLabel(self)
        self.image_label.setProperty('_i18n_user_text', True)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.image_label.installEventFilter(self)
        self.image_label.setWordWrap(True)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setFixedHeight(160)
        self.image_label.setStyleSheet("background: #171b22; border-radius: 8px; color: #aab5c6;")
        layout.addWidget(self.image_label)
        self.title_label = QLabel(self)
        self.title_label.setProperty('_i18n_user_text', True)
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.title_label)
        layout.addStretch()
        self.clear()
        register(self, self._retranslate)

    def _retranslate(self):
        if self._image is None:
            self.image_label.setText(tr(self._state_text))
        self.title_label.setText(tr('Resolving title…') if self._title == '(resolving title...)' else self._title)

    def clear(self):
        self._tid = None
        self._image = None
        self._title = ''
        self._state_text = 'Select a task to see its preview'
        self.image_label.clear()
        self.image_label.setText(tr(self._state_text))
        self.title_label.clear()

    def set_task(self, tid, title):
        if self._tid != tid:
            self.clear()
            self._tid = tid
            self._state_text = 'Preview not available yet.'
            self.image_label.setText(tr(self._state_text))
        self._title = title
        self._retranslate()

    def set_state(self, tid, state):
        if tid == self._tid and self._image is None:
            self._state_text = {"waiting": "Preview not available yet.",
                                      "loading": "Loading preview…",
                                      "unavailable": "Preview unavailable"}[state]
            self.image_label.setText(tr(self._state_text))

    def set_image(self, tid, image):
        if tid == self._tid:
            self._image = QImage(image)
            self._redraw()

    def _redraw(self):
        if self._image is not None:
            pixmap = QPixmap.fromImage(self._image)
            self.image_label.setPixmap(pixmap.scaled(
                self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def eventFilter(self, watched, event):
        if watched is self.image_label and event.type() == QEvent.Type.Resize:
            self._redraw()
        return super().eventFilter(watched, event)
