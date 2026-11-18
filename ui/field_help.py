"""Keyboard-accessible information buttons with localized, nonmodal help."""
from html import escape

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QLabel, QToolButton, QWhatsThis

from ui.help_texts import FIELD_HELP
from ui.i18n import register, tr


class FieldLabel(QLabel):
    def sizeHint(self):
        # Prefer one line when it fits, but keep QLabel's wrapping/minimum hint
        # so narrow columns can grow vertically instead of clipping text.
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 2, metrics.height() + 2)


class FieldHelpButton(QToolButton):
    def __init__(self, topic, field, parent=None):
        super().__init__(parent)
        self.setObjectName('fieldHelpButton')
        self.setText('ⓘ')
        self.setAutoRaise(True)
        self.setFixedSize(20, 20)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self.show_help)
        self.set_topic(topic, field)
        register(self, self._retranslate)

    def set_topic(self, topic, field):
        self.topic, self.field = topic, field
        self._retranslate()

    def _retranslate(self):
        body = tr(FIELD_HELP[self.topic][0])
        self.setAccessibleName(tr('About {field}', field=tr(self.topic)))
        self.setAccessibleDescription(body)
        # Rich text allows Qt to wrap long hover help to the available screen.
        hover = '<qt>' + escape(body).replace('\n', '<br>') + '</qt>'
        self.setToolTip(hover)
        self.field.setToolTip(hover)
        self.field.setAccessibleDescription(body)

    def show_help(self):
        title = escape(tr(self.topic))
        body = escape(tr(FIELD_HELP[self.topic][0])).replace('\n\n', '</p><p>')
        QWhatsThis.showText(self.mapToGlobal(QPoint(0, self.height())),
                           f'<b>{title}</b><p>{body}</p>', self)
