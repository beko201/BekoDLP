"""Qt settings migration and persistence without touching user preferences."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from settings import CODECS, DEFAULTS, SettingsStore
from task_manager import TaskManager
from ui.main_window import MainWindow
from ui.preferences import Preferences


class QtPreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name, "settings.json")
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.controller.poll()
        self.app.processEvents()

    def window(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        with patch.object(manager, "set_worker_count") as workers:
            window = MainWindow(manager, settings_path=self.path)
        self.windows.append(window)
        return window, workers

    def test_legacy_preferences_restore_before_workers_and_keep_internal_codec_values(self):
        legacy = dict(last_folder=self.temporary.name, workers=6, format="MP3", quality="720p",
                      mp3_bitrate="320k", codec_mode=CODECS[1], custom="keep")
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        before = self.path.read_bytes()
        window, workers = self.window()
        workers.assert_called_once_with(6)
        self.assertEqual(window.folder_edit.text(), self.temporary.name)
        self.assertEqual(window.format_combo.currentData(), "MP3")
        self.assertEqual(window.audio_codec_combo.currentData(), "mp3")
        self.assertEqual(window.container_combo.currentData(), "mp3")
        self.assertFalse(window.quality_combo.isEnabled())
        self.assertTrue(window.bitrate_combo.isEnabled())
        self.assertEqual(self.path.read_bytes(), before)
        window.format_combo.setCurrentText("Video")
        self.assertTrue(window.preferences.flush())
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["custom"], "keep")
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["output_mode"], "video")
        self.assertEqual(saved["audio_codec"], "mp3")
        self.assertEqual(saved["format"], "MP4")

    def test_changes_are_debounced_and_close_flushes_pending_save(self):
        preferences = Preferences(self.path)
        preferences.load()
        with patch.object(preferences.store, "save", wraps=preferences.store.save) as save:
            for count in (4, 5, 6):
                preferences.schedule({"workers": count})
            save.assert_not_called()
            QTest.qWait(260)
            save.assert_called_once_with({"workers": 6})
            preferences.schedule({"workers": 7})
            preferences.close()
            self.assertEqual(save.call_count, 2)
        self.assertEqual(json.loads(self.path.read_text())["workers"], 7)
        self.assertFalse(preferences.timer.isActive())

    def test_processing_limits_restore_apply_live_and_persist(self):
        self.path.write_text(json.dumps(dict(video_workers=2, ffmpeg_threads=3)), encoding="utf-8")
        manager = TaskManager()
        with patch.object(manager, "set_worker_count"):
            window = MainWindow(manager, settings_path=self.path)
        self.windows.append(window)
        self.assertEqual(window.video_workers_spin.value(), 2)
        self.assertEqual(manager.engine.processing_limits.video_workers, 2)
        self.assertEqual(manager.engine.processing_limits.thread_budget(), 3)
        window.video_workers_spin.setValue(1)
        window.ffmpeg_threads_spin.setValue(4)
        self.assertEqual(manager.engine.processing_limits.video_workers, 1)
        self.assertEqual(manager.engine.processing_limits.thread_budget(), 4)
        window.preferences.flush()
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["video_workers"], saved["ffmpeg_threads"]), (1, 4))

    def test_folder_selection_saves_and_trim_fields_never_persist(self):
        window, _ = self.window()
        with patch("ui.main_window.QFileDialog.getExistingDirectory", return_value=self.temporary.name):
            window.choose_folder()
        window.trim_start_edit.setText("1:00")
        window.trim_end_edit.setText("2:00")
        window.quality_combo.setCurrentText("720p")
        window.close()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["last_folder"], self.temporary.name)
        self.assertEqual(saved["quality"], "720p")
        self.assertNotIn("trim_start", saved)
        self.assertNotIn("trim_end", saved)
        restored, _ = self.window()
        self.assertEqual(restored.quality_combo.currentText(), "720p")
        self.assertEqual(restored.trim_start_edit.text(), "")
        self.assertEqual(restored.trim_end_edit.text(), "")

    def test_network_limits_restore_apply_live_and_persist(self):
        self.path.write_text(json.dumps(dict(fragment_downloads=3, network_connections=6)), encoding="utf-8")
        manager = TaskManager()
        with patch.object(manager, "set_worker_count"):
            window = MainWindow(manager, settings_path=self.path)
        self.windows.append(window)
        limits = manager.engine.network_limits
        self.assertEqual((limits.fragment_downloads, limits.network_connections), (3, 6))
        window.fragment_downloads_spin.setValue(2)
        window.network_connections_spin.setValue(4)
        self.assertEqual((limits.fragment_downloads, limits.network_connections), (2, 4))
        window.preferences.flush()
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["fragment_downloads"], saved["network_connections"]), (2, 4))

    def test_corrupt_primary_recovers_backup_and_preserves_original(self):
        store = SettingsStore(self.path)
        store.save({"workers": 4})
        store.save({"workers": 5})
        self.path.write_text("{broken", encoding="utf-8")
        window, workers = self.window()
        workers.assert_called_once_with(4)
        self.assertEqual(self.path.read_text(), "{broken")
        self.assertEqual(next(self.path.parent.glob("*.corrupt-*")).read_text(), "{broken")
        self.assertEqual(len(window._message_boxes), 1)
        self.assertEqual(next(iter(window._message_boxes)).windowTitle(), "Preferences warning")
        window.format_combo.setCurrentText("Audio")
        self.assertTrue(window.preferences.flush())
        self.assertEqual(json.loads(self.path.read_text())["workers"], 4)

    def test_unsupported_schema_and_read_errors_never_overwrite_original(self):
        self.path.write_text('{"schema_version": 99, "workers": 9}', encoding="utf-8")
        before = self.path.read_bytes()
        window, _ = self.window()
        window.format_combo.setCurrentText("Audio")
        window.close()
        self.assertFalse(window.preferences.store.writable)
        self.assertEqual(self.path.read_bytes(), before)
        preferences = Preferences(self.path)
        messages = []
        preferences.message.connect(lambda *args: messages.append(args))
        with patch("settings._read", side_effect=PermissionError("denied")):
            self.assertEqual(preferences.load(), DEFAULTS)
        preferences.schedule({"workers": 3})
        preferences.close()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(messages), 1)

    def test_failed_save_warns_once_keeps_original_and_can_be_retried(self):
        preferences = Preferences(self.path)
        preferences.load()
        preferences.schedule({"workers": 4})
        preferences.flush()
        before = self.path.read_bytes()
        messages = []
        preferences.message.connect(lambda *args: messages.append(args))
        preferences.schedule({"workers": 5})
        with patch.object(preferences.store, "save", side_effect=PermissionError("locked")):
            self.assertFalse(preferences.flush())
            self.assertFalse(preferences.flush())
        self.assertEqual(len(messages), 1)
        self.assertIn("could not be saved", messages[0][2])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(preferences.flush())
        self.assertEqual(json.loads(self.path.read_text())["workers"], 5)
        preferences.close()

    def test_log_limits_are_restored_and_invalid_choices_use_defaults(self):
        self.path.write_text(json.dumps({"quality": "8K", "workers": 0,
                                       "log_max_bytes": 65536, "log_backup_count": 2}), encoding="utf-8")
        with patch("ui.preferences.configure_logging") as logging:
            window, workers = self.window()
        logging.assert_called_once_with(65536, 2, repeat_seconds=60)
        workers.assert_called_once_with(DEFAULTS["workers"])
        self.assertEqual(window.quality_combo.currentText(), DEFAULTS["quality"])
        self.assertEqual(len(window._message_boxes), 1)

    def test_partial_debounced_updates_are_merged_before_save(self):
        preferences = Preferences(self.path)
        preferences.load()
        preferences.schedule({'workers': 5})
        preferences.schedule({'quality': '720p'})
        self.assertTrue(preferences.close())
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['workers'], 5)
        self.assertEqual(saved['quality'], '720p')

    def test_pending_save_does_not_report_success_after_store_is_disabled(self):
        preferences = Preferences(self.path)
        preferences.load()
        preferences.schedule({'workers': 5})
        preferences.store.writable = False
        messages = []
        preferences.message.connect(lambda *args: messages.append(args))
        self.assertFalse(preferences.close())
        self.assertTrue(messages)
        self.assertIsNotNone(preferences._pending)
        self.assertFalse(self.path.exists())

    def test_repeat_interval_is_restored_from_json(self):
        self.path.write_text('{"log_repeat_seconds": 15}', encoding='utf-8')
        preferences = Preferences(self.path)
        with patch('ui.preferences.configure_logging') as configure:
            self.assertEqual(preferences.load()['log_repeat_seconds'], 15)
        configure.assert_called_once_with(DEFAULTS['log_max_bytes'], DEFAULTS['log_backup_count'],
                                          repeat_seconds=15)
        preferences.close()

    def test_close_time_save_error_remains_visible_then_shutdown_continues(self):
        window, _ = self.window()
        window.show()
        window.quality_combo.setCurrentText("720p")
        with patch.object(window.preferences.store, "save", side_effect=PermissionError("locked")):
            window.close()
        self.assertFalse(window._closing)
        self.assertTrue(window.isVisible())
        self.assertEqual(len(window._message_boxes), 1)
        next(iter(window._message_boxes)).done(QMessageBox.StandardButton.Ok)
        self.app.processEvents()
        self.assertTrue(window._allow_close)
        self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
