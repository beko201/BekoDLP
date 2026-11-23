"""Subtitle output, language and automatic-caption controls."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QGridLayout, QGroupBox, QLabel, QLineEdit

from subtitle_options import subtitle_selection
from ui.i18n import register, tr


class SubtitleControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('Subtitles', parent)
        self.video_supported = True
        layout = QGridLayout(self)
        self.mode_combo = QComboBox(self)
        self.mode_combo.setAccessibleName('Subtitle output')
        for label, value in (('No subtitles', 'none'), ('Separate files', 'files'),
                             ('Embed in video', 'embed'), ('Embed and save files', 'both')):
            self.mode_combo.addItem(label, value)
        self.languages_edit = QLineEdit('tr,en', self)
        self.languages_edit.setAccessibleName('Subtitle languages')
        self.languages_edit.setPlaceholderText('tr,en or en.* or all')
        self.format_combo = QComboBox(self)
        self.format_combo.setAccessibleName('Subtitle format')
        self.format_combo.addItem('SRT', 'srt')
        self.format_combo.addItem('WebVTT', 'vtt')
        self.auto_check = QCheckBox('Include automatic subtitles', self)
        for row, (label, widget) in enumerate((('Subtitle output', self.mode_combo),
                                              ('Subtitle languages', self.languages_edit),
                                              ('Subtitle format', self.format_combo))):
            title = QLabel(label, self)
            title.setBuddy(widget)
            layout.addWidget(title, row, 0)
            layout.addWidget(widget, row, 1)
        layout.addWidget(self.auto_check, 3, 0, 1, 2)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setObjectName('mutedLabel')
        layout.addWidget(self.hint, 4, 0, 1, 2)
        layout.setColumnStretch(1, 1)
        self.mode_combo.currentIndexChanged.connect(self._changed)
        for signal in (self.languages_edit.textChanged, self.format_combo.currentIndexChanged, self.auto_check.toggled):
            signal.connect(lambda *_: self.changed.emit())
        self._refresh()
        register(self, self._refresh)

    def _changed(self):
        self._refresh()
        self.changed.emit()

    def _refresh(self):
        enabled = self.mode_combo.currentData() != 'none'
        for widget in (self.languages_edit, self.format_combo, self.auto_check):
            widget.setEnabled(enabled)
        self.hint.setText(tr('Embedding supports MP4, MKV and WebM. Missing languages are reported as warnings.'
                            if self.video_supported else 'Audio downloads save subtitles as separate files.'))

    def set_video_supported(self, supported):
        self.video_supported = supported
        for value in ('embed', 'both'):
            self.mode_combo.model().item(self.mode_combo.findData(value)).setEnabled(supported)
        if not supported and self.mode_combo.currentData() in ('embed', 'both'):
            self.mode_combo.setCurrentIndex(self.mode_combo.findData('files'))
        self._refresh()

    def values(self):
        return dict(subtitle_mode=self.mode_combo.currentData(), subtitle_languages=self.languages_edit.text().strip(),
                    subtitle_auto='on' if self.auto_check.isChecked() else 'off', subtitle_format=self.format_combo.currentData())

    def restore(self, data):
        selected = subtitle_selection(data)
        blockers = [QSignalBlocker(widget) for widget in
                    (self, self.mode_combo, self.languages_edit, self.format_combo, self.auto_check)]
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(selected['subtitle_mode']))
        self.languages_edit.setText(selected['subtitle_languages'])
        self.format_combo.setCurrentIndex(self.format_combo.findData(selected['subtitle_format']))
        self.auto_check.setChecked(selected['subtitle_auto'] == 'on')
        self._refresh()
        del blockers
