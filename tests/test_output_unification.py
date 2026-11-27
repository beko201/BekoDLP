"""Old output inputs normalize once and use the same current execution profile."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from media_options import validate_output_options
from output_compat import LEGACY_OUTPUT_KEYS, normalize_output
from queue_store import QueueStore
from settings import SettingsStore, DEFAULTS
from task_manager import TaskManager, TaskOptions


class OutputUnificationTests(unittest.TestCase):
    def test_legacy_and_current_audio_options_produce_the_same_profile(self):
        old = TaskOptions(folder='.', format='MP3', mp3_bitrate='128k')
        current = TaskOptions(folder='.', output_mode='audio', container='mp3',
                              audio_codec='mp3', audio_bitrate='128k')
        self.assertEqual(asdict(old), asdict(current))
        self.assertEqual(old.output_mode, 'audio')
        self.assertEqual(old.trim_download_mode, 'full')

    def test_legacy_h264_and_current_options_produce_the_same_profile(self):
        old = TaskOptions(folder='.', codec_mode='H.264 (Compatibility)')
        current = TaskOptions(folder='.', output_mode='video', container='mp4',
                              video_codec='h264', audio_codec='aac')
        self.assertEqual(asdict(old), asdict(current))

    def test_explicit_current_profile_wins_over_stale_legacy_labels(self):
        options = TaskOptions(folder='.', output_mode='audio', container='flac', audio_codec='flac',
                              audio_bitrate='auto', format='MP4', codec_mode='H.264 (Compatibility)',
                              mp3_bitrate='128k')
        self.assertEqual((options.output_mode, options.container, options.audio_codec), ('audio', 'flac', 'flac'))
        self.assertEqual(options.audio_bitrate, 'auto')
        self.assertEqual(options.format, 'MP3')

    def test_normalization_is_idempotent_and_does_not_modify_the_input(self):
        original = {'format': 'MP3', 'mp3_bitrate': '256k', 'quality': '720p'}
        result = normalize_output(original)
        self.assertEqual(normalize_output(result), result)
        self.assertEqual(original, {'format': 'MP3', 'mp3_bitrate': '256k', 'quality': '720p'})
        self.assertFalse(LEGACY_OUTPUT_KEYS.intersection(result))

    def test_legacy_inputs_do_not_bypass_current_profile_validation(self):
        with self.assertRaises(ValueError):
            TaskOptions(folder='.', format='MP3', mp3_bitrate='invalid')
        with self.assertRaises(ValueError):
            validate_output_options({'output_mode': 'legacy'})

    def test_settings_save_one_vocabulary_and_accept_old_api_changes(self):
        self.assertFalse(LEGACY_OUTPUT_KEYS.intersection(DEFAULTS))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            store = SettingsStore(path)
            store.save({'format': 'MP3', 'mp3_bitrate': '128k'})
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(LEGACY_OUTPUT_KEYS.intersection(saved))
            self.assertFalse(LEGACY_OUTPUT_KEYS.intersection(store.data))
            self.assertEqual((saved['output_mode'], saved['audio_bitrate']), ('audio', '128k'))
            self.assertFalse(store.save({}))
            store.save({'format': 'MP4', 'codec_mode': 'H.264 (Compatibility)'})
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((saved['output_mode'], saved['video_codec']), ('video', 'h264'))

    def test_invalid_legacy_preference_changes_preserve_the_saved_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            store = SettingsStore(path)
            store.save({})
            original = path.read_bytes()
            for change in ({'format': []}, {'codec_mode': 'unknown'}, {'mp3_bitrate': 'bad'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    store.save(change)
                self.assertEqual(path.read_bytes(), original)

    def test_old_queue_records_restore_and_are_rewritten_as_current_options(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            document = dict(schema_version=1, tasks=[dict(
                id='T1', url='https://example.com/audio', state='pending',
                options=dict(folder=folder, format='MP3', mp3_bitrate='128k'), title='Audio')])
            path.write_text(json.dumps(document), encoding='utf-8')
            store = QueueStore(path)
            restored, warnings = store.load()
            self.assertEqual(warnings, [])
            saved = json.loads(path.read_text(encoding='utf-8'))['tasks'][0]['options']
            self.assertFalse(LEGACY_OUTPUT_KEYS.intersection(saved))
            self.assertEqual((saved['output_mode'], saved['container'], saved['audio_bitrate']), ('audio', 'mp3', '128k'))
            manager = TaskManager(engine_factory=lambda manager: Mock())
            self.addCleanup(manager.close)
            manager.restore_queue(restored)
            self.assertEqual(manager.get_task('T1')['output_mode'], 'audio')
            self.assertTrue(manager.paused)
            self.assertFalse(LEGACY_OUTPUT_KEYS.intersection(manager.queue_snapshot()[1]['tasks'][0]['options']))
