"""Main-window widget layout and menus; application flows live in controllers."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar,
    QPushButton, QSplitter, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from app_info import APP_NAME, APP_VERSION
from ui.completion_actions import ACTION_LABELS
from ui.options_scroll import OptionsScrollArea
from ui.output_controls import OutputControls
from ui.preview_panel import PreviewPanel
from ui.task_table import TaskTable


def build_window(window):
    central = QWidget(window)
    window.setCentralWidget(central)
    layout = QVBoxLayout(central)
    layout.setContentsMargins(20, 16, 20, 12)
    layout.setSpacing(12)

    heading = QLabel(APP_NAME, window)
    heading.setObjectName("headingLabel")
    layout.addWidget(heading)
    note = QLabel("An independent video and audio downloader for YouTube and other sites supported by yt-dlp.", window)
    note.setObjectName("mutedLabel")
    note.setWordWrap(True)
    layout.addWidget(note)

    link_row = QHBoxLayout()
    window.url_edit = QLineEdit(window)
    window.url_edit.setPlaceholderText("Paste a video URL…")
    window.url_edit.setAccessibleName("Video URL")
    window.enqueue_button = QPushButton("Add to queue", window)
    window.enqueue_button.setObjectName("primaryButton")
    window.bulk_button = QPushButton("Add multiple links", window)
    window.bulk_button.clicked.connect(window.open_bulk_dialog)
    window.source_button = QPushButton('Source formats…', window)
    window.source_button.clicked.connect(window.open_source_formats)
    link_row.addWidget(window.url_edit, 1)
    link_row.addWidget(window.enqueue_button)
    link_row.addWidget(window.bulk_button)
    link_row.addWidget(window.source_button)
    layout.addLayout(link_row)

    window.options_scroll = OptionsScrollArea(build_output_controls(window), central)
    layout.addWidget(window.options_scroll)
    folder_row = QHBoxLayout()
    window.folder_edit = QLineEdit(window)
    window.folder_edit.setReadOnly(True)
    window.folder_edit.setPlaceholderText("No download folder selected")
    window.folder_edit.setAccessibleName("Download folder")
    window.folder_button = QPushButton("Choose folder", window)
    window.folder_button.clicked.connect(window.choose_folder)
    folder_row.addWidget(window.folder_edit, 1)
    folder_row.addWidget(window.folder_button)
    layout.addLayout(folder_row)

    actions = QHBoxLayout()
    window.pause_button = QPushButton("Pause", window)
    window.cancel_action = QAction("Cancel selected", window)
    window.retry_action = QAction("Retry failed tasks", window)
    window.clear_button = QPushButton("Clear list…", window)
    window.show_file_action = QAction("Show in folder", window)
    window.diagnostics_action = QAction('View diagnostics', window)
    window.task_actions_button = QToolButton(window)
    window.task_actions_button.setText('Task actions')
    window.task_actions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    window.task_actions_menu = QMenu(window.task_actions_button)
    window.task_actions_menu.addActions([window.cancel_action, window.retry_action])
    window.task_actions_menu.addSeparator()
    window.task_actions_menu.addActions([window.show_file_action, window.diagnostics_action])
    window.task_actions_button.setMenu(window.task_actions_menu)
    window.task_actions_menu.aboutToShow.connect(window._refresh_actions)
    for button in (window.pause_button, window.clear_button, window.task_actions_button):
        actions.addWidget(button)
    actions.addStretch()
    window.completion_combo = QComboBox(window)
    window.completion_combo.setAccessibleName('When queue finishes')
    for value, label in ACTION_LABELS.items():
        window.completion_combo.addItem(label, value)
    window.completion_combo.setToolTip('Runs only after all new tasks succeed. Power actions have a 30-second cancel countdown. Resets each session.')
    actions.addWidget(QLabel('When queue finishes', window))
    actions.addWidget(window.completion_combo)
    layout.addLayout(actions)

    window.splitter = QSplitter(Qt.Orientation.Horizontal, window)
    window.task_table = TaskTable(window.splitter)
    window.preview_panel = PreviewPanel(window.splitter)
    window.splitter.addWidget(window.task_table)
    window.splitter.addWidget(window.preview_panel)
    window.splitter.setChildrenCollapsible(False)
    window.splitter.setStretchFactor(0, 1)
    window.splitter.setStretchFactor(1, 0)
    window.splitter.setSizes([900, 260])
    window.preview_panel.hide()
    layout.addWidget(window.splitter, 1)
    window.global_progress = QProgressBar(window)
    window.global_progress.setValue(0)
    window.global_progress.setAccessibleName("Overall progress")
    layout.addWidget(window.global_progress)
    window.statusBar().showMessage("Ready")
    window.task_count_label = QLabel("0 tasks", window)
    window.task_count_label.setObjectName("mutedLabel")
    window.statusBar().addPermanentWidget(window.task_count_label)
    for button, icon in ((window.enqueue_button, QStyle.StandardPixmap.SP_ArrowDown),
                         (window.bulk_button, QStyle.StandardPixmap.SP_FileDialogListView),
                         (window.folder_button, QStyle.StandardPixmap.SP_DirOpenIcon),
                         (window.pause_button, QStyle.StandardPixmap.SP_MediaPause),
                         (window.cancel_action, QStyle.StandardPixmap.SP_DialogCancelButton),
                         (window.retry_action, QStyle.StandardPixmap.SP_BrowserReload),
                         (window.clear_button, QStyle.StandardPixmap.SP_TrashIcon),
                         (window.show_file_action, QStyle.StandardPixmap.SP_DirIcon)):
        button.setIcon(window.style().standardIcon(icon))


def build_menus(window):
    file_menu = window.menuBar().addMenu("&File")
    for text, shortcut, callback in (("Focus URL", "Ctrl+L", window.url_edit.setFocus),
                                     ("Add multiple links…", "Ctrl+Shift+L", window.open_bulk_dialog),
                                     ("Choose download folder…", "Ctrl+O", window.choose_folder)):
        action = QAction(text, window)
        action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(callback)
        file_menu.addAction(action)
    file_menu.addSeparator()
    window.quit_action = QAction("Quit", window)
    window.quit_action.setShortcut(QKeySequence("Ctrl+Q"))
    window.quit_action.triggered.connect(window.close)
    file_menu.addAction(window.quit_action)
    language_menu = window.menuBar().addMenu('Language')
    language_group = QActionGroup(window)
    language_group.setExclusive(True)
    window.language_actions = {}
    for code, label in (('en', 'English'), ('tr', 'Türkçe')):
        action = language_menu.addAction(label)
        action.setCheckable(True)
        action.setData(code)
        language_group.addAction(action)
        action.triggered.connect(lambda checked=False, selected=code: window._change_language(selected))
        window.language_actions[code] = action
    help_menu = window.menuBar().addMenu("&Help")
    window.help_menu = help_menu
    window.open_log_action = help_menu.addAction('Open log folder', window.open_log_folder)
    window.copy_error_action = help_menu.addAction('Copy error details', window.copy_error_details)
    window.copy_error_action.setEnabled(False)
    window.export_report_action = help_menu.addAction('Export diagnostic report…', window.export_diagnostic_report)
    help_menu.addAction('Third-party licenses', window.show_licenses)
    help_menu.addSeparator()
    about = help_menu.addAction(f"About {APP_NAME}")
    about.triggered.connect(lambda: window._show_message(
        "information", f"About {APP_NAME}", f"{APP_NAME} {APP_VERSION}\n\n"
        "An independent video and audio downloader for YouTube and other sites supported by yt-dlp.\n"
        "Interface by PySide6. Media processing by FFmpeg.\n"
        "Not affiliated with the yt-dlp project.\n\n"
        "This project was developed with AI using Claude and ChatGPT.", ""))


def build_output_controls(window):
    window.output_controls = OutputControls(window)
    for name in ("format_combo", "quality_combo", "container_combo", "codec_combo", "audio_codec_combo",
                 "video_quality_combo", "bitrate_combo", "bit_depth_combo", "workers_spin", "video_workers_spin",
                 "ffmpeg_threads_spin", "trim_start_edit", "trim_end_edit"):
        setattr(window, name, getattr(window.output_controls, name))
    for name in ("fragment_downloads_spin", "network_connections_spin"):
        setattr(window, name, getattr(window.output_controls, name))
    return window.output_controls
