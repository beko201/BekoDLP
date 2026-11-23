"""Checkable playlist preview with original-position range selection."""
from dataclasses import asdict
import threading

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QSpinBox, QTableView, QVBoxLayout)

from playlist_selection import PreviewCancellation, load_playlist_preview
from resolved_info_cache import ResolvedInfoCache
from naming_templates import uses_playlist_naming
from ui.i18n import register, tr
from utils import DownloadCancelled


class PlaylistModel(QAbstractTableModel):
    selection_changed = Signal()
    HEADERS = ('Playlist position', 'Video title', 'Duration', 'Availability')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.checked = set()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < 4:
            return tr(self.HEADERS[section])
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if index.row() in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            return (str(entry.number), entry.title, entry.duration, tr(entry.status))[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.url or tr(entry.status)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if index.column() == 0 and self.entries[index.row()].available:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if (not index.isValid() or role != Qt.ItemDataRole.CheckStateRole or index.column() != 0
                or not self.entries[index.row()].available):
            return False
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.checked.add(index.row())
        else:
            self.checked.discard(index.row())
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()
        return True

    def clear(self):
        self.beginResetModel()
        self.entries.clear()
        self.checked.clear()
        self.endResetModel()
        self.selection_changed.emit()

    def append(self, entries):
        if not entries:
            return
        first = len(self.entries)
        self.beginInsertRows(QModelIndex(), first, first + len(entries) - 1)
        self.entries.extend(entries)
        self.checked.update(first + index for index, entry in enumerate(entries) if entry.available)
        self.endInsertRows()
        self.selection_changed.emit()

    def select(self, first=None, last=None):
        self.checked = {index for index, entry in enumerate(self.entries) if entry.available and
                        (first is None or first <= entry.number <= last)} if first != 0 else set()
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()

    def selected_urls(self):
        return [self.entries[index].url for index in sorted(self.checked) if self.entries[index].available]


class PlaylistSelectionDialog(QDialog):
    _batch = Signal(int, object)
    _loaded = Signal(int, str, str)
    queued = Signal(str, int, int)

    def __init__(self, manager, url, options, parent=None):
        super().__init__(parent)
        self.manager, self.url, self.options = manager, url, options
        self._generation = 0
        self._cancel = threading.Event()
        self._closed = False
        self.loading = False
        self.playlist_title = ''
        self.preview_info = ResolvedInfoCache()
        self.setWindowTitle('Choose playlist videos')
        self.resize(920, 620)
        self.setMinimumSize(680, 440)
        layout = QVBoxLayout(self)
        self.title_label = QLabel(url, self)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setProperty('_i18n_user_text', True)
        layout.addWidget(self.title_label)
        hint = QLabel('No downloads start until you queue the selected videos. Output settings are captured when this window opens.', self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.all_button = QPushButton('Select all', self)
        self.none_button = QPushButton('Select none', self)
        self.first_spin, self.last_spin = QSpinBox(self), QSpinBox(self)
        for spin, name in ((self.first_spin, 'First playlist position'), (self.last_spin, 'Last playlist position')):
            spin.setRange(1, 2147483647)
            spin.setAccessibleName(name)
        self.range_button = QPushButton('Select range', self)
        self.refresh_button = QPushButton('Reload playlist', self)
        for widget in (self.all_button, self.none_button, QLabel('From', self), self.first_spin,
                       QLabel('To', self), self.last_spin, self.range_button):
            row.addWidget(widget)
        row.addStretch()
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.model = PlaylistModel(self)
        self.table = QTableView(self)
        self.table.setAccessibleName('Playlist videos')
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 130)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)
        self.warning = QLabel(self)
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.setProperty('_i18n_user_text', True)
        layout.addWidget(self.warning)
        self.count_label = QLabel(self)
        self.count_label.setProperty('_i18n_user_text', True)
        layout.addWidget(self.count_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.add_button = buttons.addButton('Queue selected videos', QDialogButtonBox.ButtonRole.ActionRole)
        self.add_button.clicked.connect(self.enqueue)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.model.selection_changed.connect(self._selection_changed)
        self.all_button.clicked.connect(lambda: self.model.select())
        self.none_button.clicked.connect(lambda: self.model.select(0, 0))
        self.range_button.clicked.connect(self.select_range)
        self.first_spin.valueChanged.connect(self.last_spin.setMinimum)
        self.refresh_button.clicked.connect(self.load)
        self._batch.connect(self._receive_batch)
        self._loaded.connect(self._receive_finished)
        self.finished.connect(self._finish)
        self._selection_changed()
        register(self, self._retranslate)

    def _retranslate(self):
        self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 3)
        if self.model.entries:
            self.model.dataChanged.emit(self.model.index(0, 3), self.model.index(len(self.model.entries) - 1, 3))
        self._selection_changed()

    def _selection_changed(self):
        self.count_label.setText(tr('{selected} of {total} videos selected.',
            selected=len(self.model.checked), total=len(self.model.entries)))
        self.add_button.setEnabled(bool(self.model.checked) and not self.loading and not self._closed)

    def _set_loading(self, loading):
        self.loading = loading
        for widget in (self.table, self.all_button, self.none_button, self.first_spin, self.last_spin,
                       self.range_button, self.refresh_button):
            widget.setEnabled(not loading)
        self._selection_changed()

    def load(self):
        if self._closed:
            return
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        generation = self._generation
        self.preview_info = ResolvedInfoCache()
        preview_info = self.preview_info
        cancel = PreviewCancellation(self._cancel, self.manager._shutdown_event)
        self._set_loading(True)
        self.model.clear()
        self.playlist_title = ''
        self.warning.setText(tr('Loading playlist videos…'))
        def fetch():
            def deliver(batch):
                if not cancel.is_set():
                    try:
                        self._batch.emit(generation, batch)
                    except RuntimeError:
                        pass
            try:
                title, warning = load_playlist_preview(self.url, asdict(self.options), cancel, deliver,
                                                       resolved_info=preview_info)
            except DownloadCancelled:
                preview_info.clear()
                return
            except Exception as error:
                title, warning = '', str(error)
            try:
                if not cancel.is_set():
                    self._loaded.emit(generation, title, warning)
                else:
                    preview_info.clear()
            except RuntimeError:
                pass  # Parent deletion during cancellation.
        if self.manager.start_thread(threading.Thread(target=fetch, name='playlist-preview')) is None:
            self._set_loading(False)

    def _receive_batch(self, generation, entries):
        if not self._closed and generation == self._generation:
            self.model.append(entries)

    def _receive_finished(self, generation, title, warning):
        if self._closed or generation != self._generation:
            return
        self._set_loading(False)
        if title:
            self.title_label.setText(title)
            self.playlist_title = title
        parts = warning.split('\n', 1)
        self.warning.setText(tr(parts[0]) + ('\n' + parts[1] if len(parts) > 1 else ''))
        if not self.model.checked and not warning:
            self.warning.setText(tr('No downloadable playlist videos were found.'))
        if self.model.entries:
            numbers = [entry.number for entry in self.model.entries]
            self.first_spin.setValue(min(numbers))
            self.last_spin.setValue(max(numbers))

    def select_range(self):
        if not self.loading:
            self.model.select(self.first_spin.value(), self.last_spin.value())

    def enqueue(self):
        if self.loading or self._closed or self.manager._shutdown_event.is_set():
            return
        urls = self.model.selected_urls()
        if not urls:
            return
        resolved = {url: info for url in set(urls) if (info := self.preview_info.pop(url, None)) is not None}
        contexts = {}
        if uses_playlist_naming(asdict(self.options)):
            for index in sorted(self.model.checked):
                entry = self.model.entries[index]
                contexts.setdefault(entry.url, {'playlist_title': entry.playlist_title or self.playlist_title,
                                               'playlist_index': str(entry.number)})
        added = self.manager.add_urls(urls, self.options, resolved_info=resolved, notify=False,
                                      **({'entry_context': contexts} if contexts else {}))
        if added:
            self.queued.emit(self.url, len(added), len(urls) - len(added))
            self.accept()
        else:
            self.warning.setText(tr('The selected videos are already queued or active.'))

    def _finish(self, *_):
        self._closed = True
        self._cancel.set()
        if not self.loading:
            self.preview_info.clear()
