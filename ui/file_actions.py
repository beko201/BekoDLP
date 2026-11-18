"""Reveal a completed output without invoking a shell or opening the media."""
import os
import subprocess
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def open_folder(path):
    if not os.path.isdir(path):
        raise FileNotFoundError('The log folder does not exist.')
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path))):
        raise OSError('The file manager could not open the log folder.')


def reveal_file(path):
    if not path or not os.path.isfile(path):
        raise FileNotFoundError("The downloaded file was moved or deleted.")
    path = os.path.abspath(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer.exe", "/select,", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif not QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path))):
        raise OSError("The file manager could not open the download folder.")
