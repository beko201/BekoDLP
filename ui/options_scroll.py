"""Keep the options form readable when the task preview needs vertical space."""
from PySide6.QtCore import QEvent, QSize
from PySide6.QtWidgets import QFrame, QScrollArea, QSizePolicy


class OptionsScrollArea(QScrollArea):
    def __init__(self, controls, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setWidget(controls)
        controls.installEventFilter(self)

    def sizeHint(self):
        # QScrollArea's cached hint otherwise keeps the collapsed form's height.
        return self.widget().sizeHint() if self.widget() is not None else super().sizeHint()

    def minimumSizeHint(self):
        return QSize(0, 80)

    def eventFilter(self, watched, event):
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self.updateGeometry()
        return super().eventFilter(watched, event)
