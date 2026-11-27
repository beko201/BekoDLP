"""Shared diagnostic actions for regular and startup error dialogs."""
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from diagnostics import build_diagnostic_report, save_diagnostic_report
from ui.file_actions import open_folder
from app_logging import _log_text, get_log_status
from ui.i18n import tr


def copy_error_details(details):
    QApplication.clipboard().setText(_log_text(details))


def open_log_folder():
    status = get_log_status()
    open_folder(str(Path(status['path'] or status['primary_path']).parent))


def export_diagnostic_report(parent, details='', tasks=()):
    path, _ = QFileDialog.getSaveFileName(parent, tr('Export diagnostic report'),
                                         'video-downloader-diagnostics.txt', tr('Text files (*.txt)'))
    if not path:
        return None
    report = build_diagnostic_report(details, tasks)
    save_diagnostic_report(path, report)
    return path


def add_diagnostic_buttons(box, details, open_callback, export_callback):
    for label, callback in (('Copy error details', lambda: copy_error_details(details)),
                            ('Open log folder', open_callback),
                            ('Export diagnostic report…', export_callback)):
        button = box.addButton(tr(label), QMessageBox.ButtonRole.ActionRole)
        button.clicked.connect(callback)
