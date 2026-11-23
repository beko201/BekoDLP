"""File/folder templates and a live example without touching the filesystem."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QVBoxLayout

from naming_templates import NAMING_DEFAULTS, render_naming
from ui.i18n import register, tr

PRESETS = (
    ('Title only', '{title}', ''),
    ('Channel / Title', '{title}', '{channel}'),
    ('Channel / Date - Title', '{upload_date} - {title}', '{channel}'),
    ('Playlist / Position - Title', '{playlist_index:03d} - {title}', '{playlist}'),
)


class NamingControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__('File and folder naming', parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.preset = QComboBox(self)
        self.preset.setAccessibleName('Naming preset')
        for index, (label, _, _) in enumerate(PRESETS):
            self.preset.addItem(label, index)
        self.preset.addItem('Custom', -1)
        self.filename = QLineEdit('{title}', self)
        self.filename.setAccessibleName('Filename template')
        self.filename.setMaxLength(512)
        self.subfolder = QLineEdit(self)
        self.subfolder.setAccessibleName('Subfolder template')
        self.subfolder.setMaxLength(512)
        self.subfolder.setPlaceholderText('Leave blank to use the download folder')
        form.addRow('Naming preset', self.preset)
        form.addRow('Filename template', self.filename)
        form.addRow('Subfolder template', self.subfolder)
        layout.addLayout(form)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setProperty('_i18n_user_text', True)
        layout.addWidget(self.hint)
        self.preview = QLabel(self)
        self.preview.setWordWrap(True)
        self.preview.setProperty('_i18n_user_text', True)
        layout.addWidget(self.preview)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self.filename.textChanged.connect(self._edited)
        self.subfolder.textChanged.connect(self._edited)
        register(self, self._preview)

    def values(self):
        return {'filename_template': self.filename.text().strip(), 'subfolder_template': self.subfolder.text().strip()}

    def restore(self, data):
        blockers = [QSignalBlocker(widget) for widget in (self.filename, self.subfolder, self.preset)]
        self.filename.setText(data.get('filename_template', NAMING_DEFAULTS['filename_template']))
        self.subfolder.setText(data.get('subfolder_template', ''))
        self._match_preset()
        del blockers
        self._preview()

    def _match_preset(self):
        values = self.values()
        found = next((index for index, (_, name, folder) in enumerate(PRESETS)
            if name == values['filename_template'] and folder == values['subfolder_template']), -1)
        blocker = QSignalBlocker(self.preset)
        self.preset.setCurrentIndex(self.preset.findData(found))
        del blocker

    def _preset_changed(self, *_):
        index = self.preset.currentData()
        if index == -1:
            return
        _, name, folder = PRESETS[index]
        blockers = [QSignalBlocker(self.filename), QSignalBlocker(self.subfolder)]
        self.filename.setText(name)
        self.subfolder.setText(folder)
        del blockers
        self._preview()
        self.changed.emit()

    def _edited(self, *_):
        self._match_preset()
        self._preview()
        self.changed.emit()

    def _preview(self):
        self.hint.setText(tr('Fields: {fields}. Use {index_example} for 001. The extension is added automatically. Folder templates may use / between folders.',
            fields='{title}, {channel}, {upload_date}, {playlist}, {playlist_index}, {id}', index_example='{playlist_index:03d}'))
        try:
            folders, name = render_naming(self.values(), {'title': 'Example video', 'channel': 'Example channel',
                'upload_date': '20261122', 'playlist_title': 'Example playlist', 'playlist_index': 3, 'id': 'abc123'})
            self.preview.setText(tr('Example: {path}', path='/'.join((*folders, name + '.mp4'))))
        except ValueError as error:
            self.preview.setText(tr(str(error)))
