"""Codec/container UI choices, persistence and immutable task snapshots."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from media_options import AUDIO_CODECS, AUDIO_CONTAINERS, VIDEO_CODECS, VIDEO_CONTAINERS, allowed_codecs
from settings import validate_settings
from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
from ui.output_controls import OutputControls


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)


class OutputControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controls = OutputControls()
        self.addCleanup(self.controls.close)

    def test_two_modes_default_to_original_video_with_automatic_container(self):
        controls = self.controls
        self.assertEqual(controls.mode(), "video")
        self.assertEqual(controls.format_combo.count(), 2)
        self.assertEqual(controls.format_combo.findData("AUTO"), -1)
        self.assertTrue(controls.container_combo.isEnabled())
        values = controls.values()
        self.assertEqual((values["container"], values["video_codec"], values["audio_codec"]),
                         ("auto", "original", "original"))

    def test_quick_audio_selection_emits_only_valid_complete_profiles(self):
        controls = self.controls
        choose(controls.format_combo, 'MP3')
        choose(controls.audio_codec_combo, 'mp3')
        snapshots = []
        controls.changed.connect(lambda: snapshots.append(TaskOptions(folder='.', **controls.values())))
        for codec, container in (('original', 'auto'), ('mp3', 'mp3'), ('aac', 'm4a'),
                                 ('opus', 'opus'), ('flac', 'flac'), ('lpcm', 'wav')):
            with self.subTest(codec=codec):
                snapshots.clear()
                choose(controls.audio_format_combo, codec)
                self.assertEqual(len(snapshots), 1)
                self.assertEqual((snapshots[0].audio_codec, snapshots[0].container), (codec, container))
                self.assertTrue(controls.quality_combo.isHidden())
                self.assertFalse(controls.audio_format_combo.isHidden())
                self.assertFalse(controls.advanced_toggle.isChecked())
        choose(controls.format_combo, 'MP4')
        self.assertFalse(controls.quality_combo.isHidden())
        self.assertTrue(controls.audio_format_combo.isHidden())

    def test_custom_audio_profile_roundtrips_without_quick_selector_overwriting_it(self):
        controls = self.controls
        choose(controls.format_combo, 'MP3')
        choose(controls.audio_format_combo, 'opus')
        choose(controls.container_combo, 'webm')
        options = TaskOptions(folder='.', **controls.values())
        data, issues = validate_settings(asdict(options))
        self.assertFalse(issues)
        controls.restore(data)
        self.assertEqual(controls.audio_codec_combo.currentData(), 'opus')
        self.assertEqual(controls.container_combo.currentData(), 'webm')
        self.assertEqual(controls.audio_format_combo.currentText(), 'Custom (advanced)')
        self.assertIn('WebM · Opus', controls.summary_label.text())
        controls.trim_start_edit.setText('1:30')
        controls.trim_end_edit.setText('2:45')
        self.assertIn('Trim: 1:30 – 2:45', controls.summary_label.text())
        choose(controls.audio_format_combo, 'original')
        self.assertEqual(controls.container_combo.currentData(), 'auto')
        self.assertEqual(controls.audio_codec_combo.currentData(), 'original')

    def test_depth_is_lossless_only_and_roundtrips_in_an_immutable_snapshot(self):
        controls = self.controls
        choose(controls.format_combo, "MP3")
        self.assertTrue(controls.bit_depth_combo.isHidden())
        for codec in ("flac", "lpcm"):
            choose(controls.audio_codec_combo, codec)
            self.assertFalse(controls.bit_depth_combo.isHidden())
            self.assertFalse(controls.bitrate_combo.isEnabled())
            choose(controls.bit_depth_combo, "24")
            options = TaskOptions(folder=".", **controls.values())
            choose(controls.bit_depth_combo, "16")
            self.assertEqual(options.audio_bit_depth, "24")
            restored, issues = validate_settings(asdict(options))
            self.assertFalse(issues)
            controls.restore(restored)
            self.assertEqual(controls.bit_depth_combo.currentData(), "24")
        choose(controls.audio_codec_combo, "opus")
        self.assertTrue(controls.bit_depth_combo.isHidden())
        self.assertTrue(controls.bitrate_combo.isEnabled())

    def test_webm_resets_incompatible_codecs_before_emitting_preferences(self):
        controls = self.controls
        choose(controls.format_combo, "MP4")
        choose(controls.codec_combo, "h264")
        choose(controls.audio_codec_combo, "aac")
        snapshots = []
        controls.changed.connect(lambda: snapshots.append(TaskOptions(folder=".", **controls.values())))
        choose(controls.container_combo, "webm")
        self.assertEqual(controls.codec_combo.currentData(), "original")
        self.assertEqual(controls.audio_codec_combo.currentData(), "original")
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(controls.codec_combo.model().item(controls.codec_combo.findData("h264")).isEnabled())
        self.assertIn("reset to Original", controls.hint.text())
        self.assertEqual(TaskOptions(folder=".", **controls.values()).container, "webm")
        choose(controls.codec_combo, "vp9")
        choose(controls.audio_codec_combo, "opus")
        self.assertEqual(TaskOptions(folder=".", **controls.values()).container, "webm")

    def test_automatic_lpcm_then_mp4_cannot_leave_an_incompatible_selection(self):
        controls = self.controls
        choose(controls.container_combo, "mp4")
        choose(controls.container_combo, "auto")
        choose(controls.audio_codec_combo, "lpcm")
        choose(controls.bit_depth_combo, "24")
        choose(controls.container_combo, "mp4")
        self.assertEqual(controls.audio_codec_combo.currentData(), "original")
        self.assertTrue(controls.bit_depth_combo.isHidden())
        self.assertIn("LPCM", controls.hint.text())
        self.assertEqual(TaskOptions(folder=".", **controls.values()).audio_codec, "original")

    def test_container_transitions_preserve_only_compatible_explicit_codecs(self):
        controls = self.controls
        for mode, containers in (("MP4", VIDEO_CONTAINERS), ("MP3", AUDIO_CONTAINERS)):
            choose(controls.format_combo, mode)
            for audio in AUDIO_CODECS:
                if audio == "none" and mode == "MP3":
                    continue
                for video in VIDEO_CODECS if mode == "MP4" else ("original",):
                    for container in containers:
                        with self.subTest(mode=mode, video=video, audio=audio, container=container):
                            choose(controls.container_combo, "auto")
                            choose(controls.codec_combo, video)
                            choose(controls.audio_codec_combo, audio)
                            choose(controls.container_combo, container)
                            videos, audios = allowed_codecs(controls.mode(), container)
                            self.assertEqual(controls.codec_combo.currentData(), video if video in videos else "original")
                            self.assertEqual(controls.audio_codec_combo.currentData(), audio if audio in audios else "original")
                            TaskOptions(folder=".", **controls.values())

    def test_removed_containers_are_absent_but_ogg_opus_and_aac_remain(self):
        controls = self.controls
        choose(controls.format_combo, "MP3")
        choose(controls.audio_codec_combo, "aac")
        for container in ("m4a",):
            choose(controls.container_combo, container)
            options = TaskOptions(folder=".", **controls.values())
            self.assertEqual(options.audio_codec, "aac")
            self.assertEqual(options.container, container)
        self.assertEqual(controls.container_combo.findData("aac"), -1)
        self.assertEqual(controls.container_combo.findData("ogg"), -1)
        self.assertGreaterEqual(controls.container_combo.findData("opus"), 0)
        self.assertEqual(controls.audio_codec_combo.itemText(controls.audio_codec_combo.findData("lpcm")), "LPCM")
        self.assertFalse(controls.codec_combo.isEnabled())
        choose(controls.container_combo, "auto")
        choose(controls.audio_codec_combo, "flac")
        self.assertFalse(controls.bitrate_combo.isEnabled())
        self.assertIn("does not restore quality", controls.hint.text())

    def test_encoder_probe_disables_unavailable_encoders_without_saving_preferences(self):
        changes = []
        self.controls.changed.connect(lambda: changes.append(1))
        self.controls.set_encoders({"libx264", "aac"})
        self.assertEqual(changes, [])
        codec = self.controls.codec_combo
        self.assertTrue(codec.model().item(codec.findData("h264")).isEnabled())
        self.assertFalse(codec.model().item(codec.findData("hevc")).isEnabled())
        self.assertTrue(codec.model().item(codec.findData("original")).isEnabled())
        choose(self.controls.format_combo, 'MP3')
        quick = self.controls.audio_format_combo
        self.assertTrue(quick.model().item(quick.findData('aac')).isEnabled())
        self.assertTrue(quick.model().item(quick.findData('original')).isEnabled())
        self.assertFalse(quick.model().item(quick.findData('mp3')).isEnabled())

    def test_profile_snapshot_is_independent_and_64k_bitrate_roundtrips(self):
        controls = self.controls
        choose(controls.format_combo, "MP3")
        choose(controls.audio_codec_combo, "opus")
        choose(controls.container_combo, "opus")
        controls.bitrate_combo.setCurrentText("64k")
        options = TaskOptions(folder=".", **controls.values())
        controls.bitrate_combo.setCurrentText("320k")
        choose(controls.audio_codec_combo, "aac")
        self.assertEqual((options.audio_codec, options.container, options.audio_bitrate), ("opus", "opus", "64k"))
        restored, warnings = validate_settings(asdict(options))
        self.assertFalse(warnings)
        controls.restore(restored)
        self.assertEqual(controls.bitrate_combo.currentText(), "64k")
        self.assertEqual(controls.audio_codec_combo.currentData(), "opus")

    def test_automatic_bitrate_roundtrips_and_tracks_the_selected_codec(self):
        controls = self.controls
        choose(controls.format_combo, 'MP3')
        choose(controls.audio_format_combo, 'mp3')
        controls.bitrate_combo.setCurrentText('Automatic')
        options = TaskOptions(folder='.', **controls.values())
        self.assertEqual(options.audio_bitrate, 'auto')
        self.assertIn('Automatic (192k)', controls.summary_label.text())
        saved, issues = validate_settings(asdict(options))
        self.assertFalse(issues)
        controls.bitrate_combo.setCurrentText('320k')
        controls.restore(saved)
        self.assertEqual(controls.bitrate_combo.currentText(), 'Automatic')
        choose(controls.audio_format_combo, 'opus')
        self.assertIn('Automatic (128k)', controls.summary_label.text())
        choose(controls.audio_format_combo, 'original')
        self.assertFalse(controls.bitrate_combo.isEnabled())
        self.assertEqual(controls.values()['audio_bitrate'], 'auto')

    def test_window_persists_new_fields_and_rejects_invalid_profile_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = TaskManager(engine_factory=lambda manager: Mock())
            path = Path(folder, "settings.json")
            with patch.object(manager, "set_worker_count"):
                window = MainWindow(manager, settings_path=path)
            try:
                window.folder = folder
                window.url_edit.setText("https://example.com/a")
                choose(window.format_combo, "MP4")
                choose(window.codec_combo, "h264")
                choose(window.audio_codec_combo, "aac")
                choose(window.container_combo, "webm")
                self.assertEqual(window.audio_codec_combo.currentData(), "original")
                # Bypass disabled UI items to verify the final submission guard too.
                choose(window.codec_combo, "h264")
                with patch.object(manager, "submit_link") as submit, patch.object(window, "_show_message") as message:
                    window.enqueue()
                submit.assert_not_called()
                self.assertEqual(message.call_args.args[1], "Invalid output profile")
                self.assertEqual(window.url_edit.text(), "https://example.com/a")
                choose(window.codec_combo, "vp9")
                choose(window.audio_codec_combo, "opus")
                window.preferences.flush()
                saved = json.loads(path.read_text())
                self.assertEqual((saved["container"], saved["video_codec"], saved["audio_codec"]), ("webm", "vp9", "opus"))
            finally:
                finish_window_close(window)
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
