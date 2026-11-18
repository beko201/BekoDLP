"""Automate a real Qt window using local media and a local HTTP server.

Example: python tools/qt_smoke.py --platform windows --screenshot preview.png
Use --platform offscreen for environments without a desktop.
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", help="Qt platform plugin, e.g. windows or offscreen")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--layout-preview-dir", type=Path, help="Save collapsed and expanded layout previews")
    parser.add_argument('--language', choices=('en', 'tr'), default='en')
    args = parser.parse_args()
    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from settings import SettingsStore
    from task_manager import TaskManager
    from ui.exception_reporter import QtExceptionReporter
    from ui.main_window import MainWindow
    from ui.theme import apply_theme
    from ui.i18n import tr
    from utils import NO_WINDOW
    import utils

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    if app.platformName() == "offscreen" and Path("C:/Windows/Fonts/segoeui.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    apply_theme(app)
    reporter = QtExceptionReporter(app)
    unexpected = []
    reporter.message.connect(lambda *message: unexpected.append(message))
    reporter.install()

    def wait_until(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        raise AssertionError("Native Qt flow timed out")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    try:
        with tempfile.TemporaryDirectory(prefix="yd-qt-smoke-") as folder, \
                patch.object(utils, 'ERROR_LOG_FILE', str(Path(folder, 'errors.log'))), \
                patch.object(utils, 'DEBUG_LOG_FILE', str(Path(folder, 'debug.log'))), \
                patch.object(utils, 'FALLBACK_DEBUG_LOG_FILE', str(Path(folder, 'debug-fallback.log'))), \
                patch.object(utils, 'FALLBACK_ERROR_LOG_FILE', str(Path(folder, 'fallback.log'))):
            root = Path(folder)
            target = root / "downloads"
            target.mkdir()
            source = root / "tone.mp3"
            subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", str(source)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
            shutil.copyfile(source, root / "second.mp3")
            settings_path = root / "settings.json"
            SettingsStore(settings_path).save(dict(last_folder=str(target), format="MP3", workers=2, language=args.language))
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=folder))
            server.daemon_threads = True
            server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
            server_thread.start()
            manager = TaskManager()
            window = MainWindow(manager, settings_path=settings_path)
            reporter.message.connect(window._show_message, Qt.ConnectionType.QueuedConnection)
            try:
                window.show()
                window.format_combo.setCurrentIndex(window.format_combo.findData('MP3'))
                window.output_controls.audio_format_combo.setCurrentIndex(window.output_controls.audio_format_combo.findData("mp3"))
                window.bitrate_combo.setCurrentIndex(window.bitrate_combo.findData('auto'))
                if app.platformName() != "offscreen":
                    assert QTest.qWaitForWindowExposed(window, 5000), "Native window was not exposed"
                if args.layout_preview_dir:
                    args.layout_preview_dir.mkdir(parents=True, exist_ok=True)
                    # Capture the initial video layout before exercising audio output.
                    window.format_combo.setCurrentIndex(window.format_combo.findData('MP4'))
                    window.audio_codec_combo.setCurrentIndex(window.audio_codec_combo.findData('original'))
                    app.processEvents()
                    assert window.grab().save(str(args.layout_preview_dir / 'simple.png'))
                    window.output_controls.advanced_toggle.setChecked(True)
                    app.processEvents()
                    assert window.grab().save(str(args.layout_preview_dir / 'advanced.png'))
                    window.format_combo.setCurrentIndex(window.format_combo.findData('MP3'))
                    window.output_controls.audio_format_combo.setCurrentIndex(window.output_controls.audio_format_combo.findData('mp3'))
                    window.output_controls.advanced_toggle.setChecked(False)
                    app.processEvents()
                    assert window.grab().save(str(args.layout_preview_dir / 'audio.png'))
                if not window.output_controls.advanced_toggle.isChecked():
                    QTest.mouseClick(window.output_controls.advanced_toggle, Qt.MouseButton.LeftButton)
                assert window.output_controls.advanced_widget.isVisible()
                help_button = window.output_controls.help_buttons['FFmpeg threads']
                window.options_scroll.ensureWidgetVisible(help_button)
                QTest.mouseClick(help_button, Qt.MouseButton.LeftButton)
                popup = app.activePopupWidget()
                assert popup is not None, 'Field help did not open'
                app.processEvents()
                if args.layout_preview_dir:
                    assert popup.grab().save(str(args.layout_preview_dir / 'field-help.png'))
                QTest.keyClick(popup, Qt.Key.Key_Escape)
                assert app.activePopupWidget() is None, 'Field help did not close with Escape'
                url = f"http://127.0.0.1:{server.server_port}/tone.mp3"
                QTest.keyClicks(window.trim_start_edit, "0.1")
                QTest.keyClicks(window.trim_end_edit, "0.6")
                QTest.mouseClick(window.output_controls.advanced_toggle, Qt.MouseButton.LeftButton)
                QTest.keyClicks(window.url_edit, url)
                QTest.mouseClick(window.enqueue_button, Qt.MouseButton.LeftButton)

                def completed(count):
                    tasks = manager.list_tasks()
                    return len(tasks) == count and not manager.tid_to_workerid and all(
                        task["status"].startswith(("Completed", "Error")) for task in tasks)

                wait_until(lambda: completed(1))
                assert manager.get_task("T1")["status"].startswith("Completed"), manager.list_tasks()
                QTest.mouseClick(window.bulk_button, Qt.MouseButton.LeftButton)
                dialog = window.bulk_dialog
                QTest.keyClicks(dialog.links_edit, url)
                QTest.keyClick(dialog.links_edit, Qt.Key.Key_Return)
                QTest.keyClicks(dialog.links_edit, url.replace("tone.mp3", "second.mp3"))
                QTest.mouseClick(dialog.submit_button, Qt.MouseButton.LeftButton)
                wait_until(lambda: completed(3))
                window.controller.poll()
                assert all(task["status"].startswith("Completed") for task in manager.list_tasks()), manager.list_tasks()
                assert window.global_progress.value() == 100
                outputs = [Path(task["output_path"]) for task in manager.list_tasks()]
                assert len(set(outputs)) == 3
                for output in outputs:
                    result = subprocess.run([manager.engine.ffprobe, "-v", "error", "-show_entries",
                                             "format=duration", "-of", "csv=p=0", str(output)],
                                            check=True, capture_output=True, text=True, **NO_WINDOW)
                    assert .2 < float(result.stdout.strip()) < 1.5

                table = window.task_table
                table.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
                table.view.selectRow(0)
                app.processEvents()
                with patch("ui.main_window.reveal_file") as reveal:
                    menu = window.task_actions_menu
                    def choose_file_action():
                        menu.setActiveAction(window.show_file_action)
                        QTest.keyClick(menu, Qt.Key.Key_Return)
                    QTimer.singleShot(50, choose_file_action)
                    window.task_actions_button.showMenu()
                    wait_until(lambda: reveal.called)
                    assert reveal.call_args.args[0] == manager.get_task("T3")["output_path"]
                if args.layout_preview_dir:
                    window.resize(900, 680)
                    window.output_controls.advanced_toggle.setChecked(True)
                    for _ in range(3):
                        app.processEvents()
                    assert window.grab().save(str(args.layout_preview_dir / 'compact-preview.png'))
                    window.output_controls.advanced_toggle.setChecked(False)
                    window.resize(1220, 800)
                # Validate an actual warning dialog through the native widget path.
                QTest.mouseClick(window.enqueue_button, Qt.MouseButton.LeftButton)
                wait_until(lambda: bool(window._message_boxes))
                box = next(iter(window._message_boxes))
                assert box.windowTitle() == tr("Missing URL")
                QTest.mouseClick(box.button(QMessageBox.StandardButton.Ok), Qt.MouseButton.LeftButton)
                if args.screenshot:
                    app.processEvents()
                    assert window.grab().save(str(args.screenshot.resolve()))
                window.bitrate_combo.setCurrentText("256k")
                window.close()
                wait_until(lambda: not window.isVisible())
                saved = json.loads(settings_path.read_text(encoding="utf-8"))
                assert saved["mp3_bitrate"] == "256k"
                assert manager.is_idle() and not manager.engine.managed_processes
                assert all(output.is_file() for output in outputs)
                assert not unexpected, unexpected
                print(f"Qt platform: {app.platformName()}")
                print("PASS: widget input, local yt-dlp downloads, real MP3 conversion/trim, bulk input,")
                print("      sorted selection, file action, validation dialog, settings flush and shutdown")
            finally:
                window.close()
                wait_until(manager.is_idle)
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=1)
    finally:
        reporter.restore()


if __name__ == "__main__":
    main()
