"""Language changes must not change task options, identity or user-provided text."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from pathlib import Path
import tempfile
from string import Formatter
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from settings import DEFAULTS, MAX_SETTINGS_BYTES, SettingsStore, validate_settings
from task_manager import TaskManager, TaskOptions
from ui.i18n import TR, language, set_language, startup_language, tr
from ui.main_window import MainWindow
from qt_support import finish_window_close


class LanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        set_language('en')
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            for box in list(window._message_boxes):
                box.done(QMessageBox.StandardButton.Ok)
            finish_window_close(window)
            window.controller.poll()
        for _ in range(4):
            self.app.processEvents()
        set_language('en')
        self.temp.cleanup()

    def window(self, path=None):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=path or self.folder / 'settings.json')
        self.windows.append(window)
        window.show()
        return window

    def test_english_default_and_language_validation(self):
        self.assertEqual(DEFAULTS['language'], 'en')
        data, _ = validate_settings({})
        self.assertEqual(data['language'], 'en')
        for value in ('unknown', None, True, ['tr']):
            recovered, issues = validate_settings({'language': value})
            self.assertEqual(recovered['language'], 'en')
            self.assertIn('language', issues)
        window = self.window()
        self.assertEqual(window.windowTitle(), 'BekoDLP')
        self.assertTrue(window.language_actions['en'].isChecked())

    def test_translation_templates_keep_the_same_named_parameters(self):
        for source, translated in TR.items():
            with self.subTest(source=source):
                fields = lambda value: {name for _, name, _, _ in Formatter().parse(value) if name}
                self.assertEqual(fields(source), fields(translated))

    def test_live_translation_preserves_options_task_identity_selection_and_titles(self):
        window = self.window()
        controls = window.output_controls
        controls.format_combo.setCurrentIndex(controls.format_combo.findData('MP3'))
        controls.audio_format_combo.setCurrentIndex(controls.audio_format_combo.findData('mp3'))
        controls.bitrate_combo.setCurrentIndex(controls.bitrate_combo.findData('auto'))
        window.trim_start_edit.setText('1:30')
        window.trim_end_edit.setText('2:45')
        before = controls.values()
        manager = window.controller.manager
        tid = manager.add_urls(['https://example.com/Pause'], TaskOptions(folder=str(self.folder), **before))[0]
        manager.update_task(tid, title='Pause')
        manager.report_stage_progress(tid, 'downloading', .1, status='Downloading')
        window.controller.poll()
        window.task_table.view.selectRow(0)
        model = window.task_table.model
        persistent = QPersistentModelIndex(model.index(0, 0))
        original = manager.get_task(tid)
        window.language_actions['tr'].trigger()
        self.assertEqual(language(), 'tr')
        self.assertEqual(window.windowTitle(), 'BekoDLP')
        self.assertEqual(controls.selection_label.text(), 'Ses biçimi')
        self.assertEqual(controls.bitrate_combo.currentText(), 'Otomatik')
        self.assertIn('Kırpma: 1:30 – 2:45', controls.summary_label.text())
        self.assertEqual(model.headerData(4, Qt.Orientation.Horizontal), 'Durum')
        self.assertEqual(model.index(0, 4).data(), 'İndiriliyor')
        self.assertEqual(model.index(0, 1).data(), 'Pause')
        self.assertEqual(window.preview_panel.title_label.text(), 'Pause')
        self.assertEqual(window.task_table.selected_task_ids(), [tid])
        self.assertTrue(persistent.isValid())
        self.assertEqual(controls.values(), before)
        self.assertEqual(manager.get_task(tid), original)
        manager.toggle_pause()
        window.controller.poll()
        self.assertEqual(window.pause_button.text(), 'Devam et')
        window.language_actions['en'].trigger()
        self.assertEqual(controls.selection_label.text(), 'Audio format')
        self.assertEqual(window.pause_button.text(), 'Resume')
        self.assertEqual(model.index(0, 4).data(), 'Downloading')
        self.assertEqual(controls.values(), before)

    def test_language_is_saved_and_restored_without_changing_output_choices(self):
        window = self.window()
        window.language_actions['tr'].trigger()
        before = window.output_controls.values()
        window.preferences.flush()
        path = self.folder / 'settings.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['language'], 'tr')
        restored = self.window(path)
        self.assertEqual(restored.windowTitle(), 'BekoDLP')
        self.assertEqual(restored.output_controls.values(), before)
        self.assertTrue(restored.language_actions['tr'].isChecked())

    def test_dialogs_bulk_count_and_standard_buttons_are_turkish(self):
        window = self.window()
        window.language_actions['tr'].trigger()
        window.open_bulk_dialog()
        window.bulk_dialog.links_edit.setPlainText('https://example.com/a\nhttps://example.com/b')
        self.assertEqual(window.bulk_dialog.count_label.text(), '2 bağlantı')
        self.assertEqual(window.bulk_dialog.submit_button.text(), 'Kuyruğa ekle')
        window.bulk_dialog.reject()
        window.enqueue()
        box = next(iter(window._message_boxes))
        self.assertEqual(box.windowTitle(), 'Bağlantı eksik')
        self.assertEqual(box.text(), 'Önce bir video bağlantısı girin.')
        self.assertIn('Tamam', box.button(QMessageBox.StandardButton.Ok).text())
        self.assertEqual(tr('Resolving 3 link(s)…'), '3 bağlantı çözümleniyor…')

    def test_turkish_preferences_failure_can_be_acknowledged_to_close(self):
        window = self.window()
        window.language_actions['tr'].trigger()
        with patch.object(window.preferences, 'close', return_value=False):
            window.close()
            box = next(iter(window._message_boxes))
            self.assertEqual(box.windowTitle(), 'Tercih uyarısı')
            box.done(QMessageBox.StandardButton.Ok)
            finish_window_close(window)
        self.assertTrue(window._allow_close)

    def test_startup_language_read_is_bounded_read_only_and_rejects_bad_documents(self):
        path = self.folder / 'startup.json'
        SettingsStore(path).save({'language': 'tr'})
        before = path.read_bytes()
        self.assertEqual(startup_language(path), 'tr')
        self.assertEqual(path.read_bytes(), before)
        for content in ('{"language":"en","language":"tr"}', '{broken',
                        '{"schema_version":true,"language":"tr"}',
                        '{"schema_version":999,"language":"tr"}', 'x' * (MAX_SETTINGS_BYTES + 1)):
            path.write_text(content, encoding='utf-8')
            self.assertEqual(startup_language(path), 'en')
            self.assertEqual(path.read_text(encoding='utf-8'), content)
