"""Asynchronous window shutdown, final persistence and update handoff."""
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMessageBox

from ui.i18n import tr
from app_logging import log_warning


class WindowShutdown(QObject):
    """Own close state and wait for worker cleanup and durable queue saving."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.closing = False
        self.allow_close = False
        self.update_on_close = False
        self.preferences_closed = False
        self.queue_closed = False

    def handle_close(self, event):
        window = self.window
        if self.allow_close:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        if self.update_on_close:
            manager = window.controller.manager
            if manager.has_update_work():
                self.update_on_close = False
                window.updater._info('Finish or cancel pending downloads before installing the update. The verified update is ready for this session.')
                return
            # Flushing may show a save warning. Keep preferences usable if
            # installation is deferred rather than closing the application.
            if not self.preferences_closed and not window.preferences.flush():
                self.update_on_close = False
                return
            # Admission and the idle check share the task lock. A resolver or
            # another producer cannot insert work between this check and close.
            if not manager.close(require_idle=True):
                self.update_on_close = False
                window.updater._info('Finish or cancel pending downloads before installing the update. The verified update is ready for this session.')
                return
        if not self.preferences_closed:
            self.preferences_closed = True
            if not window.preferences.close():
                # Keep the save error visible until acknowledged, then resume
                # the ordinary asynchronous shutdown without retrying forever.
                warning = next((box for box in window._message_boxes
                                if box.property('_message_key') == "Preferences warning"), None)
                if warning is None:
                    window._show_message("warning", "Preferences warning",
                                       "Your recent preferences could not be saved. "
                                       "Click OK to continue closing.", "Use the log file location below for details.")
                    warning = next(box for box in window._message_boxes
                                   if box.property('_message_key') == "Preferences warning")
                warning.finished.connect(lambda result: window.close())
                return
        self.closing = True
        window.finish_actions.close()
        if window.playlist_dialog is not None:
            window.playlist_dialog.reject()
        if window.source_dialog is not None:
            window.source_dialog.reject()
        window.queue_persistence.timer.stop()
        window._pending_messages.clear()
        window.updater.close(installing=self.update_on_close)
        window.tool_updater.close()
        window.thumbnails.close()
        window.centralWidget().setEnabled(False)
        if window.bulk_dialog is not None:
            window.bulk_dialog.reject()
        for box in list(window._message_boxes):
            box.reject()
        window._show_notice("Closing — cancelling tasks and cleaning up…", 0)
        window.controller.begin_shutdown()

    def schedule_finish(self):
        # finish_shutdown() may succeed inside closeEvent itself. Defer the
        # accepted close until that ignored event has returned to Qt.
        QTimer.singleShot(0, self.finish)

    def finish(self):
        window = self.window
        if self.allow_close:
            return
        if not self.queue_closed:
            saved = window.queue_persistence.close()
            if saved is None:
                return
            self.queue_closed = True
            if not saved:
                warning = next((box for box in window._message_boxes
                                if box.property('_message_key') == 'Queue warning'), None)
                if warning is None:
                    window._show_message('warning', 'Queue warning',
                        'Your queue could not be saved. Recent changes may not be restored next time.', '')
                    warning = next(box for box in window._message_boxes
                                   if box.property('_message_key') == 'Queue warning')
                warning.finished.connect(lambda result: self.finish())
                return
        if self.update_on_close:
            try:
                window.updater.apply_on_exit()
            except (OSError, ValueError) as error:
                # The old executable is untouched if the helper cannot start.
                self.update_on_close = False
                window.updater.close()
                log_warning('Could not start update helper', error, event='update_failed', component='updater')
                box = QMessageBox(window)
                box.setWindowTitle(tr('Software update'))
                box.setText(tr('The update could not be completed. Your current version is unchanged.'))
                box.setDetailedText(str(error))
                box.finished.connect(lambda result: self.finish())
                window._open_message(box)
                return
        self.allow_close = True
        for box in list(window._message_boxes):
            box.reject()
        window.close()

    def ask_choice(self):
        window = self.window
        box = QMessageBox(window)
        box.setWindowTitle("Some tasks have not stopped")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(tr(
            "Cancellation and process termination have timed out. Close now?\n\n"
            "Completed files are preserved, but partial files may remain.\n\n"
            "Choose No to wait another {seconds} seconds.", seconds=f'{window.controller.manager.shutdown_grace_seconds:g}'))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.finished.connect(self.resolve_choice)
        window._open_message(box)

    def resolve_choice(self, result):
        window = self.window
        if not self.allow_close:
            window.controller.resolve_shutdown_choice(result == QMessageBox.StandardButton.Yes)
