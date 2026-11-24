"""English/Turkish Qt interface connected to the toolkit-independent task manager."""
import os
import threading
from collections import deque
from pathlib import Path

from app_info import APP_NAME

from PySide6.QtCore import Qt, QTimer, QSignalBlocker
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QComboBox,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSplitter, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from media_options import validate_output_options
from cookie_options import validate_cookie_options
from subtitle_options import validate_subtitle_options
from network_options import validate_network_options
from naming_templates import validate_naming_options
from ui.output_controls import OutputControls
from ui.options_scroll import OptionsScrollArea
from ui.i18n import register, set_language, tr
from ui.bulk_add_dialog import BulkAddDialog
from ui.preview_panel import PreviewPanel
from ui.task_table import TaskTable
from ui.task_controller import TaskController
from ui.file_actions import reveal_file, open_folder
from ui.diagnostics import (copy_error_details, open_log_folder, export_diagnostic_report,
                            add_diagnostic_buttons)
from ui.preferences import Preferences
from ui.queue_persistence import QueuePersistence
from ui.thumbnails import ThumbnailLoader
from ui.updates import UpdateController
from ui.tool_updates import ToolUpdateController
from ui.completion_actions import CompletionActions, ACTION_LABELS
from ui.source_formats_dialog import SourceFormatsDialog
from ui.playlist_selection import PlaylistSelectionDialog
from task_manager import TaskOptions
from urllib.parse import urlsplit
from utils import SETTINGS_FILE, log_warning, log_location_text


class MainWindow(QMainWindow):
    def __init__(self, manager, parent=None, *, settings_path=None):
        super().__init__(parent)
        self.controller = TaskController(manager, self)
        self._closing = False
        self._allow_close = False
        self._update_on_close = False
        self._message_boxes = set()
        self._pending_messages = deque()
        self._last_error_details = ''
        self._preferences_closed = False
        self._queue_closed = False
        self._preview_tid = None
        self.language = 'en'
        self._notice_source = 'Ready'
        self.thumbnails = ThumbnailLoader(self)
        self.preferences = Preferences(settings_path if settings_path is not None else SETTINGS_FILE, self)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "assets/app.svg")))
        self.resize(1220, 800)
        self.setMinimumSize(900, 680)
        self.folder = ""
        self.bulk_dialog = None
        self.source_dialog = None
        self.playlist_dialog = None
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(12)

        heading = QLabel(APP_NAME, self)
        heading.setObjectName("headingLabel")
        layout.addWidget(heading)
        note = QLabel("An independent video and audio downloader for YouTube and other sites supported by yt-dlp.", self)
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)

        link_row = QHBoxLayout()
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("Paste a video URL…")
        self.url_edit.setAccessibleName("Video URL")
        self.enqueue_button = QPushButton("Add to queue", self)
        self.enqueue_button.setObjectName("primaryButton")
        self.bulk_button = QPushButton("Add multiple links", self)
        self.bulk_button.clicked.connect(self.open_bulk_dialog)
        self.source_button = QPushButton('Source formats…', self)
        self.source_button.clicked.connect(self.open_source_formats)
        link_row.addWidget(self.url_edit, 1)
        link_row.addWidget(self.enqueue_button)
        link_row.addWidget(self.bulk_button)
        link_row.addWidget(self.source_button)
        layout.addLayout(link_row)

        self.options_scroll = OptionsScrollArea(self._build_options(), central)
        layout.addWidget(self.options_scroll)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(self)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText("No download folder selected")
        self.folder_edit.setAccessibleName("Download folder")
        self.folder_button = QPushButton("Choose folder", self)
        self.folder_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(self.folder_button)
        layout.addLayout(folder_row)

        actions = QHBoxLayout()
        self.pause_button = QPushButton("Pause", self)
        self.cancel_action = QAction("Cancel selected", self)
        self.retry_action = QAction("Retry failed tasks", self)
        self.clear_button = QPushButton("Clear list…", self)
        self.show_file_action = QAction("Show in folder", self)
        self.diagnostics_action = QAction('View diagnostics', self)
        self.task_actions_button = QToolButton(self)
        self.task_actions_button.setText('Task actions')
        self.task_actions_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.task_actions_menu = QMenu(self.task_actions_button)
        self.task_actions_menu.addActions([self.cancel_action, self.retry_action])
        self.task_actions_menu.addSeparator()
        self.task_actions_menu.addActions([self.show_file_action, self.diagnostics_action])
        self.task_actions_button.setMenu(self.task_actions_menu)
        self.task_actions_menu.aboutToShow.connect(self._refresh_actions)
        for button in (self.pause_button, self.clear_button, self.task_actions_button):
            actions.addWidget(button)
        actions.addStretch()
        self.completion_combo = QComboBox(self)
        self.completion_combo.setAccessibleName('When queue finishes')
        for value, label in ACTION_LABELS.items():
            self.completion_combo.addItem(label, value)
        self.completion_combo.setToolTip('Runs only after all new tasks succeed. Power actions have a 30-second cancel countdown. Resets each session.')
        actions.addWidget(QLabel('When queue finishes', self))
        actions.addWidget(self.completion_combo)
        self.finish_actions = CompletionActions(manager, self._perform_completion_action, self)
        self.finish_actions.reset_requested.connect(self._reset_completion_combo)
        self.finish_actions.message.connect(self._show_message)
        self.finish_actions.notice.connect(self._show_notice)
        self.completion_combo.currentIndexChanged.connect(lambda _: self.finish_actions.arm(self.completion_combo.currentData()))
        layout.addLayout(actions)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.task_table = TaskTable(self.splitter)
        self.preview_panel = PreviewPanel(self.splitter)
        self.splitter.addWidget(self.task_table)
        self.splitter.addWidget(self.preview_panel)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([900, 260])
        self.preview_panel.hide()
        layout.addWidget(self.splitter, 1)
        self.global_progress = QProgressBar(self)
        self.global_progress.setValue(0)
        self.global_progress.setAccessibleName("Overall progress")
        layout.addWidget(self.global_progress)
        self.statusBar().showMessage("Ready")
        self.task_count_label = QLabel("0 tasks", self)
        self.task_count_label.setObjectName("mutedLabel")
        self.statusBar().addPermanentWidget(self.task_count_label)
        for button, icon in ((self.enqueue_button, QStyle.StandardPixmap.SP_ArrowDown),
                             (self.bulk_button, QStyle.StandardPixmap.SP_FileDialogListView),
                             (self.folder_button, QStyle.StandardPixmap.SP_DirOpenIcon),
                             (self.pause_button, QStyle.StandardPixmap.SP_MediaPause),
                             (self.cancel_action, QStyle.StandardPixmap.SP_DialogCancelButton),
                             (self.retry_action, QStyle.StandardPixmap.SP_BrowserReload),
                             (self.clear_button, QStyle.StandardPixmap.SP_TrashIcon),
                             (self.show_file_action, QStyle.StandardPixmap.SP_DirIcon)):
            button.setIcon(self.style().standardIcon(icon))

        self.enqueue_button.clicked.connect(self.enqueue)
        self.url_edit.returnPressed.connect(self.enqueue)
        self.pause_button.clicked.connect(self.controller.toggle_pause)
        self.cancel_action.triggered.connect(self.cancel_selected)
        self.retry_action.triggered.connect(self.retry_failed)
        self.clear_button.clicked.connect(self.clear_list)
        self.clear_button.setToolTip("Clear finished tasks, with an option to remove queued tasks as well.")
        self.show_file_action.triggered.connect(self.show_selected_file)
        self.diagnostics_action.triggered.connect(self.show_selected_diagnostics)
        self.workers_spin.valueChanged.connect(self.controller.set_worker_count)
        self.controller.tasks_changed.connect(self.task_table.update_tasks)
        self.controller.tasks_removed.connect(self.task_table.remove_tasks)
        self.controller.summary_changed.connect(self._update_summary)
        self.controller.actions_changed.connect(self._refresh_actions)
        self.controller.pause_changed.connect(self._pause_changed)
        self.controller.notice.connect(self._show_notice)
        self.controller.message.connect(self._show_message)
        self.controller.shutdown_complete.connect(self._finish_close)
        self.controller.shutdown_wait_required.connect(self._ask_shutdown_choice)
        self.task_table.view.doubleClicked.connect(self._activate_task)
        self.task_table.selection_changed.connect(self._refresh_actions)
        self.task_table.selection_changed.connect(self._update_preview)
        self.task_table.show_file_requested.connect(self.show_file)
        self.task_table.diagnostics_requested.connect(self.show_task_details)
        self.task_table.cancel_requested.connect(self._cancel_tasks)
        self.task_table.retry_requested.connect(self.controller.retry_failed)
        self._sync_format_controls()
        self._build_menus()
        self.preferences.message.connect(self._show_message)
        self._restore_preferences()
        self._apply_processing_limits()
        self.video_workers_spin.valueChanged.connect(self._apply_processing_limits)
        self.ffmpeg_threads_spin.valueChanged.connect(self._apply_processing_limits)
        self.fragment_downloads_spin.valueChanged.connect(self._apply_processing_limits)
        self.network_connections_spin.valueChanged.connect(self._apply_processing_limits)
        self.output_controls.changed.connect(self._schedule_preferences)
        self.output_controls.profiles.profiles_changed.connect(self._persist_profiles)
        self.workers_spin.valueChanged.connect(self._schedule_preferences)
        self.video_workers_spin.valueChanged.connect(self._schedule_preferences)
        self.ffmpeg_threads_spin.valueChanged.connect(self._schedule_preferences)
        self.fragment_downloads_spin.valueChanged.connect(self._schedule_preferences)
        self.network_connections_spin.valueChanged.connect(self._schedule_preferences)
        self.controller.thumbnail_available.connect(self._register_thumbnail)
        self.controller.task_changed.connect(self._preview_task_changed)
        self.controller.task_removed.connect(self._remove_preview_task)
        self.thumbnails.image_ready.connect(self._preview_image_ready)
        self.thumbnails.state_changed.connect(self.preview_panel.set_state)
        self.controller.encoders_changed.connect(self.output_controls.set_encoders, Qt.ConnectionType.QueuedConnection)
        settings_file = self.preferences.store.path
        queue_path = settings_file.with_name('yd_queue.json' if settings_file.name == 'yd_settings.json'
                                             else settings_file.stem + '.queue.json')
        self.queue_persistence = QueuePersistence(manager, queue_path, self)
        self.queue_persistence.message.connect(self._show_message)
        self.queue_persistence.closed.connect(self._finish_close, Qt.ConnectionType.QueuedConnection)
        restored = self.queue_persistence.load()
        self.controller.start(self.workers_spin.value())
        if restored:
            self._show_notice('Saved queue restored. Press Resume to restart unfinished downloads.'
                              if manager.paused else 'Saved queue restored.', 0)
        QTimer.singleShot(0, self.controller.load_encoders)
        self.updater = UpdateController(self, self.help_menu)
        self.tool_updater = ToolUpdateController(self, self.help_menu)
        register(self, self._retranslate)

    def _change_language(self, code):
        if self._closing:
            return
        self.language = code
        set_language(code)
        self.preferences.schedule({'language': code})

    def _reset_completion_combo(self):
        blocker = QSignalBlocker(self.completion_combo)
        self.completion_combo.setCurrentIndex(0)
        del blocker

    def _perform_completion_action(self, action, paths):
        if action == 'folder':
            for folder in sorted({path if os.path.isdir(path) else os.path.dirname(path) for path in paths}):
                if os.path.isdir(folder):
                    open_folder(folder)
        elif action == 'close':
            self.close()
        else:
            if not self.preferences.flush():
                return
            manager = self.controller.manager
            revision = manager.queue_revision
            def perform():
                from power_actions import run_power_action
                try:
                    run_power_action(action)
                except (OSError, ValueError, RuntimeError) as error:
                    log_warning('After-download power action failed', error)
                    try:
                        self.finish_actions.message.emit('warning', 'After-download action',
                            'The after-download action could not be completed.', str(error))
                    except RuntimeError:
                        pass  # The application may have closed while Windows was suspended.
            def saved(success):
                if not success or self._closing:
                    return
                if manager.queue_revision != revision or manager.paused or manager.has_update_work():
                    self._show_notice('After-download action cancelled.')
                    return
                threading.Thread(target=perform, name='after-download-power', daemon=True).start()
            self.queue_persistence.flush(on_finished=saved)

    def _retranslate(self):
        self._pause_changed(self.controller.manager.paused)
        self._refresh_actions()
        summary = self.controller.manager.summary()
        self._update_summary(summary.total, summary.pending, summary.progress)
        for code, action in self.language_actions.items():
            action.setChecked(code == self.language)
        if self.statusBar().currentMessage():
            self.statusBar().showMessage(tr(self._notice_source))

    def _show_notice(self, message, timeout=5000):
        self._notice_source = message
        self.statusBar().showMessage(tr(message), timeout)

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        for text, shortcut, callback in (("Focus URL", "Ctrl+L", self.url_edit.setFocus),
                                         ("Add multiple links…", "Ctrl+Shift+L", self.open_bulk_dialog),
                                         ("Choose download folder…", "Ctrl+O", self.choose_folder)):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            file_menu.addAction(action)
        file_menu.addSeparator()
        self.quit_action = QAction("Quit", self)
        self.quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self.quit_action.triggered.connect(self.close)
        file_menu.addAction(self.quit_action)
        language_menu = self.menuBar().addMenu('Language')
        language_group = QActionGroup(self)
        language_group.setExclusive(True)
        self.language_actions = {}
        for code, label in (('en', 'English'), ('tr', 'Türkçe')):
            action = language_menu.addAction(label)
            action.setCheckable(True)
            action.setData(code)
            language_group.addAction(action)
            action.triggered.connect(lambda checked=False, selected=code: self._change_language(selected))
            self.language_actions[code] = action
        help_menu = self.menuBar().addMenu("&Help")
        self.help_menu = help_menu
        self.open_log_action = help_menu.addAction('Open log folder', self.open_log_folder)
        self.copy_error_action = help_menu.addAction('Copy error details', self.copy_error_details)
        self.copy_error_action.setEnabled(False)
        self.export_report_action = help_menu.addAction('Export diagnostic report…', self.export_diagnostic_report)
        help_menu.addAction('Third-party licenses', self.show_licenses)
        help_menu.addSeparator()
        about = help_menu.addAction(f"About {APP_NAME}")
        about.triggered.connect(lambda: self._show_message(
            "information", f"About {APP_NAME}", f"{APP_NAME}\n\n"
            "An independent video and audio downloader for YouTube and other sites supported by yt-dlp.\n"
            "Interface by PySide6. Media processing by FFmpeg.\n"
            "Not affiliated with the yt-dlp project.\n\n"
            "This project was developed with AI using Claude and ChatGPT.", ""))

    def _pause_changed(self, paused):
        self.pause_button.setText(tr("Resume" if paused else "Pause"))
        self.pause_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay if paused else QStyle.StandardPixmap.SP_MediaPause))

    def _build_options(self):
        self.output_controls = OutputControls(self)
        for name in ("format_combo", "quality_combo", "container_combo", "codec_combo", "audio_codec_combo",
                     "video_quality_combo", "bitrate_combo", "bit_depth_combo", "workers_spin", "video_workers_spin",
                     "ffmpeg_threads_spin", "trim_start_edit", "trim_end_edit"):
            setattr(self, name, getattr(self.output_controls, name))
        for name in ("fragment_downloads_spin", "network_connections_spin"):
            setattr(self, name, getattr(self.output_controls, name))
        return self.output_controls

    def _sync_format_controls(self):
        self.output_controls.refresh()

    def _restore_preferences(self):
        data = self.preferences.load()
        self.language = data['language']
        set_language(self.language)
        self.folder = data["last_folder"]
        self.folder_edit.setText(self.folder)
        self.folder_edit.setToolTip(self.folder)
        self.output_controls.restore(data)
        self.output_controls.profiles.restore(data.get('saved_profiles', []))

    def _persist_profiles(self):
        self.preferences.schedule({'saved_profiles': self.output_controls.profiles.profiles()})
        if not self.preferences.flush() or not self.preferences.store.writable:
            self.output_controls.profiles.persistence_failed()

    def _schedule_preferences(self):
        values = self.output_controls.values()
        try:
            validate_output_options(values)
            validate_cookie_options(values)
            validate_subtitle_options(values)
            validate_network_options(values)
            validate_naming_options(values)
        except ValueError:
            return
        self.preferences.schedule(dict(values, language=self.language, last_folder=self.folder, workers=self.workers_spin.value(),
                                       video_workers=self.video_workers_spin.value(),
                                       ffmpeg_threads=self.ffmpeg_threads_spin.value(),
                                       fragment_downloads=self.fragment_downloads_spin.value(),
                                       network_connections=self.network_connections_spin.value()))

    def _apply_processing_limits(self):
        engine = self.controller.manager.engine
        if callable(getattr(type(engine), "configure_processing", None)):
            engine.configure_processing(self.video_workers_spin.value(), self.ffmpeg_threads_spin.value())
        if callable(getattr(type(engine), "configure_network", None)):
            engine.configure_network(self.fragment_downloads_spin.value(), self.network_connections_spin.value())

    def _update_preview(self):
        tids = self.task_table.selected_task_ids()
        current = self.task_table.view.currentIndex().data(Qt.ItemDataRole.UserRole)
        tid = current if current in tids else (tids[0] if tids else None)
        task = self.controller.manager.get_task(tid) if tid else None
        self._preview_tid = tid if task else None
        self.preview_panel.setVisible(task is not None)
        if task is None:
            self.preview_panel.clear()
            return
        self.preview_panel.set_task(tid, task.get("title", ""))
        self.thumbnails.request(tid)

    def _register_thumbnail(self, tid, url):
        task = self.controller.manager.get_task(tid)
        if not self._closing and task is not None:
            self.thumbnails.register(tid, url, task)
            if tid == self._preview_tid:
                self.thumbnails.request(tid)

    def _preview_task_changed(self, tid, task):
        if tid == self._preview_tid:
            self.preview_panel.set_task(tid, task.get("title", ""))

    def _remove_preview_task(self, tid):
        self.thumbnails.remove(tid)
        if tid == self._preview_tid:
            self._update_preview()

    def _preview_image_ready(self, tid, image):
        if self._closing or self.controller.manager.get_task(tid) is None:
            self.thumbnails.remove(tid)
            return
        self.preview_panel.set_image(tid, image)

    def choose_folder(self):
        if self._closing:
            return
        folder = QFileDialog.getExistingDirectory(
            self, tr("Choose download folder"), self.folder,
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if folder:
            self.folder = folder
            self.folder_edit.setText(folder)
            self.folder_edit.setToolTip(folder)
            self._schedule_preferences()

    def open_bulk_dialog(self):
        if self._closing:
            return
        if self.bulk_dialog is None:
            self.bulk_dialog = BulkAddDialog(self)
            self.bulk_dialog.links_submitted.connect(self._submit_bulk_links)
            self.bulk_dialog.playlist_requested.connect(self.open_playlist_selection)
        self.bulk_dialog.open()

    def open_playlist_selection(self, url):
        if self._closing:
            return
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('Enter one playlist URL to choose which videos to download.')
            if not self.folder or not os.path.isdir(self.folder):
                raise ValueError('Choose an existing download folder first.')
            options = TaskOptions(folder=self.folder, **self.output_controls.values(),
                **self.output_controls.trim_values())
            validate_cookie_options(self.output_controls.values(), check_file=True)
        except ValueError as error:
            self._show_message('warning', 'Choose playlist videos', str(error), '')
            return
        if self.playlist_dialog is not None:
            self.playlist_dialog.reject()
            self.playlist_dialog.deleteLater()
        parent = self.bulk_dialog if self.bulk_dialog is not None and self.bulk_dialog.isVisible() else self
        self.playlist_dialog = PlaylistSelectionDialog(self.controller.manager, url, options, parent)
        self.playlist_dialog.queued.connect(self._playlist_queued)
        self.playlist_dialog.open()
        self.playlist_dialog.load()

    def _playlist_queued(self, url, added, skipped):
        if self.bulk_dialog is not None and self.bulk_dialog.links() == [url]:
            self.bulk_dialog.links_edit.clear()
            self.bulk_dialog.accept()
        self.controller.poll()
        self._show_notice(tr('{added} selected videos queued; {skipped} already queued or repeated.', added=added, skipped=skipped))

    def open_source_formats(self):
        if self._closing:
            return
        url = self.url_edit.text().strip()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('Enter a single video URL to inspect its formats.')
            if not self.folder or not os.path.isdir(self.folder):
                raise ValueError('Choose an existing download folder first.')
            options = TaskOptions(folder=self.folder, **self.output_controls.values(),
                **self.output_controls.trim_values())
            validate_cookie_options(self.output_controls.values(), check_file=True)
        except ValueError as error:
            self._show_message('warning', 'Source formats', str(error), '')
            return
        if self.source_dialog is not None:
            self.source_dialog.reject()
            self.source_dialog.deleteLater()
        self.source_dialog = SourceFormatsDialog(self.controller.manager, url, options, self)
        self.source_dialog.queued.connect(self._source_queued)
        self.source_dialog.open()
        self.source_dialog.load()

    def _source_queued(self, url):
        if self.url_edit.text().strip() == url:
            self.url_edit.clear()
        self.controller.poll()
        self._show_notice('Selected source formats added to the queue.')

    def _submit_links(self, links, *, expand_playlist):
        if self._closing:
            return False
        if not links:
            self._validation_warning("Missing URL", "Enter a video URL first.")
            return False
        if not self.folder or not os.path.isdir(self.folder):
            self._validation_warning("Download folder", "Choose an existing download folder first.")
            return False
        try:
            validate_output_options(self.output_controls.values())
        except ValueError as error:
            self._validation_warning("Invalid output profile", str(error))
            return False
        try:
            validate_cookie_options(self.output_controls.values(), check_file=True)
        except ValueError as error:
            self._validation_warning('Cookie settings', str(error))
            return False
        try:
            validate_subtitle_options(self.output_controls.values())
        except ValueError as error:
            self._validation_warning('Subtitle settings', str(error))
            return False
        try:
            validate_network_options(self.output_controls.values())
        except ValueError as error:
            self._validation_warning('Network settings', str(error))
            return False
        try:
            validate_naming_options(self.output_controls.values())
        except ValueError as error:
            self._validation_warning('File and folder naming', str(error))
            return False
        try:
            return self.controller.submit_links(
                links, expand_playlist=expand_playlist, folder=self.folder,
                **self.output_controls.values(),
                **self.output_controls.trim_values())
        except ValueError as error:
            self._validation_warning(
                "Invalid trim range",
                "Use seconds, mm:ss or hh:mm:ss (for example, 90 or 1:30). "
                "Values must be non-negative, and the end must be later than the start.", str(error))
            return False

    def _validation_warning(self, title, text, details=''):
        if title in ('Invalid trim range', 'Invalid output profile', 'Cookie settings', 'Subtitle settings', 'Network settings', 'File and folder naming'):
            self.output_controls.advanced_toggle.setChecked(True)
        log_warning(title, text, [('details', details)],
                    once_key=('input_validation', title, text, details),
                    event='input_validation_failed', component='qt')
        self._show_message('warning', title, text, details)

    def enqueue(self):
        url = self.url_edit.text().strip()
        if self._submit_links([url] if url else [], expand_playlist=False):
            self.url_edit.clear()

    def _submit_bulk_links(self, links):
        if self._submit_links(links, expand_playlist=True):
            self.bulk_dialog.links_edit.clear()
            self.bulk_dialog.accept()

    def cancel_selected(self):
        tids = self.task_table.selected_task_ids()
        if not tids:
            self._show_message("information", "Select a task", "Select the tasks you want to cancel.", "")
            return
        self._cancel_tasks(tids)

    def _cancel_tasks(self, tids):
        if self._closing:
            return
        cancellable = [tid for tid in tids if (task := self.controller.manager.get_task(tid))
                       and not task["status"].startswith(("Completed", "Error", "Cancel"))]
        if len(cancellable) > 1:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Cancel tasks")
            box.setText(tr('Cancel {count} selected tasks?', count=len(cancellable)))
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.finished.connect(lambda result: self.controller.cancel_tasks(cancellable)
                                 if result == QMessageBox.StandardButton.Yes else None)
            self._open_message(box)
        else:
            self.controller.cancel_tasks(cancellable)

    def retry_failed(self):
        self.controller.retry_failed(self.task_table.selected_task_ids())

    def clear_list(self):
        if self._closing:
            return
        pending = self.controller.manager.summary().pending > 0
        if not pending:
            self.controller.clear_tasks()
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Clear task list")
        box.setText(tr("Remove queued tasks as well?\n\n"
                    "Yes: clear finished and queued tasks.\n"
                    "No: clear only completed, failed and cancelled tasks.\n"
                    "Active downloads and saved files are kept."))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                               QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.finished.connect(lambda result: self.controller.clear_tasks(
            remove_pending=result == QMessageBox.StandardButton.Yes)
            if result in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No) else None)
        self._open_message(box)

    def _refresh_actions(self):
        if self._closing:
            return
        manager = self.controller.manager
        summary = manager.summary()
        selected_ids = self.task_table.selected_task_ids()
        selected = [task for tid in selected_ids if (task := manager.get_task(tid)) is not None]
        self.cancel_action.setEnabled(any(not t["status"].startswith(("Completed", "Error", "Cancel"))
                                          for t in selected))
        retryable = (any(t["status"].startswith("Error") and manager.task_control(t["id"])[1] is None
                         for t in selected) if selected_ids else summary.retryable > 0)
        self.retry_action.setEnabled(retryable)
        self.retry_action.setText(tr('Retry selected failed tasks' if selected_ids else 'Retry all failed tasks'))
        self.show_file_action.setEnabled(len(selected) == 1 and
                                         selected[0]["status"].startswith("Completed") and
                                         bool(selected[0].get("output_path")))
        self.clear_button.setEnabled(summary.clearable > 0)
        self.pause_button.setEnabled(summary.total > 0 or manager.paused)
        self.diagnostics_action.setEnabled(len(selected) == 1 and
                                           (selected[0]['status'].startswith('Error') or bool(selected[0].get('warnings'))))

    def _update_summary(self, total, pending, progress):
        self.global_progress.setValue(progress)
        self.task_count_label.setText(tr('{total} task(s) • {pending} queued', total=total, pending=pending))

    def _activate_task(self, index):
        tid = index.data(Qt.ItemDataRole.UserRole)
        task = self.controller.manager.get_task(tid)
        if task and task["status"].startswith("Completed"):
            self.show_file(tid)
        elif task:
            self.show_task_details(tid)

    def show_selected_file(self):
        tids = self.task_table.selected_task_ids()
        if len(tids) == 1:
            self.show_file(tids[0])

    def show_selected_diagnostics(self):
        tids = self.task_table.selected_task_ids()
        if len(tids) == 1:
            self.show_task_details(tids[0])

    def show_file(self, tid):
        if self._closing:
            return
        task = self.controller.manager.get_task(tid)
        if not task or not task["status"].startswith("Completed"):
            return
        try:
            reveal_file(task.get("output_path"))
        except OSError as error:
            log_warning("Could not reveal output file", error, [("task", tid)])
            self._show_message("warning", "Could not show file",
                               "The file may have been moved or deleted, or the file manager could not start.",
                               str(error))

    def show_task_details(self, tid):
        task = self.controller.manager.get_task(tid)
        if task is None:
            return
        details = "\n".join(filter(None, [tr('Task: {value}', value=tid), tr('Attempt: {value}', value=task.get('attempt', 0)),
                                         tr('Stage: {value}', value=task.get('_log_stage', 'resolve')),
                                         task.get("status", ""), *task.get("warnings", [])]))
        if task["status"].startswith("Error") or task.get("warnings"):
            self._show_message("warning", "Task diagnostics",
                               f"Diagnostic details for {tid}. Engine and provider messages are shown verbatim. "
                               "Use the diagnostic actions to inspect or share the error context.", details)

    def copy_error_details(self):
        if not self._closing and self._last_error_details:
            copy_error_details(self._last_error_details)
            self._show_notice('Error details copied.')

    def show_licenses(self):
        if self._closing:
            return
        from app_paths import licenses_root
        folder = licenses_root()
        try:
            if not (folder / 'THIRD_PARTY_NOTICES.md').is_file():
                raise FileNotFoundError('Run tools/collect_licenses.py in the build environment.')
            open_folder(str(folder))
        except OSError as error:
            self._show_message('warning', 'Third-party licenses',
                               'The license folder could not be opened.', str(error))

    def open_log_folder(self):
        if self._closing:
            return
        try:
            open_log_folder()
        except OSError as error:
            self._show_message('warning', 'Could not open log folder',
                               'The log folder could not be opened.', str(error))

    def export_diagnostic_report(self, details=None):
        if self._closing:
            return
        # QAction.triggered may supply its checked boolean.
        if not isinstance(details, str):
            details = self._last_error_details
        try:
            path = export_diagnostic_report(self, details, self.controller.manager.list_tasks())
        except (OSError, ValueError) as error:
            self._show_message('warning', 'Could not export diagnostic report',
                               'The report could not be saved. Choose another filename or folder.', str(error))
        else:
            if path:
                self._show_notice(f'Diagnostic report saved: {path}')

    def _show_message(self, kind, title, text, details):
        if self._closing and title != 'Queue warning':
            return
        if self._message_boxes and not (self._preferences_closed and title == "Preferences warning"):
            self._pending_messages.append((kind, title, text, details))
            return
        box = QMessageBox(self)
        box.setProperty('_message_key', title)
        box.setIcon({"critical": QMessageBox.Icon.Critical, "warning": QMessageBox.Icon.Warning,
                     "information": QMessageBox.Icon.Information}[kind])
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(tr(text))
        if details:
            box.setDetailedText(details)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        if kind in ('critical', 'warning'):
            diagnostic_details = f'{title}\n{text}\n\n{details}\n\n{log_location_text()}'
            self._last_error_details = diagnostic_details
            self.copy_error_action.setEnabled(True)
            box.setInformativeText(tr(log_location_text()))
            add_diagnostic_buttons(box, diagnostic_details, self.open_log_folder,
                                   lambda: self.export_diagnostic_report(diagnostic_details))
        self._open_message(box)

    def _open_message(self, box):
        # Asynchronous dialogs keep the event and shutdown timers running.
        self._message_boxes.add(box)
        register(box)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.finished.connect(lambda result: self._message_finished(box))
        box.open()

    def _message_finished(self, box):
        self._message_boxes.discard(box)
        QTimer.singleShot(0, self._show_next_message)

    def _show_next_message(self):
        if not self._closing and not self._message_boxes and self._pending_messages:
            self._show_message(*self._pending_messages.popleft())

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        if self._update_on_close:
            manager = self.controller.manager
            if manager.has_update_work():
                self._update_on_close = False
                self.updater._info('Finish or cancel pending downloads before installing the update. The verified update is ready for this session.')
                return
            # Flushing may show a save warning. Keep preferences usable if
            # installation is deferred rather than closing the application.
            if not self._preferences_closed and not self.preferences.flush():
                self._update_on_close = False
                return
            # Admission and the idle check share the task lock. A resolver or
            # another producer cannot insert work between this check and close.
            if not manager.close(require_idle=True):
                self._update_on_close = False
                self.updater._info('Finish or cancel pending downloads before installing the update. The verified update is ready for this session.')
                return
        if not self._preferences_closed:
            self._preferences_closed = True
            if not self.preferences.close():
                # Keep the save error visible until acknowledged, then resume
                # the ordinary asynchronous shutdown without retrying forever.
                warning = next((box for box in self._message_boxes
                                if box.property('_message_key') == "Preferences warning"), None)
                if warning is None:
                    self._show_message("warning", "Preferences warning",
                                       "Your recent preferences could not be saved. "
                                       "Click OK to continue closing.", "Use the log file location below for details.")
                    warning = next(box for box in self._message_boxes
                                   if box.property('_message_key') == "Preferences warning")
                warning.finished.connect(lambda result: self.close())
                return
        self._closing = True
        self.finish_actions.close()
        if self.playlist_dialog is not None:
            self.playlist_dialog.reject()
        if self.source_dialog is not None:
            self.source_dialog.reject()
        self.queue_persistence.timer.stop()
        self._pending_messages.clear()
        self.updater.close(installing=self._update_on_close)
        self.tool_updater.close()
        self.thumbnails.close()
        self.centralWidget().setEnabled(False)
        if self.bulk_dialog is not None:
            self.bulk_dialog.reject()
        for box in list(self._message_boxes):
            box.reject()
        self._show_notice("Closing — cancelling tasks and cleaning up…", 0)
        self.controller.begin_shutdown()

    def _finish_close(self):
        # finish_shutdown() may succeed inside closeEvent itself. Defer the
        # accepted close until that ignored event has returned to Qt.
        QTimer.singleShot(0, self._complete_close)

    def _complete_close(self):
        if self._allow_close:
            return
        if not self._queue_closed:
            saved = self.queue_persistence.close()
            if saved is None:
                return
            self._queue_closed = True
            if not saved:
                warning = next((box for box in self._message_boxes
                                if box.property('_message_key') == 'Queue warning'), None)
                if warning is None:
                    self._show_message('warning', 'Queue warning',
                        'Your queue could not be saved. Recent changes may not be restored next time.', '')
                    warning = next(box for box in self._message_boxes
                                   if box.property('_message_key') == 'Queue warning')
                warning.finished.connect(lambda result: self._complete_close())
                return
        if self._update_on_close:
            try:
                self.updater.apply_on_exit()
            except (OSError, ValueError) as error:
                # The old executable is untouched if the helper cannot start.
                self._update_on_close = False
                self.updater.close()
                log_warning('Could not start update helper', error, event='update_failed', component='updater')
                box = QMessageBox(self)
                box.setWindowTitle(tr('Software update'))
                box.setText(tr('The update could not be completed. Your current version is unchanged.'))
                box.setDetailedText(str(error))
                box.finished.connect(lambda result: self._complete_close())
                self._open_message(box)
                return
        self._allow_close = True
        for box in list(self._message_boxes):
            box.reject()
        self.close()

    def _ask_shutdown_choice(self):
        box = QMessageBox(self)
        box.setWindowTitle("Some tasks have not stopped")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(tr(
            "Cancellation and process termination have timed out. Close now?\n\n"
            "Completed files are preserved, but partial files may remain.\n\n"
            "Choose No to wait another {seconds} seconds.", seconds=f'{self.controller.manager.shutdown_grace_seconds:g}'))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.finished.connect(self._shutdown_choice)
        self._open_message(box)

    def _shutdown_choice(self, result):
        if not self._allow_close:
            self.controller.resolve_shutdown_choice(result == QMessageBox.StandardButton.Yes)
