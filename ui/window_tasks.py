"""User task actions and download dialogs for a single main window."""
import os
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QMessageBox

from task_manager import TaskOptions
from task_state import TaskState, state_of, STOPPED_STATES
from media_options import validate_output_options
from cookie_options import validate_cookie_options
from subtitle_options import validate_subtitle_options
from network_options import validate_network_options
from naming_templates import validate_naming_options
from ui.bulk_add_dialog import BulkAddDialog
from ui.file_actions import open_folder, reveal_file
from ui.i18n import tr
from ui.playlist_selection import PlaylistSelectionDialog
from ui.source_formats_dialog import SourceFormatsDialog
from app_logging import log_warning


class WindowTaskActions(QObject):
    """Coordinate task UI operations without owning the window's construction."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def _perform_completion_action(self, action, paths):
        window = self.window
        if action == 'folder':
            for folder in sorted({path if os.path.isdir(path) else os.path.dirname(path) for path in paths}):
                if os.path.isdir(folder):
                    open_folder(folder)
        elif action == 'close':
            window.close()
        else:
            if not window.preferences.flush():
                return
            manager = window.controller.manager
            revision = manager.queue_revision
            def perform():
                from power_actions import run_power_action
                try:
                    run_power_action(action)
                except (OSError, ValueError, RuntimeError) as error:
                    log_warning('After-download power action failed', error)
                    try:
                        window.finish_actions.message.emit('warning', 'After-download action',
                            'The after-download action could not be completed.', str(error))
                    except RuntimeError:
                        pass  # The application may have closed while Windows was suspended.
            def saved(success):
                if not success or window._closing:
                    return
                if manager.queue_revision != revision or manager.paused or manager.has_update_work():
                    window._show_notice('After-download action cancelled.')
                    return
                threading.Thread(target=perform, name='after-download-power', daemon=True).start()
            window.queue_persistence.flush(on_finished=saved)

    def open_bulk_dialog(self):
        window = self.window
        if window._closing:
            return
        if window.bulk_dialog is None:
            window.bulk_dialog = BulkAddDialog(window)
            window.bulk_dialog.links_submitted.connect(self._submit_bulk_links)
            window.bulk_dialog.playlist_requested.connect(self.open_playlist_selection)
        window.bulk_dialog.open()

    def open_playlist_selection(self, url):
        window = self.window
        if window._closing:
            return
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('Enter one playlist URL to choose which videos to download.')
            if not window.folder or not os.path.isdir(window.folder):
                raise ValueError('Choose an existing download folder first.')
            options = TaskOptions(folder=window.folder, **window.output_controls.values(),
                **window.output_controls.trim_values())
            validate_cookie_options(window.output_controls.values(), check_file=True)
        except ValueError as error:
            window._show_message('warning', 'Choose playlist videos', str(error), '')
            return
        if window.playlist_dialog is not None:
            window.playlist_dialog.reject()
            window.playlist_dialog.deleteLater()
        parent = window.bulk_dialog if window.bulk_dialog is not None and window.bulk_dialog.isVisible() else window
        window.playlist_dialog = PlaylistSelectionDialog(window.controller.manager, url, options, parent)
        window.playlist_dialog.queued.connect(self._playlist_queued)
        window.playlist_dialog.open()
        window.playlist_dialog.load()

    def _playlist_queued(self, url, added, skipped):
        window = self.window
        if window.bulk_dialog is not None and window.bulk_dialog.links() == [url]:
            window.bulk_dialog.links_edit.clear()
            window.bulk_dialog.accept()
        window.controller.poll()
        window._show_notice(tr('{added} selected videos queued; {skipped} already queued or repeated.', added=added, skipped=skipped))

    def open_source_formats(self):
        window = self.window
        if window._closing:
            return
        url = window.url_edit.text().strip()
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('Enter a single video URL to inspect its formats.')
            if not window.folder or not os.path.isdir(window.folder):
                raise ValueError('Choose an existing download folder first.')
            options = TaskOptions(folder=window.folder, **window.output_controls.values(),
                **window.output_controls.trim_values())
            validate_cookie_options(window.output_controls.values(), check_file=True)
        except ValueError as error:
            window._show_message('warning', 'Source formats', str(error), '')
            return
        if window.source_dialog is not None:
            window.source_dialog.reject()
            window.source_dialog.deleteLater()
        window.source_dialog = SourceFormatsDialog(window.controller.manager, url, options, window)
        window.source_dialog.queued.connect(self._source_queued)
        window.source_dialog.open()
        window.source_dialog.load()

    def _source_queued(self, url):
        window = self.window
        if window.url_edit.text().strip() == url:
            window.url_edit.clear()
        window.controller.poll()
        window._show_notice('Selected source formats added to the queue.')

    def _submit_links(self, links, *, expand_playlist):
        window = self.window
        if window._closing:
            return False
        if not links:
            self._validation_warning("Missing URL", "Enter a video URL first.")
            return False
        if not window.folder or not os.path.isdir(window.folder):
            self._validation_warning("Download folder", "Choose an existing download folder first.")
            return False
        try:
            validate_output_options(window.output_controls.values())
        except ValueError as error:
            self._validation_warning("Invalid output profile", str(error))
            return False
        try:
            validate_cookie_options(window.output_controls.values(), check_file=True)
        except ValueError as error:
            self._validation_warning('Cookie settings', str(error))
            return False
        try:
            validate_subtitle_options(window.output_controls.values())
        except ValueError as error:
            self._validation_warning('Subtitle settings', str(error))
            return False
        try:
            validate_network_options(window.output_controls.values())
        except ValueError as error:
            self._validation_warning('Network settings', str(error))
            return False
        try:
            validate_naming_options(window.output_controls.values())
        except ValueError as error:
            self._validation_warning('File and folder naming', str(error))
            return False
        try:
            return window.controller.submit_links(
                links, expand_playlist=expand_playlist, folder=window.folder,
                **window.output_controls.values(),
                **window.output_controls.trim_values())
        except ValueError as error:
            self._validation_warning(
                "Invalid trim range",
                "Use seconds, mm:ss or hh:mm:ss (for example, 90 or 1:30). "
                "Values must be non-negative, and the end must be later than the start.", str(error))
            return False

    def _validation_warning(self, title, text, details=''):
        window = self.window
        if title in ('Invalid trim range', 'Invalid output profile', 'Cookie settings', 'Subtitle settings', 'Network settings', 'File and folder naming'):
            window.output_controls.advanced_toggle.setChecked(True)
        log_warning(title, text, [('details', details)],
                    once_key=('input_validation', title, text, details),
                    event='input_validation_failed', component='qt')
        window._show_message('warning', title, text, details)

    def enqueue(self):
        window = self.window
        url = window.url_edit.text().strip()
        if self._submit_links([url] if url else [], expand_playlist=False):
            window.url_edit.clear()

    def _submit_bulk_links(self, links):
        window = self.window
        if self._submit_links(links, expand_playlist=True):
            window.bulk_dialog.links_edit.clear()
            window.bulk_dialog.accept()

    def cancel_selected(self):
        window = self.window
        tids = window.task_table.selected_task_ids()
        if not tids:
            window._show_message("information", "Select a task", "Select the tasks you want to cancel.", "")
            return
        self._cancel_tasks(tids)

    def _cancel_tasks(self, tids):
        window = self.window
        if window._closing:
            return
        cancellable = [tid for tid in tids if (task := window.controller.manager.get_task(tid))
                       and state_of(task) not in STOPPED_STATES]
        if len(cancellable) > 1:
            box = QMessageBox(window)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Cancel tasks")
            box.setText(tr('Cancel {count} selected tasks?', count=len(cancellable)))
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.finished.connect(lambda result: window.controller.cancel_tasks(cancellable)
                                 if result == QMessageBox.StandardButton.Yes else None)
            window._open_message(box)
        else:
            window.controller.cancel_tasks(cancellable)

    def retry_failed(self):
        window = self.window
        window.controller.retry_failed(window.task_table.selected_task_ids())

    def clear_list(self):
        window = self.window
        if window._closing:
            return
        pending = window.controller.manager.summary().pending > 0
        if not pending:
            window.controller.clear_tasks()
            return
        box = QMessageBox(window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Clear task list")
        box.setText(tr("Remove queued tasks as well?\n\n"
                    "Yes: clear finished and queued tasks.\n"
                    "No: clear only completed, failed and cancelled tasks.\n"
                    "Active downloads and saved files are kept."))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                               QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.finished.connect(lambda result: window.controller.clear_tasks(
            remove_pending=result == QMessageBox.StandardButton.Yes)
            if result in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No) else None)
        window._open_message(box)

    def _refresh_actions(self):
        window = self.window
        if window._closing:
            return
        manager = window.controller.manager
        summary = manager.summary()
        selected_ids = window.task_table.selected_task_ids()
        selected = [task for tid in selected_ids if (task := manager.get_task(tid)) is not None]
        window.cancel_action.setEnabled(any(state_of(t) not in STOPPED_STATES
                                          for t in selected))
        retryable = (any(state_of(t) == TaskState.FAILED and manager.task_control(t["id"])[1] is None
                         for t in selected) if selected_ids else summary.retryable > 0)
        window.retry_action.setEnabled(retryable)
        window.retry_action.setText(tr('Retry selected failed tasks' if selected_ids else 'Retry all failed tasks'))
        window.show_file_action.setEnabled(len(selected) == 1 and
                                         state_of(selected[0]) == TaskState.COMPLETED and
                                         bool(selected[0].get("output_path")))
        window.clear_button.setEnabled(summary.clearable > 0)
        window.pause_button.setEnabled(summary.total > 0 or manager.paused)
        window.diagnostics_action.setEnabled(len(selected) == 1 and
                                           (state_of(selected[0]) == TaskState.FAILED or bool(selected[0].get('warnings'))))

    def _activate_task(self, index):
        window = self.window
        tid = index.data(Qt.ItemDataRole.UserRole)
        task = window.controller.manager.get_task(tid)
        if task and state_of(task) == TaskState.COMPLETED:
            self.show_file(tid)
        elif task:
            self.show_task_details(tid)

    def show_selected_file(self):
        window = self.window
        tids = window.task_table.selected_task_ids()
        if len(tids) == 1:
            self.show_file(tids[0])

    def show_selected_diagnostics(self):
        window = self.window
        tids = window.task_table.selected_task_ids()
        if len(tids) == 1:
            self.show_task_details(tids[0])

    def show_file(self, tid):
        window = self.window
        if window._closing:
            return
        task = window.controller.manager.get_task(tid)
        if not task or state_of(task) != TaskState.COMPLETED:
            return
        try:
            reveal_file(task.get("output_path"))
        except OSError as error:
            log_warning("Could not reveal output file", error, [("task", tid)])
            window._show_message("warning", "Could not show file",
                               "The file may have been moved or deleted, or the file manager could not start.",
                               str(error))

    def show_task_details(self, tid):
        window = self.window
        task = window.controller.manager.get_task(tid)
        if task is None:
            return
        details = "\n".join(filter(None, [tr('Task: {value}', value=tid), tr('Attempt: {value}', value=task.get('attempt', 0)),
                                         tr('Stage: {value}', value=task.get('_log_stage', 'resolve')),
                                         task.get("status", ""), *task.get("warnings", [])]))
        if state_of(task) == TaskState.FAILED or task.get("warnings"):
            window._show_message("warning", "Task diagnostics",
                               f"Diagnostic details for {tid}. Engine and provider messages are shown verbatim. "
                               "Use the diagnostic actions to inspect or share the error context.", details)
