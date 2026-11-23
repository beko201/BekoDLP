"""Proxy routing and a per-task download speed limit."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QWidget

from network_options import network_selection
from ui.i18n import register, tr


class NetworkControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Network', parent)
        layout = QGridLayout(self)
        self.proxy_combo = QComboBox(self)
        self.proxy_combo.setAccessibleName('Proxy mode')
        for label, value in (('System proxy', 'system'), ('Direct connection', 'direct'), ('Custom proxy', 'custom')):
            self.proxy_combo.addItem(label, value)
        self.proxy_edit = QLineEdit(self)
        self.proxy_edit.setAccessibleName('Proxy URL')
        self.proxy_edit.setPlaceholderText('http://127.0.0.1:8080 or socks5://127.0.0.1:1080')
        self.rate_spin = QSpinBox(self)
        self.rate_spin.setRange(0, 1048576)
        self.rate_spin.setSpecialValueText('Unlimited')
        self.rate_spin.setAccessibleName('Download speed limit')
        self.rate_unit = QComboBox(self)
        self.rate_unit.addItem('KiB/s', 1024)
        self.rate_unit.addItem('MiB/s', 1024 ** 2)
        rate = QWidget(self)
        row = QHBoxLayout(rate)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.rate_spin, 1)
        row.addWidget(self.rate_unit)
        for index, (text, widget) in enumerate((('Proxy mode', self.proxy_combo), ('Proxy URL', self.proxy_edit),
                                               ('Speed limit per task', rate))):
            label = QLabel(text, self)
            layout.addWidget(label, index, 0)
            layout.addWidget(widget, index, 1)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setObjectName('mutedLabel')
        layout.addWidget(self.hint, 3, 0, 1, 2)
        layout.setColumnStretch(1, 1)
        self.proxy_combo.currentIndexChanged.connect(self._changed)
        for signal in (self.proxy_edit.textChanged, self.rate_spin.valueChanged, self.rate_unit.currentIndexChanged):
            signal.connect(lambda *_: self.changed.emit())
        self._refresh()
        register(self, self._refresh)

    def _changed(self):
        self._refresh()
        self.changed.emit()

    def _refresh(self):
        self.proxy_edit.setEnabled(self.proxy_combo.currentData() == 'custom')
        self.hint.setText(tr('Applies to new tasks. Parallel video/audio share the speed limit. Zero means unlimited.'))

    def values(self):
        rate = self.rate_spin.value() * self.rate_unit.currentData()
        return dict(proxy_mode=self.proxy_combo.currentData(), proxy_url=self.proxy_edit.text().strip(),
                    download_rate=str(rate) if rate else '')

    def restore(self, data):
        selected = network_selection(data)
        blockers = [QSignalBlocker(widget) for widget in
                    (self, self.proxy_combo, self.proxy_edit, self.rate_spin, self.rate_unit)]
        self.proxy_combo.setCurrentIndex(self.proxy_combo.findData(selected['proxy_mode']))
        self.proxy_edit.setText(selected['proxy_url'])
        rate = int(selected['download_rate'] or 0)
        unit = 1024 ** 2 if rate and rate % (1024 ** 2) == 0 else 1024
        self.rate_unit.setCurrentIndex(self.rate_unit.findData(unit))
        self.rate_spin.setValue(rate // unit)
        self._refresh()
        del blockers
