"""English/Turkish Qt interface connected to the toolkit-independent task manager."""
import os
from collections import deque
from pathlib import Path

from app_info import APP_NAME

from PySide6.QtCore import Qt, QTimer, QSignalBlocker
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox, QStyle

from media_options import validate_output_options
from cookie_options import validate_cookie_options, cookie_error_guidance
from subtitle_options import validate_subtitle_options
from network_options import validate_network_options
from naming_templates import validate_naming_options
from ui.i18n import register, set_language, tr
from ui.task_controller import TaskController
from ui.file_actions import open_folder
from ui.diagnostics import (copy_error_details, open_log_folder, export_diagnostic_report,
                            add_diagnostic_buttons)
from ui.preferences import Preferences
from ui.queue_persistence import QueuePersistence
from ui.thumbnails import ThumbnailLoader
from ui.updates import UpdateController
from ui.tool_updates import ToolUpdateController
from ui.completion_actions import CompletionActions
from ui.window_layout import build_window, build_menus
from ui.window_tasks import WindowTaskActions
from ui.window_shutdown import WindowShutdown
from app_paths import SETTINGS_FILE
from app_logging import log_warning, log_location_text


class MainWindow(QMainWindow):
    def __init__(self, manager, parent=None, *, settings_path=None):
        super().__init__(parent)
        self.controller = TaskController(manager, self)
        self.shutdown_flow = WindowShutdown(self)
        self.task_actions = WindowTaskActions(self)
        self._message_boxes = set()
        self._pending_messages = deque()
        self._last_error_details = ''
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
        build_window(self)
        self.finish_actions = CompletionActions(manager, self._perform_completion_action, self)
        self.finish_actions.reset_requested.connect(self._reset_completion_combo)
        self.finish_actions.message.connect(self._show_message)
        self.finish_actions.notice.connect(self._show_notice)
        self.completion_combo.currentIndexChanged.connect(lambda _: self.finish_actions.arm(self.completion_combo.currentData()))
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
        self.controller.shutdown_complete.connect(self.shutdown_flow.schedule_finish)
        self.controller.shutdown_wait_required.connect(self.shutdown_flow.ask_choice)
        self.task_table.view.doubleClicked.connect(self._activate_task)
        self.task_table.selection_changed.connect(self._refresh_actions)
        self.task_table.selection_changed.connect(self._update_preview)
        self.task_table.show_file_requested.connect(self.show_file)
        self.task_table.diagnostics_requested.connect(self.show_task_details)
        self.task_table.cancel_requested.connect(self._cancel_tasks)
        self.task_table.retry_requested.connect(self.controller.retry_failed)
        self._sync_format_controls()
        build_menus(self)
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
        self.queue_persistence.closed.connect(self.shutdown_flow.schedule_finish, Qt.ConnectionType.QueuedConnection)
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
        return self.task_actions._perform_completion_action(action, paths)

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

    def _pause_changed(self, paused):
        self.pause_button.setText(tr("Resume" if paused else "Pause"))
        self.pause_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay if paused else QStyle.StandardPixmap.SP_MediaPause))

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
        return self.task_actions.open_bulk_dialog()

    def open_playlist_selection(self, url):
        return self.task_actions.open_playlist_selection(url)

    def _playlist_queued(self, url, added, skipped):
        return self.task_actions._playlist_queued(url, added, skipped)

    def open_source_formats(self):
        return self.task_actions.open_source_formats()

    def _source_queued(self, url):
        return self.task_actions._source_queued(url)

    def _submit_links(self, links, *, expand_playlist):
        return self.task_actions._submit_links(links, expand_playlist=expand_playlist)

    def _validation_warning(self, title, text, details=''):
        return self.task_actions._validation_warning(title, text, details)

    def enqueue(self):
        return self.task_actions.enqueue()

    def _submit_bulk_links(self, links):
        return self.task_actions._submit_bulk_links(links)

    def cancel_selected(self):
        return self.task_actions.cancel_selected()

    def _cancel_tasks(self, tids):
        return self.task_actions._cancel_tasks(tids)

    def retry_failed(self):
        return self.task_actions.retry_failed()

    def clear_list(self):
        return self.task_actions.clear_list()

    def _refresh_actions(self):
        return self.task_actions._refresh_actions()

    def _update_summary(self, total, pending, progress):
        self.global_progress.setValue(progress)
        self.task_count_label.setText(tr('{total} task(s) • {pending} queued', total=total, pending=pending))

    def _activate_task(self, index):
        return self.task_actions._activate_task(index)

    def show_selected_file(self):
        return self.task_actions.show_selected_file()

    def show_selected_diagnostics(self):
        return self.task_actions.show_selected_diagnostics()

    def show_file(self, tid):
        return self.task_actions.show_file(tid)

    def show_task_details(self, tid):
        return self.task_actions.show_task_details(tid)

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
        text = cookie_error_guidance(details) or cookie_error_guidance(text) or text
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
        self.shutdown_flow.handle_close(event)

    # Compatibility accessors for existing window integrations. Close state is
    # owned exclusively by WindowShutdown.
    @property
    def _closing(self):
        return self.shutdown_flow.closing

    @_closing.setter
    def _closing(self, value):
        self.shutdown_flow.closing = value

    @property
    def _allow_close(self):
        return self.shutdown_flow.allow_close

    @_allow_close.setter
    def _allow_close(self, value):
        self.shutdown_flow.allow_close = value

    @property
    def _update_on_close(self):
        return self.shutdown_flow.update_on_close

    @_update_on_close.setter
    def _update_on_close(self, value):
        self.shutdown_flow.update_on_close = value

    @property
    def _preferences_closed(self):
        return self.shutdown_flow.preferences_closed

    @_preferences_closed.setter
    def _preferences_closed(self, value):
        self.shutdown_flow.preferences_closed = value

    @property
    def _queue_closed(self):
        return self.shutdown_flow.queue_closed

    @_queue_closed.setter
    def _queue_closed(self, value):
        self.shutdown_flow.queue_closed = value
