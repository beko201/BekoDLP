"""Standalone services and multiple facades keep runtime ownership isolated."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from component_ports import EngineOperations, TaskOperations
from download_engine import DownloadEngine
from engine_conversion import MediaConversion
from engine_publication import MediaPublication
from engine_runtime import EngineRuntime
from task_lifecycle import TaskLifecycle
from task_manager import TaskManager, TaskOptions
from task_runtime import TaskRuntime
from task_state import TaskState
from task_storage import CancellationEvent, TaskRecord


class CoreCompositionTests(unittest.TestCase):
    def test_publication_component_reserves_paths_without_a_host_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            first_state = EngineRuntime(ffmpeg=None, ffprobe=None)
            second_state = EngineRuntime(ffmpeg=None, ffprobe=None)
            first = MediaPublication(first_state, Mock(spec=TaskOperations), Mock(spec=EngineOperations))
            second = MediaPublication(second_state, Mock(spec=TaskOperations), Mock(spec=EngineOperations))
            target = str(Path(folder) / 'video.mp4')
            self.assertEqual(first._dedupe_path(target, 'T1'), target)
            reserved = first._dedupe_path(target, 'T2')
            self.assertNotEqual(reserved, target)
            self.assertEqual(second._dedupe_path(target, 'T1'), target)
            first._release_output_reservation(target, 'T1')
            self.assertEqual(first._dedupe_path(target, 'T3'), target)
            self.assertNotEqual(second._dedupe_path(target, 'T2'), target)

    def test_probe_sessions_are_isolated_between_independent_conversion_services(self):
        first_state = EngineRuntime(ffmpeg=None, ffprobe=None)
        second_state = EngineRuntime(ffmpeg=None, ffprobe=None)
        first = MediaConversion(first_state, Mock(spec=TaskOperations), Mock(spec=EngineOperations))
        second = MediaConversion(second_state, Mock(spec=TaskOperations), Mock(spec=EngineOperations))
        with first.media_probe_session():
            first_cache = first_state._probe_cache.get()
            with second.media_probe_session():
                self.assertIsNot(first_cache, second_state._probe_cache.get())
                self.assertIs(first_state._probe_cache.get(), first_cache)
            self.assertIsNone(second_state._probe_cache.get())
            self.assertIs(first_state._probe_cache.get(), first_cache)
        self.assertIsNone(first_state._probe_cache.get())

    def test_lifecycle_component_cancels_using_only_declared_state_and_port(self):
        state = TaskRuntime()
        state.tasks['T1'] = TaskRecord({}, id='T1', url='https://example.com/video',
                                      state=TaskState.QUEUED, status='Queued')
        event = state.cancel_events['T1'] = CancellationEvent()
        notifications = Mock(spec=TaskOperations)
        lifecycle = TaskLifecycle(state, notifications)
        self.assertTrue(lifecycle._cancel_task_by_tid('T1'))
        self.assertTrue(event.is_set())
        self.assertEqual(state.tasks['T1']['state'], TaskState.CANCELLED)
        notifications._emit_task.assert_called_once_with('task_updated', 'T1', state.tasks['T1'])
        self.assertFalse(lifecycle._cancel_task_by_tid('T1'))

    def test_cancelling_and_closing_one_manager_leaves_the_other_usable(self):
        first = TaskManager(engine_factory=lambda manager: Mock())
        second = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        options = TaskOptions(folder='.')
        first.add_urls(['https://example.com/shared'], options)
        second.add_urls(['https://example.com/shared'], options)
        first.cancel_task('T1')
        first.close()
        self.assertTrue(first.finish_shutdown())
        self.assertEqual(second.get_task('T1')['state'], TaskState.QUEUED)
        self.assertFalse(second.is_cancelled('T1'))
        self.assertEqual(second.add_urls(['https://example.com/new'], options), ['T2'])
        self.assertEqual(second.summary().pending, 2)

    def test_existing_engine_override_is_used_by_composed_probe_operations(self):
        class CustomEngine(DownloadEngine):
            def _read_media_probe(self, path, executable):
                return {'streams': [{'codec_type': 'video', 'codec_name': 'h264'}],
                        'format': {'duration': '2'}}

        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        with patch('download_engine.require_executable'):
            engine = CustomEngine(manager, ffmpeg='ffmpeg', ffprobe='ffprobe')
        with engine.media_probe_session():
            self.assertEqual(engine._ffprobe_duration('source.mp4', 'ffprobe'), 2)
            self.assertEqual(engine._ffprobe_vcodec('source.mp4'), 'h264')

    def test_tool_changes_through_facade_reach_existing_components(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        self.addCleanup(manager.close)
        with patch('download_engine.require_executable'):
            engine = DownloadEngine(manager, ffmpeg='old-ffmpeg', ffprobe='old-ffprobe')
        engine.ffmpeg = 'updated-ffmpeg'
        with patch('engine_conversion.run_cmd_capture', return_value='') as run:
            engine.available_encoders()
        self.assertEqual(run.call_args.args[0][0], 'updated-ffmpeg')
        engine._encoders = None
        engine.ffmpeg = 'next-ffmpeg'
        with patch('engine_conversion.run_cmd_capture', return_value='') as run:
            engine.available_encoders()
        self.assertEqual(run.call_args.args[0][0], 'next-ffmpeg')
