"""Start the PySide6 downloader: python main.py."""
import sys
import traceback
from app_info import APP_NAME, APP_VERSION
from diagnostics import initialize_runtime_diagnostics

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from ui.main_window import MainWindow
from ui.theme import apply_theme
from ui.exception_reporter import QtExceptionReporter
from ui.diagnostics import add_diagnostic_buttons, open_log_folder, export_diagnostic_report
from task_manager import TaskManager
from utils import log_error, flush_warning_summaries, log_location_text
from utils import SETTINGS_FILE
from ui.i18n import register, set_language, startup_language, tr


def _startup_failure(error, message):
    log_error("Qt application startup failed", details=[("error", error)], tb=traceback.format_exc(),
              event='startup_failed', component='application')
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(tr("Startup failed"))
    box.setText(tr(message))
    details = traceback.format_exc()
    box.setDetailedText(details)
    box.setInformativeText(tr(log_location_text()))
    def perform(action):
        try:
            action()
        except (OSError, ValueError) as action_error:
            QMessageBox.warning(box, tr('Diagnostic action failed'), str(action_error))
    add_diagnostic_buttons(box, f'{message}\n\n{details}\n\n{log_location_text()}',
                           lambda: perform(open_log_folder),
                           lambda: perform(lambda: export_diagnostic_report(box, details)))
    register(box)
    box.exec()
    return 1


def main():
    QLocale.setDefault(QLocale("en_US"))
    app = QApplication(sys.argv)
    set_language(startup_language(SETTINGS_FILE))
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_NAME)
    apply_theme(app)
    reporter = QtExceptionReporter(app)
    reporter.install()
    manager = None
    window = None
    try:
        try:
            manager = TaskManager()
        except RuntimeError as error:
            initialize_runtime_diagnostics()
            return _startup_failure(error, "The download engine could not start. Install the project dependencies "
                                    "and make sure ffmpeg and ffprobe are available.")
        initialize_runtime_diagnostics(manager.engine.ffmpeg, manager.engine.ffprobe)
        try:
            window = MainWindow(manager)
        except Exception as error:
            return _startup_failure(error, "The interface could not start. Use the diagnostic actions for details.")
        reporter.message.connect(window._show_message, Qt.ConnectionType.QueuedConnection)
        window.show()
        return app.exec()
    finally:
        reporter.restore()
        if manager is not None and window is None:
            manager.close()
        flush_warning_summaries()


if __name__ == "__main__":
    raise SystemExit(main())
