"""Regression for compressed advanced controls with a selected preview."""
import os
from itertools import product
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QLabel

from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from ui.theme import apply_theme
from ui.i18n import set_language


class OptionsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_preview_resize_and_mode_changes_never_compress_or_overlap_fields(self):
        old_style = self.app.style().objectName()
        old_font, old_palette, old_sheet = self.app.font(), self.app.palette(), self.app.styleSheet()
        self.addCleanup(self.app.setStyleSheet, old_sheet)
        self.addCleanup(self.app.setPalette, old_palette)
        self.addCleanup(self.app.setFont, old_font)
        self.addCleanup(self.app.setStyle, old_style)
        apply_theme(self.app)
        with tempfile.TemporaryDirectory() as folder:
            manager = TaskManager(engine_factory=lambda manager: Mock())
            with patch.object(manager, 'set_worker_count'):
                window = MainWindow(manager, settings_path=Path(folder, 'settings.json'))
            try:
                window.show()
                tid = manager.add_urls(['https://example.com/layout'], TaskOptions(folder=folder))[0]
                manager.update_task(tid, title='Long preview title with multiple wrapped lines ' * 12,
                                    status='Completed', progress=100, output_path='offline.mp3')
                window.controller.poll()
                window.task_table.view.selectRow(0)
                controls = window.output_controls
                saw_scrollbar = False
                for code, (width, height) in product(('en', 'tr'), ((1220, 800), (900, 680), (1220, 800))):
                    window.language_actions[code].trigger()
                    window.resize(width, height)
                    for codec in (None, 'mp3', 'flac'):
                        controls.format_combo.setCurrentIndex(controls.format_combo.findData('MP4' if codec is None else 'MP3'))
                        if codec:
                            controls.audio_format_combo.setCurrentIndex(controls.audio_format_combo.findData(codec))
                        for expanded in (True, False, True):
                            controls.advanced_toggle.setChecked(expanded)
                            for _ in range(5):
                                self.app.processEvents()
                            if not expanded:
                                continue
                            with self.subTest(language=code, size=(width, height), codec=codec):
                                self.assertTrue(window.preview_panel.isVisible())
                                grid = controls.advanced_widget.layout()
                                widgets = [grid.itemAt(i).widget() for i in range(grid.count())
                                           if grid.itemAt(i).widget().isVisible()]
                                for widget in widgets:
                                    self.assertTrue(controls.advanced_widget.rect().contains(widget.geometry()),
                                                    str(widget.geometry()))
                                    if isinstance(widget, QLabel):
                                        required = widget.heightForWidth(widget.width())
                                        self.assertGreaterEqual(widget.height(), max(widget.fontMetrics().height(), required),
                                                                widget.text())
                                    self.assertGreaterEqual(widget.height(), widget.minimumHeight())
                                    for label in widget.findChildren(QLabel):
                                        if label.isVisible():
                                            self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()),
                                                                    label.text())
                                for index, widget in enumerate(widgets):
                                    for other in widgets[index + 1:]:
                                        self.assertFalse(widget.geometry().intersects(other.geometry()))
                                self.assertGreaterEqual(controls.trim_start_edit.height(),
                                                        controls.trim_start_edit.minimumHeight())
                                scrollbar = window.options_scroll.verticalScrollBar()
                                saw_scrollbar |= scrollbar.maximum() > 0
                                scrollbar.setValue(scrollbar.maximum())
                                self.app.processEvents()
                                viewport = window.options_scroll.viewport()
                                bottom = controls.hint.mapTo(viewport, controls.hint.rect().bottomLeft())
                                self.assertTrue(viewport.rect().contains(bottom), str(bottom))
                self.assertTrue(saw_scrollbar)
            finally:
                window.close()
                window.controller.poll()
                self.app.processEvents()
                set_language('en')
