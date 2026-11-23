"""One-selection output profiles with user names preserved across languages."""
from copy import deepcopy

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QInputDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from saved_profiles import builtin_profiles, capture_profile, save_profile, validate_profiles
from ui.i18n import register, tr


class ProfileControls(QWidget):
    profiles_changed = Signal()

    def __init__(self, controls):
        super().__init__(controls)
        self.controls = controls
        self._profiles = []
        self._selected_options = None
        self._name_dialog = None
        self._status_source = ''
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        row = QHBoxLayout()
        row.addWidget(QLabel('Settings profile', self))
        self.combo = QComboBox(self)
        self.combo.setAccessibleName('Saved settings profiles')
        self.combo.setProperty('_i18n_user_text', True)
        self.combo.setMinimumContentsLength(16)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.combo, 1)
        self.save_button = QPushButton('Save profile…', self)
        self.delete_button = QPushButton('Delete profile', self)
        row.addWidget(self.save_button)
        row.addWidget(self.delete_button)
        layout.addLayout(row)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setProperty('_i18n_user_text', True)
        self.status.setObjectName('mutedLabel')
        self.status.hide()
        layout.addWidget(self.status)
        self.setToolTip('Save output codecs, quality, subtitles, cover artwork, chapters and naming. Profiles apply to newly queued downloads.')
        self.combo.activated.connect(self.apply_selected)
        self.combo.currentIndexChanged.connect(self._buttons)
        self.save_button.clicked.connect(self.prompt_save)
        self.delete_button.clicked.connect(self.delete_selected)
        controls.changed.connect(self.observe_changes)
        register(self, self._retranslate)

    def profiles(self):
        return deepcopy(self._profiles)

    def restore(self, rows):
        self._profiles = validate_profiles(rows)
        self._selected_options = None
        self._retranslate()

    def _records(self):
        return builtin_profiles() + self._profiles

    def _retranslate(self):
        key = self.combo.currentData()
        blocker = QSignalBlocker(self.combo)
        self.combo.clear()
        self.combo.addItem(tr('Custom settings'), '')
        for row in self._records():
            label = tr(row['name']) if row['id'].startswith('builtin:') else row['name']
            self.combo.addItem(label, row['id'])
        index = self.combo.findData(key)
        self.combo.setCurrentIndex(max(0, index))
        del blocker
        self._buttons()
        self.status.setText(tr(self._status_source))

    def _buttons(self):
        key = self.combo.currentData()
        self.delete_button.setEnabled(any(row['id'] == key for row in self._profiles))

    def _message(self, text):
        self._status_source = text
        self.status.setText(tr(text))
        self.status.setVisible(bool(text))

    def apply_selected(self, *_):
        key = self.combo.currentData()
        row = next((row for row in self._records() if row['id'] == key), None)
        if row is None:
            return
        self._selected_options = None
        try:
            self.controls.apply_profile(row['options'])
        except ValueError as error:
            blocker = QSignalBlocker(self.combo)
            self.combo.setCurrentIndex(0)
            del blocker
            self._selected_options = None
            self._buttons()
            self._message(str(error))
            return
        self._selected_options = capture_profile(self.controls.values())
        self._buttons()
        self._message('Profile applied. Newly queued downloads use these settings.')

    def observe_changes(self):
        if self._selected_options is not None:
            try:
                matches = capture_profile(self.controls.values()) == self._selected_options
            except ValueError:
                matches = False
            if not matches:
                self._selected_options = None
                blocker = QSignalBlocker(self.combo)
                self.combo.setCurrentIndex(0)
                del blocker
                self._buttons()
                self._message('Custom settings')

    def save_current(self, name):
        try:
            rows, key = save_profile(self._profiles, name, self.controls.values())
        except ValueError as error:
            self._message(str(error))
            return False
        self._profiles = rows
        self._retranslate()
        self.combo.setCurrentIndex(self.combo.findData(key))
        self._selected_options = capture_profile(self.controls.values())
        self._message('Profile saved.')
        self.profiles_changed.emit()
        return True

    def prompt_save(self):
        if self._name_dialog is not None:
            self._name_dialog.raise_()
            return
        dialog = QInputDialog(self)
        dialog.setWindowTitle(tr('Save profile'))
        dialog.setLabelText(tr('Profile name (an existing name updates that profile):'))
        key = self.combo.currentData()
        row = next((row for row in self._profiles if row['id'] == key), None)
        dialog.setTextValue(row['name'] if row else '')
        dialog.textValueSelected.connect(self.save_current)
        def finished(*_):
            self._name_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(finished)
        self._name_dialog = dialog
        dialog.open()

    def delete_selected(self):
        key = self.combo.currentData()
        rows = [row for row in self._profiles if row['id'] != key]
        if len(rows) == len(self._profiles):
            return
        self._profiles = rows
        self._selected_options = None
        self._retranslate()
        self._message('Profile deleted.')
        self.profiles_changed.emit()

    def persistence_failed(self):
        self._message('Profiles apply for this session, but could not be saved to disk.')
