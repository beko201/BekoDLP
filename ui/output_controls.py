"""Independent codec/container controls with explicit compatibility feedback."""
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLayout, QLineEdit, QSizePolicy, QSpinBox, QToolButton, QWidget)

from media_options import (AUDIO_BITRATES, AUTOMATIC_AUDIO_BITRATES, AUDIO_CODECS, AUDIO_CONTAINERS, CONTAINERS,
                           ENCODERS, VIDEO_CODECS, VIDEO_CONTAINERS, allowed_codecs, validate_output_options)
from settings import (BITRATES, CODECS, DEFAULTS, MAX_WORKERS, MAX_VIDEO_WORKERS, MAX_FFMPEG_THREADS,
                      MAX_FRAGMENT_DOWNLOADS, MAX_NETWORK_CONNECTIONS, QUALITIES)
from ui.i18n import register, tr, translate_combo
from ui.field_help import FieldHelpButton, FieldLabel

QUICK_AUDIO_CONTAINERS = {'original': 'auto', 'mp3': 'mp3', 'aac': 'm4a',
                          'opus': 'opus', 'flac': 'flac', 'lpcm': 'wav'}


class OutputControls(QGroupBox):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Download options", parent)
        self.encoders = None
        self.help_buttons = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        grid = QGridLayout(self)
        grid.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        self.advanced_toggle = QToolButton(self)
        self.advanced_toggle.setText('Advanced options')
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setAccessibleName('Advanced options')
        self.advanced_toggle.setToolTip('Codecs, conversion, trimming and download limits')
        self.advanced_widget = QWidget(self)
        advanced_grid = QGridLayout(self.advanced_widget)
        advanced_grid.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        advanced_grid.setContentsMargins(0, 8, 0, 0)
        for column in range(5):
            advanced_grid.setColumnStretch(column, 1)
        self.advanced_widget.hide()
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.format_combo = QComboBox(self)
        for label, data in (("Video", "MP4"), ("Audio", "MP3")):
            self.format_combo.addItem(label, data)
        self.quality_combo = QComboBox(self)
        self.quality_combo.addItems(QUALITIES)
        self.quality_combo.setCurrentText(DEFAULTS["quality"])
        self.audio_format_combo = QComboBox(self)
        self.audio_format_combo.setAccessibleName('Audio format')
        for codec in QUICK_AUDIO_CONTAINERS:
            self.audio_format_combo.addItem('WAV (LPCM)' if codec == 'lpcm' else AUDIO_CODECS[codec], codec)
        self.audio_format_combo.setToolTip('Choose an audio output format; codec and container are set together.')
        self.container_combo = QComboBox(self)
        self.codec_combo = QComboBox(self)
        for value, label in VIDEO_CODECS.items():
            self.codec_combo.addItem(label, value)
        self.audio_codec_combo = QComboBox(self)
        for value, label in AUDIO_CODECS.items():
            self.audio_codec_combo.addItem(label, value)
        self.video_quality_combo = QComboBox(self)
        for label, value in (("High quality", "high"), ("Balanced", "balanced"), ("Smaller file", "small")):
            self.video_quality_combo.addItem(label, value)
        self.video_quality_combo.setCurrentIndex(1)
        self.bitrate_combo = QComboBox(self)
        for value in AUDIO_BITRATES:
            self.bitrate_combo.addItem('Automatic' if value == 'auto' else value, value)
        self.bitrate_combo.setCurrentText(DEFAULTS["audio_bitrate"])
        self.bit_depth_combo = QComboBox(self)
        for label, value in (("Original", "original"), ("16-bit", "16"), ("24-bit", "24")):
            self.bit_depth_combo.addItem(label, value)
        self.bit_depth_combo.setToolTip("FLAC/LPCM only. Original keeps known source precision; unavailable precision uses 24-bit. Increasing depth does not restore lost quality.")
        self.workers_spin = QSpinBox(self)
        self.workers_spin.setRange(1, MAX_WORKERS)
        self.workers_spin.setValue(DEFAULTS["workers"])
        self.video_workers_spin = QSpinBox(self)
        self.video_workers_spin.setRange(1, MAX_VIDEO_WORKERS)
        self.video_workers_spin.setValue(DEFAULTS["video_workers"])
        self.video_workers_spin.setToolTip("Maximum videos encoded at once, within the parallel task limit. Active conversions finish when this limit is reduced.")
        self.ffmpeg_threads_spin = QSpinBox(self)
        self.ffmpeg_threads_spin.setRange(0, MAX_FFMPEG_THREADS)
        self.ffmpeg_threads_spin.setSpecialValueText("Automatic")
        self.ffmpeg_threads_spin.setToolTip("Threads per FFmpeg codec/filter pool. Automatic shares available CPU capacity across video conversions, up to 8 threads per pool. Running processes keep their current budget.")
        self.fragment_downloads_spin = QSpinBox(self)
        self.fragment_downloads_spin.setRange(1, MAX_FRAGMENT_DOWNLOADS)
        self.fragment_downloads_spin.setValue(DEFAULTS["fragment_downloads"])
        self.fragment_downloads_spin.setToolTip("Maximum parallel fragments for native DASH/HLS downloads. The shared media budget can lower this number. Applies to new download sessions.")
        self.network_connections_spin = QSpinBox(self)
        self.network_connections_spin.setRange(2, MAX_NETWORK_CONNECTIONS)
        self.network_connections_spin.setValue(DEFAULTS["network_connections"])
        self.network_connections_spin.setToolTip("Shared media-download capacity, including fragment workers and request overhead. Metadata and previews have separate limits. Existing downloads finish when the budget is reduced.")
        for column, (text, widget) in enumerate((("Mode", self.format_combo),
                                                 ("Resolution limit", self.quality_combo))):
            label = FieldLabel(text, self)
            if widget is self.quality_combo:
                self.selection_label = label
            label.setBuddy(widget)
            grid.addWidget(self._field_header(text, label, widget), 0, column)
            grid.addWidget(widget, 1, column)
        grid.addWidget(self.audio_format_combo, 1, 1)
        grid.addWidget(self.advanced_toggle, 1, 2, Qt.AlignmentFlag.AlignRight)
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName('mutedLabel')
        self.summary_label.setWordWrap(True)
        grid.addWidget(self.summary_label, 2, 0, 1, 3)
        grid.addWidget(self.advanced_widget, 3, 0, 1, 3)
        widgets = (("Container", self.container_combo), ("Video codec", self.codec_combo),
                   ("Audio codec", self.audio_codec_combo), ("Conversion quality", self.video_quality_combo),
                   ("Conversion bitrate", self.bitrate_combo),
                   ("Parallel downloads", self.workers_spin),
                   ("Parallel video conversions", self.video_workers_spin),
                   ("FFmpeg threads", self.ffmpeg_threads_spin))
        self.bitrate_combo.setToolTip("Automatic uses 192k for MP3/AAC or 128k for Opus when conversion is needed. "
                                      "Matching codecs are copied and keep the source bitrate.")
        self.video_quality_combo.setToolTip("Used only when video is converted. Matching codecs are copied without quality changes.")
        for index, (text, widget) in enumerate(widgets):
            row, column = divmod(index, 5)
            label = FieldLabel(text, self)
            label.setBuddy(widget)
            advanced_grid.addWidget(self._field_header(text, label, widget), row * 2, column)
            advanced_grid.addWidget(widget, row * 2 + 1, column)
        self.trim_start_edit = QLineEdit(self)
        self.trim_end_edit = QLineEdit(self)
        trim = QWidget(self)
        trim_layout = QHBoxLayout(trim)
        trim_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        trim_layout.setContentsMargins(0, 0, 0, 0)
        for text, widget, example in (("Trim start", self.trim_start_edit, "Start: 1:30"),
                                      ("Trim end", self.trim_end_edit, "End: 2:45")):
            widget.setPlaceholderText(example)
            widget.setAccessibleName(text)
            widget.setToolTip(text + " (e.g. 1:30). Leave blank for the full range.")
            trim_layout.addWidget(widget)
        trim_label = FieldLabel('Trim start / end', self)
        advanced_grid.addWidget(self._field_header('Trim start / end', trim_label, trim), 2, 3, 1, 2)
        advanced_grid.addWidget(trim, 3, 3, 1, 2)
        for column, (text, widget) in enumerate((("Parallel fragments", self.fragment_downloads_spin),
                                                ("Media download budget", self.network_connections_spin))):
            label = FieldLabel(text, self)
            label.setBuddy(widget)
            advanced_grid.addWidget(self._field_header(text, label, widget), 4, column)
            advanced_grid.addWidget(widget, 5, column)
        self.bit_depth_label = FieldLabel('Bit depth', self)
        self.bit_depth_label.setBuddy(self.bit_depth_combo)
        self.bit_depth_header = self._field_header('Bit depth', self.bit_depth_label, self.bit_depth_combo)
        advanced_grid.addWidget(self.bit_depth_header, 4, 2)
        advanced_grid.addWidget(self.bit_depth_combo, 5, 2)
        self.hint = QLabel(self)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("mutedLabel")
        advanced_grid.addWidget(self.hint, 6, 0, 1, 5)
        self.trim_start_edit.textChanged.connect(self._refresh_summary)
        self.trim_end_edit.textChanged.connect(self._refresh_summary)
        self._set_containers()
        self.format_combo.currentIndexChanged.connect(self._mode_changed)
        self.container_combo.currentIndexChanged.connect(self._container_changed)
        self.audio_format_combo.currentIndexChanged.connect(self._audio_format_selected)
        for combo in (self.codec_combo, self.audio_codec_combo,
                      self.bitrate_combo, self.bit_depth_combo, self.video_quality_combo, self.quality_combo):
            combo.currentIndexChanged.connect(self.refresh)
        self.refresh()
        register(self, self._retranslate)

    def _field_header(self, topic, label, field):
        header = QWidget(self)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        label.setWordWrap(True)
        layout.addWidget(label)
        button = FieldHelpButton(topic, field, header)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        self.help_buttons[topic] = button
        return header

    def _retranslate(self):
        blocker = QSignalBlocker(self)
        self.refresh()
        del blocker

    def _toggle_advanced(self, expanded):
        self.advanced_widget.setVisible(expanded)
        self.summary_label.setVisible(not expanded)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _refresh_summary(self):
        container = (tr('Automatic container') if self.container_combo.currentData() == 'auto' else
                     'Ogg Opus' if self.container_combo.currentData() == 'opus' else self.container_combo.currentText())
        video = self.mode() == 'video'
        codecs = ([self.codec_combo.currentText()] if video else []) + [self.audio_codec_combo.currentText()]
        text = container if not video and container == codecs[0] else container + ' · ' + ' / '.join(codecs)
        if video and self.codec_combo.currentData() != 'original':
            text += ' · ' + self.video_quality_combo.currentText()
        if self.audio_codec_combo.currentData() in ('mp3', 'aac', 'opus'):
            bitrate = self.bitrate_combo.currentText()
            if self.bitrate_combo.currentData() == 'auto':
                bitrate += f" ({AUTOMATIC_AUDIO_BITRATES[self.audio_codec_combo.currentData()]})"
            text += ' · ' + bitrate
        if self.audio_codec_combo.currentData() in ('flac', 'lpcm') and self.bit_depth_combo.currentData() != 'original':
            text += f' · {self.bit_depth_combo.currentText()}'
        start, end = self.trim_start_edit.text().strip(), self.trim_end_edit.text().strip()
        if start or end:
            text += ' · ' + tr('Trim: {start} – {end}', start=start or '0', end=end or tr('end'))
        self.summary_label.setText(text)

    def _audio_format_selected(self):
        codec = self.audio_format_combo.currentData()
        if self.mode() != 'audio' or codec not in QUICK_AUDIO_CONTAINERS:
            return
        blockers = [QSignalBlocker(widget) for widget in (self.container_combo, self.audio_codec_combo)]
        self.container_combo.setCurrentIndex(self.container_combo.findData(QUICK_AUDIO_CONTAINERS[codec]))
        self.audio_codec_combo.setCurrentIndex(self.audio_codec_combo.findData(codec))
        del blockers
        self._container_changed()

    def _sync_audio_format(self):
        blocker = QSignalBlocker(self.audio_format_combo)
        codec, container = self.audio_codec_combo.currentData(), self.container_combo.currentData()
        standard = codec in QUICK_AUDIO_CONTAINERS and (container == QUICK_AUDIO_CONTAINERS[codec]
                                                       or (codec != 'original' and container == 'auto'))
        custom = self.audio_format_combo.findData(None)
        if standard:
            if custom >= 0:
                self.audio_format_combo.removeItem(custom)
            self.audio_format_combo.setCurrentIndex(self.audio_format_combo.findData(codec))
        else:
            if custom < 0:
                self.audio_format_combo.addItem('Custom (advanced)', None)
                custom = self.audio_format_combo.count() - 1
                self.audio_format_combo.model().item(custom).setEnabled(False)
            self.audio_format_combo.setCurrentIndex(custom)
        for index in range(self.audio_format_combo.count()):
            value = self.audio_format_combo.itemData(index)
            if value is None:
                continue
            available = value not in ENCODERS or self.encoders is None or any(
                encoder in self.encoders for encoder in ENCODERS[value])
            item = self.audio_format_combo.model().item(index)
            item.setEnabled(available)
            item.setToolTip('No encoder is available in this FFmpeg installation.' if not available else '')
        del blocker

    def mode(self):
        return {"MP4": "video", "MP3": "audio"}[self.format_combo.currentData()]

    def _set_containers(self):
        previous = self.container_combo.currentData()
        blocker = QSignalBlocker(self.container_combo)
        self.container_combo.clear()
        for key in AUDIO_CONTAINERS if self.mode() == "audio" else VIDEO_CONTAINERS:
            self.container_combo.addItem(CONTAINERS[key], key)
        self.container_combo.setCurrentIndex(max(0, self.container_combo.findData(previous)))
        del blocker

    def _mode_changed(self):
        self._set_containers()
        self._container_changed()

    def _container_changed(self):
        allowed_video, allowed_audio = allowed_codecs(self.mode(), self.container_combo.currentData())
        reset = []
        for combo, allowed, label in ((self.codec_combo, allowed_video, "Video codec"),
                                      (self.audio_codec_combo, allowed_audio, "Audio codec")):
            if combo.currentData() not in allowed:
                reset.append(f"{tr(label)} ({combo.currentText()})")
                blocker = QSignalBlocker(combo)
                combo.setCurrentIndex(combo.findData("original"))
                del blocker
        self.refresh()
        if reset:
            self.hint.setText(tr('{choices} reset to Original to match the selected output profile. ', choices='; '.join(reset))
                              + self.hint.text())

    def values(self):
        mode = self.mode()
        return dict(output_mode=mode, format="MP3" if mode == "audio" else "MP4",
                    container=self.container_combo.currentData(),
                    video_codec=self.codec_combo.currentData() if mode == "video" else "original",
                    audio_codec=self.audio_codec_combo.currentData(),
                    audio_bit_depth=self.bit_depth_combo.currentData(),
                    video_quality=self.video_quality_combo.currentData(), audio_bitrate=self.bitrate_combo.currentData(),
                    quality=self.quality_combo.currentText(), codec_mode=CODECS[0],
                    mp3_bitrate=self.bitrate_combo.currentText() if self.bitrate_combo.currentText() in BITRATES else DEFAULTS["mp3_bitrate"])

    def restore(self, data):
        combos = (self.format_combo, self.container_combo, self.codec_combo, self.audio_codec_combo,
                  self.video_quality_combo, self.bitrate_combo, self.bit_depth_combo, self.quality_combo, self.workers_spin,
                  self.video_workers_spin, self.ffmpeg_threads_spin)
        combos += (self.fragment_downloads_spin, self.network_connections_spin)
        blockers = [QSignalBlocker(widget) for widget in combos]
        self.format_combo.setCurrentIndex(self.format_combo.findData(
            {"video": "MP4", "audio": "MP3"}[data["output_mode"]]))
        self._set_containers()
        for combo, key in ((self.container_combo, "container"), (self.codec_combo, "video_codec"),
                           (self.audio_codec_combo, "audio_codec"), (self.video_quality_combo, "video_quality"),
                           (self.bit_depth_combo, "audio_bit_depth")):
            combo.setCurrentIndex(combo.findData(data[key]))
        self.bitrate_combo.setCurrentIndex(self.bitrate_combo.findData(data["audio_bitrate"]))
        self.quality_combo.setCurrentText(data["quality"])
        self.workers_spin.setValue(data["workers"])
        self.video_workers_spin.setValue(data.get("video_workers", DEFAULTS["video_workers"]))
        self.ffmpeg_threads_spin.setValue(data.get("ffmpeg_threads", DEFAULTS["ffmpeg_threads"]))
        self.fragment_downloads_spin.setValue(data.get("fragment_downloads", DEFAULTS["fragment_downloads"]))
        self.network_connections_spin.setValue(data.get("network_connections", DEFAULTS["network_connections"]))
        del blockers
        self.refresh()

    def set_encoders(self, encoders):
        self.encoders = set(encoders)
        blocker = QSignalBlocker(self)
        self.refresh()
        del blocker

    def refresh(self):
        mode = self.mode()
        video = mode != "audio"
        self.quality_combo.setEnabled(video)
        self.quality_combo.setVisible(video)
        self.audio_format_combo.setVisible(not video)
        self.selection_label.setText(tr('Resolution limit' if video else 'Audio format'))
        self.selection_label.setBuddy(self.quality_combo if video else self.audio_format_combo)
        self.help_buttons['Resolution limit'].set_topic(
            'Resolution limit' if video else 'Audio format', self.quality_combo if video else self.audio_format_combo)
        if not video:
            self._sync_audio_format()
        self.container_combo.setEnabled(True)
        self.codec_combo.setEnabled(mode == "video")
        self.audio_codec_combo.setEnabled(True)
        self.video_quality_combo.setEnabled(mode == "video" and self.codec_combo.currentData() != "original")
        self.bitrate_combo.setEnabled(self.audio_codec_combo.currentData() in ("mp3", "aac", "opus"))
        lossless = self.audio_codec_combo.currentData() in ("flac", "lpcm")
        self.bit_depth_combo.setVisible(lossless)
        self.bit_depth_label.setVisible(lossless)
        self.bit_depth_header.setVisible(lossless)
        allowed_video, allowed_audio = allowed_codecs(mode, self.container_combo.currentData())
        for combo, allowed in ((self.codec_combo, allowed_video), (self.audio_codec_combo, allowed_audio)):
            for index in range(combo.count()):
                value = combo.itemData(index)
                available = value not in ENCODERS or self.encoders is None or any(
                    encoder in self.encoders for encoder in ENCODERS[value])
                item = combo.model().item(index)
                item.setEnabled(value in allowed and available)
                item.setToolTip(tr("No encoder is available in this FFmpeg installation." if not available else
                                "Not supported by the selected output profile." if value not in allowed else ""))
        for combo in (self.format_combo, self.audio_format_combo, self.container_combo, self.codec_combo,
                      self.audio_codec_combo, self.video_quality_combo, self.bitrate_combo, self.bit_depth_combo):
            translate_combo(combo)
        try:
            validate_output_options(self.values())
        except ValueError as error:
            self.hint.setText(tr(str(error)))
        else:
            if lossless:
                text = "Lossless output does not restore quality lost in the source. Original preserves known bit depth; otherwise 24-bit is used."
            elif ((self.codec_combo.currentData() != "original" and mode == "video")
                  or self.audio_codec_combo.currentData() not in ("original", "none")):
                text = "Matching codecs are copied. Quality and bitrate apply only when conversion is needed (slower, potentially lossy)."
            else:
                text = "Original streams are copied. An incompatible container requires another container or explicit codec conversion."
            self.hint.setText(tr(text))
        self._refresh_summary()
        self.changed.emit()
