"""Inspect one video's formats without blocking Qt, then queue exact source IDs."""
from dataclasses import asdict, replace
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from cookie_options import cookie_kwargs, cookie_error_guidance
from network_options import network_kwargs
from download_engine import fetch_ytinfo_with_retry
from source_formats import downloadable, format_size, selected_sources
from ui.i18n import register, tr
from errors import DownloadCancelled
from app_logging import log_warning
from url_utils import normalize_url


HEADERS = ('Format ID', 'Stream type', 'Container', 'Resolution', 'FPS', 'Video codec', 'Audio codec',
           'Bitrate (kb/s)', 'Size', 'Language', 'HDR', 'Audio channels', 'Sample rate (Hz)', 'Protocol', 'Notes', 'Availability')


def kind(fmt):
    video = fmt.get('vcodec') != 'none'
    audio = fmt.get('acodec') != 'none'
    if video and audio and (not fmt.get('vcodec') or not fmt.get('acodec')):
        return 'Unknown streams'
    return 'Video + audio' if video and audio else 'Video only' if video else 'Audio only' if audio else 'Other'


def describe(fmt):
    return ' · '.join(str(value) for value in (fmt.get('format_id'), fmt.get('resolution') or
        (str(fmt['height']) + 'p' if fmt.get('height') else ''),
        (str(fmt['fps']) + ' FPS') if fmt.get('fps') else '',
        fmt.get('vcodec') if fmt.get('vcodec') != 'none' else '',
        fmt.get('acodec') if fmt.get('acodec') != 'none' else '', fmt.get('language'),
        (str(fmt['abr']) + ' kb/s') if fmt.get('abr') else '') if value)


class SourceFormatsDialog(QDialog):
    _loaded = Signal(int, object, str)
    queued = Signal(str)

    def __init__(self, manager, url, options, parent=None):
        super().__init__(parent)
        self.manager, self.url, self.options = manager, normalize_url(url), options
        self.info = None
        self.formats = []
        self._generation = 0
        self._cancel = threading.Event()
        self._closed = False
        self.setWindowTitle('Source formats')
        self.resize(1120, 660)
        self.setMinimumSize(700, 460)
        layout = QVBoxLayout(self)
        self.title_label = QLabel(self.url, self)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setProperty('_i18n_user_text', True)
        layout.addWidget(self.title_label)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.profile_label = QLabel(self)
        self.profile_label.setWordWrap(True)
        layout.addWidget(self.profile_label)
        row = QHBoxLayout()
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText('Filter formats…')
        self.filter_edit.setAccessibleName('Filter formats')
        row.addWidget(self.filter_edit, 1)
        self.refresh_button = QPushButton('Refresh formats', self)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.table = QTableWidget(0, len(HEADERS), self)
        self.table.setAccessibleName('Available source formats')
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        layout.addWidget(self.table, 1)
        form = QFormLayout()
        self.video_combo = QComboBox(self)
        self.audio_combo = QComboBox(self)
        for combo, label in ((self.video_combo, 'Video source'), (self.audio_combo, 'Audio source')):
            combo.setAccessibleName(label)
            combo.setMinimumContentsLength(20)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            form.addRow(label, combo)
        layout.addLayout(form)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setProperty('_i18n_user_text', True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self.add_button = buttons.addButton('Queue selected formats', QDialogButtonBox.ButtonRole.ActionRole)
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.enqueue)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._loaded.connect(self._receive)
        self.refresh_button.clicked.connect(self.load)
        self.filter_edit.textChanged.connect(self._filter)
        self.video_combo.currentIndexChanged.connect(self._video_changed)
        self.audio_combo.currentIndexChanged.connect(self._validate)
        self.table.cellClicked.connect(self._pick_row)
        self.finished.connect(self._finish)
        register(self, self._retranslate)

    def _retranslate(self):
        self.table.setHorizontalHeaderLabels([tr(header) for header in HEADERS])
        for index, fmt in enumerate(self.formats):
            self.table.item(index, 1).setText(tr(kind(fmt)))
            self.table.item(index, 15).setText(tr('DRM' if fmt.get('has_drm') else
                'Available' if downloadable(fmt) else 'Unavailable'))
        if not self.audio_combo.isEnabled() and self.audio_combo.count():
            self.audio_combo.setItemText(0, self._included_audio_label())
        self.hint.setText(tr('Choose the source streams to download. Output codec, container and trim settings still apply. Manual selection overrides the resolution limit.'))
        self.profile_label.setText(tr('Output: {mode} · {container} · {video} / {audio}',
            mode=tr('Audio' if self.options.output_mode == 'audio' else 'Video'),
            container=tr('Automatic') if self.options.container == 'auto' else self.options.container.upper(),
            video=tr('Original') if self.options.video_codec == 'original' else self.options.video_codec,
            audio=tr('Original') if self.options.audio_codec == 'original' else self.options.audio_codec))

    def load(self):
        if self._closed:
            return
        self._cancel.set()
        self._cancel = threading.Event()
        self._generation += 1
        generation, cancel = self._generation, self._cancel
        self.info = None
        self.formats = []
        self.table.setRowCount(0)
        self.video_combo.clear()
        self.audio_combo.clear()
        self.add_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status.setText(tr('Loading source formats…'))
        def fetch():
            info, error = None, ''
            try:
                info = fetch_ytinfo_with_retry(self.url, max_retries=1, cancel_event=cancel,
                    stop_event=self.manager._shutdown_event, **cookie_kwargs(asdict(self.options)),
                    **network_kwargs(asdict(self.options)))
            except DownloadCancelled:
                return
            except Exception as exception:
                log_warning('Could not inspect source formats', exception)
                error = str(exception)
            if not cancel.is_set() and not self.manager._shutdown_event.is_set():
                try:
                    self._loaded.emit(generation, info, error)
                except RuntimeError:
                    pass  # The parent may have been deleted during cancellation.
        if self.manager.start_thread(threading.Thread(target=fetch, name='source-formats')) is None:
            self.refresh_button.setEnabled(True)

    def _receive(self, generation, info, error):
        if self._closed or generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        if error or info is None:
            hint = cookie_error_guidance(error)
            self.status.setText(tr('Could not load source formats.') + ('\n' + tr(hint) if hint else '')
                                + ('\n' + error if error else ''))
            return
        metadata = info._media_info.info
        if metadata.get('_type', 'video') != 'video' or not metadata.get('id'):
            self.status.setText(tr('Enter a single video URL to inspect its formats.'))
            return
        self.info = info
        self.formats = metadata.get('formats') or []
        self.title_label.setText(info.title)
        self.table.setRowCount(len(self.formats))
        self.video_combo.blockSignals(True)
        self.video_combo.clear()
        for index, fmt in enumerate(self.formats):
            values = (fmt.get('format_id'), tr(kind(fmt)), fmt.get('ext'), fmt.get('resolution') or
                (f"{fmt.get('width') or '?'}×{fmt['height']}" if fmt.get('height') else ''),
                fmt.get('fps'), fmt.get('vcodec'), fmt.get('acodec'), fmt.get('tbr') or fmt.get('abr') or fmt.get('vbr'), format_size(fmt, info.duration),
                fmt.get('language'), fmt.get('dynamic_range'), fmt.get('audio_channels'), fmt.get('asr'),
                fmt.get('protocol'), fmt.get('format_note'), tr('DRM' if fmt.get('has_drm') else
                'Available' if downloadable(fmt) else 'Unavailable'))
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value) if value not in (None, '') else '—'))
            if downloadable(fmt) and kind(fmt) in ('Video only', 'Video + audio', 'Unknown streams'):
                self.video_combo.addItem(describe(fmt), fmt['format_id'])
        self.video_combo.blockSignals(False)
        self.video_combo.setEnabled(self.options.output_mode == 'video')
        if self.video_combo.count():
            self.video_combo.setCurrentIndex(self.video_combo.count() - 1)
        self.table.resizeColumnsToContents()
        self._video_changed()
        self._filter()

    def _video_changed(self, *_):
        previous = self.audio_combo.currentData()
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        video = next((fmt for fmt in self.formats if fmt.get('format_id') == self.video_combo.currentData()), {})
        audio_output = self.options.output_mode == 'audio'
        choose_audio = audio_output or (self.options.audio_codec != 'none' and kind(video) == 'Video only')
        if choose_audio:
            for fmt in self.formats:
                if downloadable(fmt) and kind(fmt) in (('Audio only', 'Video + audio', 'Unknown streams') if audio_output else ('Audio only',)):
                    self.audio_combo.addItem(describe(fmt), fmt['format_id'])
            index = self.audio_combo.findData(previous)
            self.audio_combo.setCurrentIndex(index if index >= 0 else self.audio_combo.count() - 1)
        else:
            self.audio_combo.addItem(self._included_audio_label(), '')
        self.audio_combo.setEnabled(choose_audio)
        self.audio_combo.blockSignals(False)
        self._validate()

    def _included_audio_label(self):
        video = next((fmt for fmt in self.formats if fmt.get('format_id') == self.video_combo.currentData()), {})
        return tr('No audio' if self.options.audio_codec == 'none' else
            'Audio checked after download' if kind(video) == 'Unknown streams' else 'Included audio')

    def chosen_options(self):
        return replace(self.options, source_selection='manual',
            source_video_id=self.video_combo.currentData() or '' if self.options.output_mode == 'video' else '',
            source_audio_id=self.audio_combo.currentData() or '')

    def _validate(self, *_):
        self.add_button.setEnabled(False)
        if self.info is None:
            return
        try:
            selected_sources(self.info, asdict(self.chosen_options()))
        except (ValueError, RuntimeError) as error:
            self.status.setText(tr(str(error)))
            return
        self.status.setText(tr('Source selection ready. Sizes marked ≈ are estimates.'))
        self.add_button.setEnabled(True)

    def _filter(self, *_):
        text = self.filter_edit.text().casefold().strip()
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, bool(text) and not any(text in self.table.item(row, column).text().casefold()
                for column in range(self.table.columnCount())))

    def _pick_row(self, row, _column):
        fmt = self.formats[row]
        if not downloadable(fmt):
            self.add_button.setEnabled(False)
            self.status.setText(tr('This format cannot be downloaded.'))
            return
        combo = self.audio_combo if self.options.output_mode == 'audio' or kind(fmt) == 'Audio only' else self.video_combo
        if combo.isEnabled():
            index = combo.findData(fmt.get('format_id'))
            if index >= 0:
                combo.setCurrentIndex(index)
                self._validate()
                return
        self.add_button.setEnabled(False)
        self.status.setText(tr('Use the source selectors below to choose compatible video and audio streams.'))

    def enqueue(self):
        if self._closed or self.info is None or not self.add_button.isEnabled():
            return
        options = self.chosen_options()
        selected_sources(self.info, asdict(options))
        added = self.manager.add_urls([self.url], options, resolved_info={self.url: self.info._media_info.info})
        if added:
            self.queued.emit(self.url)
            self.accept()
        else:
            self.status.setText(tr('This URL is already queued or active.'))

    def _finish(self, *_):
        self._closed = True
        self._cancel.set()
        self.info = None
        self.formats = []
