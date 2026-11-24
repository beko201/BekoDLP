"""Profile validation, durable Qt actions and task option isolation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from saved_profiles import (PROFILE_KEYS, builtin_profiles, capture_profile, profile_options,
                            save_profile, validate_profiles)
from settings import DEFAULTS, validate_settings
from task_manager import TaskManager, TaskOptions
from ui.i18n import set_language
from ui.main_window import MainWindow
from qt_support import finish_window_close
from ui.output_controls import OutputControls


class SavedProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        set_language('en')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        self.windows = []
        self.controls = OutputControls()

    def tearDown(self):
        for window in self.windows:
            for box in list(window._message_boxes):
                box.done(QMessageBox.StandardButton.Ok)
            finish_window_close(window)
            window.controller.poll()
        self.controls.close()
        self.app.processEvents()
        set_language('en')

    def window(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        with patch.object(manager, 'set_worker_count'):
            window = MainWindow(manager, settings_path=self.path)
        self.windows.append(window)
        return window, manager

    def choose(self, controls, key):
        controls.profiles.combo.setCurrentIndex(controls.profiles.combo.findData(key))
        controls.profiles.apply_selected()

    def test_builtin_profiles_are_valid_and_apply_with_one_selection(self):
        for profile in builtin_profiles():
            with self.subTest(profile=profile['id']):
                profile_options(profile['options'])
                self.choose(self.controls, profile['id'])
                self.assertEqual(capture_profile(self.controls.values()), profile['options'])
                self.assertEqual(self.controls.profiles.combo.currentData(), profile['id'])
                self.assertFalse(self.controls.profiles.delete_button.isEnabled())
        self.choose(self.controls, 'builtin:music')
        self.assertEqual((self.controls.mode(), self.controls.values()['audio_codec']), ('audio', 'mp3'))

    def test_save_overwrite_delete_and_restart_restore(self):
        window, _ = self.window()
        controls = window.output_controls
        self.choose(controls, 'builtin:music')
        self.assertTrue(controls.profiles.save_current('Müzik 🎵'))
        key = controls.profiles.combo.currentData()
        self.assertTrue(self.path.is_file())
        document = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(document['saved_profiles'][0]['name'], 'Müzik 🎵')
        controls.bitrate_combo.setCurrentIndex(controls.bitrate_combo.findData('256k'))
        self.assertTrue(controls.profiles.save_current('Müzik 🎵'))
        self.assertEqual(len(controls.profiles.profiles()), 1)
        self.assertEqual(controls.profiles.combo.currentData(), key)
        restarted, _ = self.window()
        self.choose(restarted.output_controls, key)
        self.assertEqual(restarted.output_controls.values()['audio_bitrate'], '256k')
        restarted.output_controls.profiles.delete_selected()
        self.assertEqual(json.loads(self.path.read_text())['saved_profiles'], [])
        self.assertEqual(restarted.output_controls.values()['audio_bitrate'], '256k')
        third, _ = self.window()
        self.assertEqual(third.output_controls.profiles.profiles(), [])

    def test_profile_leaves_queue_task_and_context_unchanged(self):
        window, manager = self.window()
        controls = window.output_controls
        controls.restore({**DEFAULTS, 'proxy_mode': 'custom', 'proxy_url': 'http://localhost:8080',
            'cookie_mode': 'browser', 'cookie_browser': 'firefox', 'trim_ranges': '1 - 2',
            'workers': 7, 'ffmpeg_threads': 3})
        controls.trim_start_edit.setText('0.5')
        controls.trim_end_edit.setText('3')
        options = TaskOptions(folder=self.temp.name, **controls.values(), **controls.trim_values())
        tid = manager.add_urls(['https://example.com/original'], options)[0]
        before = manager.get_task(tid)
        worker_count = controls.workers_spin.value()
        self.choose(controls, 'builtin:music')
        after = controls.values()
        self.assertEqual(manager.get_task(tid), before)
        self.assertEqual((after['proxy_url'], after['cookie_browser'], after['trim_ranges']),
                         ('http://localhost:8080', 'firefox', '1 - 2'))
        self.assertEqual(controls.workers_spin.value(), worker_count)
        self.assertEqual(controls.ffmpeg_threads_spin.value(), 3)
        self.assertEqual((controls.trim_start_edit.text(), controls.trim_end_edit.text()), ('0.5', '3'))
        next_options = TaskOptions(folder=self.temp.name, **after, **controls.trim_values())
        self.assertEqual(next_options.audio_codec, 'mp3')
        self.assertEqual(options.output_mode, 'video')
        controls.profiles.save_current('Output only')
        self.assertEqual(set(controls.profiles.profiles()[0]['options']), PROFILE_KEYS)
        self.assertNotIn('cookie_browser', PROFILE_KEYS)

    def test_encoder_or_range_conflict_is_rejected_before_widgets_change(self):
        self.controls.set_encoders({'aac'})
        before = self.controls.values()
        self.choose(self.controls, 'builtin:small')
        self.assertEqual(self.controls.values(), before)
        self.assertIn('unavailable encoder', self.controls.profiles.status.text())
        self.controls.encoders = None
        self.controls.ranges.editor.setPlainText('1 - 2')
        rows, key = save_profile([], 'Split chapters', {**self.controls.values(), 'chapter_mode': 'split'})
        self.controls.profiles.restore(rows)
        before = self.controls.values()
        self.choose(self.controls, key)
        self.assertEqual(self.controls.values(), before)
        self.assertIn('either multiple time ranges', self.controls.profiles.status.text())

    def test_custom_names_survive_live_translation_and_manual_edits_show_custom(self):
        self.controls.profiles.save_current('Music (preset)')
        key = self.controls.profiles.combo.currentData()
        original = self.controls.profiles.profiles()
        set_language('tr')
        self.assertEqual(self.controls.profiles.combo.itemText(self.controls.profiles.combo.findData(key)), 'Music (preset)')
        self.assertEqual(self.controls.profiles.profiles(), original)
        self.assertEqual(self.controls.profiles.combo.itemText(1), 'Müzik (hazır)')
        self.assertEqual(self.controls.profiles.combo.currentData(), key)
        self.controls.quality_combo.setCurrentText('720p')
        self.assertEqual(self.controls.profiles.combo.currentData(), '')
        self.assertEqual(self.controls.profiles.profiles(), original)

    def test_name_dialog_is_nonblocking_and_saves_accepted_name(self):
        self.controls.profiles.prompt_save()
        dialog = self.controls.profiles._name_dialog
        self.assertIsInstance(dialog, QInputDialog)
        dialog.setTextValue('Dialog profile')
        dialog.accept()
        self.app.processEvents()
        self.assertIsNone(self.controls.profiles._name_dialog)
        self.assertEqual(self.controls.profiles.profiles()[0]['name'], 'Dialog profile')

    def test_bad_profile_records_recover_valid_neighbors_and_old_settings_default(self):
        rows, _ = save_profile([], 'Valid', self.controls.values())
        invalid = deepcopy(rows[0])
        invalid['options']['audio_codec'] = 'bad'
        settings, issues = validate_settings({'saved_profiles': [invalid, *rows]})
        self.assertEqual(settings['saved_profiles'], rows)
        self.assertIn('saved_profiles', issues)
        for bad in [None, 'bad', 10, [None]]:
            settings, issues = validate_settings({'saved_profiles': bad})
            self.assertEqual(settings['saved_profiles'], [])
            self.assertIn('saved_profiles', issues)
        self.assertEqual(validate_settings({})[0]['saved_profiles'], [])
        for name in ['', 'x' * 65, 'bad\nname']:
            with self.assertRaises(ValueError):
                save_profile([], name, self.controls.values())
        with self.assertRaises(ValueError):
            validate_profiles([*rows, *rows])
        settings, _ = validate_settings({})
        settings['saved_profiles'].append('local')
        self.assertEqual(DEFAULTS['saved_profiles'], [])

    def test_maximum_profiles_and_case_insensitive_overwrite(self):
        rows = []
        for index in range(30):
            rows, _ = save_profile(rows, f'Profile {index}', self.controls.values())
        with self.assertRaises(ValueError):
            save_profile(rows, 'One more', self.controls.values())
        updated, key = save_profile(rows, 'PROFILE 0', {**self.controls.values(), 'quality': '720p'})
        self.assertEqual(len(updated), 30)
        self.assertEqual(key, rows[0]['id'])
        self.assertEqual(rows[0]['options']['quality'], '1080p')

    def test_write_failure_keeps_disk_and_reports_session_only_profiles(self):
        window, _ = self.window()
        window.output_controls.profiles.save_current('Original')
        before = self.path.read_bytes()
        with patch('settings._atomic_write', side_effect=OSError('disk full')):
            self.assertTrue(window.output_controls.profiles.save_current('Session only'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(window.output_controls.profiles.profiles()), 2)
        self.assertIn('could not be saved to disk', window.output_controls.profiles.status.text())
        self.assertTrue(window.preferences.flush())
