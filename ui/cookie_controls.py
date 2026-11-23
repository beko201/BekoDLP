"""Cookie source selection without reading browser data or cookie contents."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QWidget)

from cookie_options import COOKIE_BROWSERS, cookie_selection
from ui.i18n import register, tr


class CookieControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Cookies', parent)
        layout = QGridLayout(self)
        self.mode_combo = QComboBox(self)
        self.mode_combo.setAccessibleName('Cookie source')
        for label, value in (('Do not use cookies', 'none'), ('Cookie file', 'file'), ('Browser cookies', 'browser')):
            self.mode_combo.addItem(label, value)
        label = QLabel('Cookie source', self)
        label.setBuddy(self.mode_combo)
        layout.addWidget(label, 0, 0)
        layout.addWidget(self.mode_combo, 0, 1)
        layout.setColumnStretch(1, 1)
        self.file_row = QWidget(self)
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_edit = QLineEdit(self.file_row)
        self.file_edit.setAccessibleName('Cookie file')
        self.file_edit.setPlaceholderText('Path to cookies.txt')
        self.file_button = QPushButton('Choose cookie file…', self.file_row)
        self.file_button.clicked.connect(self.choose_file)
        file_layout.addWidget(self.file_edit, 1)
        file_layout.addWidget(self.file_button)
        layout.addWidget(self.file_row, 1, 0, 1, 2)
        self.browser_row = QWidget(self)
        browser_layout = QGridLayout(self.browser_row)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        self.browser_combo = QComboBox(self.browser_row)
        self.browser_combo.setAccessibleName('Cookie browser')
        names = dict(chrome='Chrome', edge='Edge', firefox='Firefox', brave='Brave', opera='Opera',
                     vivaldi='Vivaldi', chromium='Chromium', whale='Whale', safari='Safari')
        for browser in COOKIE_BROWSERS:
            self.browser_combo.addItem(names[browser], browser)
        self.profile_edit = QLineEdit(self.browser_row)
        self.profile_edit.setAccessibleName('Browser profile')
        self.profile_edit.setPlaceholderText('Default profile, or enter a profile name / path')
        browser_label = QLabel('Browser', self.browser_row)
        browser_label.setBuddy(self.browser_combo)
        profile_label = QLabel('Browser profile', self.browser_row)
        profile_label.setBuddy(self.profile_edit)
        browser_layout.addWidget(browser_label, 0, 0)
        browser_layout.addWidget(self.browser_combo, 0, 1)
        browser_layout.addWidget(profile_label, 1, 0)
        browser_layout.addWidget(self.profile_edit, 1, 1)
        browser_layout.setColumnStretch(1, 1)
        layout.addWidget(self.browser_row, 2, 0, 1, 2)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setObjectName('mutedLabel')
        layout.addWidget(self.hint, 3, 0, 1, 2)
        self.mode_combo.currentIndexChanged.connect(self._changed)
        for signal in (self.browser_combo.currentIndexChanged, self.file_edit.textChanged, self.profile_edit.textChanged):
            signal.connect(lambda *_: self.changed.emit())
        self._refresh()
        register(self, self._refresh)

    def _changed(self):
        self._refresh()
        self.changed.emit()

    def _refresh(self):
        mode = self.mode_combo.currentData()
        self.file_row.setVisible(mode == 'file')
        self.browser_row.setVisible(mode == 'browser')
        self.hint.setText(tr({
            'none': 'Cookie selection applies to newly added tasks.',
            'file': 'Use a Netscape cookies.txt file. The original file is kept unchanged.',
            'browser': 'Leave the profile blank for the default. Close the browser if its cookies cannot be read.',
        }[mode]))

    def values(self):
        return dict(cookie_mode=self.mode_combo.currentData(), cookie_file=self.file_edit.text().strip(),
                    cookie_browser=self.browser_combo.currentData(), cookie_profile=self.profile_edit.text().strip())

    def restore(self, data):
        values = cookie_selection(data)
        blockers = [QSignalBlocker(widget) for widget in
                    (self, self.mode_combo, self.browser_combo, self.file_edit, self.profile_edit)]
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(values['cookie_mode']))
        self.browser_combo.setCurrentIndex(self.browser_combo.findData(values['cookie_browser']))
        self.file_edit.setText(values['cookie_file'])
        self.profile_edit.setText(values['cookie_profile'])
        self._refresh()
        del blockers

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('Choose cookie file'), self.file_edit.text(),
            tr('Cookie files (*.txt);;All files (*)'), options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.file_edit.setText(path)
