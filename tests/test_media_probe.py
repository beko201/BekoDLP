"""Task-scoped probe reuse, invalidation and real FFprobe process counts."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import download_engine
from errors import MediaOperationError
from media_probe import MediaProbeCache, probe_duration
from media_plan import MediaSources, SourceStream, build_output_plan
from media_options import OUTPUT_DEFAULTS
from media_processing import ConfiguredOutput
from media_sources import SourcePreparer
from task_manager import TaskManager, TaskOptions
from utils import DownloadCancelled


def document(duration="2"):
    return {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
            "format": {"format_name": "mov,mp4", "duration": duration, "tags": {"major_brand": "isom"}}}


class MediaProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "source.mp4"
        self.path.write_bytes(b"media")
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.engine = download_engine.DownloadEngine(self.manager, ffmpeg="ffmpeg", ffprobe="ffprobe")
        self.manager.engine = self.engine
        self.addCleanup(self.manager.close)

    def test_one_query_serves_codec_container_and_duration_in_nested_scope(self):
        with patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            with self.engine.media_probe_session():
                probe = self.engine._ffprobe_media(str(self.path))
                with self.engine.media_probe_session():
                    self.assertEqual(self.engine._ffprobe_duration(self.path, self.engine.ffprobe), 2)
                    self.assertEqual(self.engine._ffprobe_vcodec(self.path), "h264")
                self.assertEqual(run.call_count, 1)
                self.assertEqual(probe["format"]["format_name"], "mov,mp4")
                command = run.call_args.args[0]
                self.assertIn("format=format_name,duration", command[command.index("-show_entries") + 1])
                cache = self.engine._probe_cache.get()
            self.assertFalse(cache.entries)
            self.assertIsNone(self.engine._probe_cache.get())
            with self.engine.media_probe_session():
                self.engine._ffprobe_media(self.path)
            self.assertEqual(run.call_count, 2)

    def test_file_changes_and_replacement_invalidate_results(self):
        replacement = self.folder / "replacement.mp4"
        replacement.write_bytes(b"new")
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            self.engine._ffprobe_media(self.path)
            self.path.write_bytes(b"longer updated media")
            self.engine._ffprobe_media(self.path)
            self.assertEqual(run.call_count, 2)
            self.engine._ffprobe_media(replacement)
            self.engine._replace_media(replacement, self.path)
            self.assertFalse(self.engine._probe_cache.get().entries)
            self.engine._ffprobe_media(self.path)
            self.assertEqual(run.call_count, 4)

    def test_ffmpeg_invalidates_output_even_on_failure_without_a_stat_change(self):
        command = ["ffmpeg", "-i", "input.wav", "-c", "copy", str(self.path)]
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as probe:
            self.engine._ffprobe_media(self.path)
            with patch.object(self.engine, "_run_ffmpeg_process", side_effect=OSError("launch failed")):
                with self.assertRaises(OSError):
                    self.engine._run_ffmpeg("T1", 1, command)
            self.engine._ffprobe_media(self.path)
            self.assertEqual(probe.call_count, 2)

    def test_fresh_verification_and_other_executables_bypass_cache(self):
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            self.engine._ffprobe_media(self.path)
            self.engine._ffprobe_media(self.path, fresh=True)
            self.assertEqual(run.call_count, 2)
            self.engine._ffprobe_duration(self.path, "another-ffprobe")
            self.assertEqual(run.call_args.args[0][0], "another-ffprobe")
            self.assertEqual(run.call_count, 3)
            self.engine._ffprobe_duration(self.path, self.engine.ffprobe)
            self.assertEqual(run.call_count, 3)

    def test_output_verification_requeries_even_when_old_cache_matches_file_signature(self):
        sources = MediaSources((self.path,), (SourceStream(0, 0, "video", "h264"),), 2)
        plan = build_output_plan({**OUTPUT_DEFAULTS, "quality": "1080p", "container": "mp4"}, sources)
        configured = ConfiguredOutput(self.engine)
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            self.engine._ffprobe_media(self.path)
            bad = document()
            bad["streams"][0]["codec_name"] = "vp9"
            run.return_value = json.dumps(bad)
            with self.assertRaisesRegex(MediaOperationError, "codecs"):
                configured.verify_destination("T1", str(self.path), plan)
            self.assertEqual(run.call_count, 2)

    def test_snapshot_isolation_and_failed_fresh_probe_cannot_reuse_old_data(self):
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            first = self.engine._ffprobe_media(self.path)
            first["streams"][0]["codec_name"] = "wrong"
            first["format"]["duration"] = "999"
            self.assertEqual(self.engine._ffprobe_vcodec(self.path), "h264")
            self.assertEqual(self.engine._ffprobe_duration(self.path, self.engine.ffprobe), 2)
            run.side_effect = RuntimeError("probe failed")
            with self.assertRaises(RuntimeError):
                self.engine._ffprobe_media(self.path, fresh=True)
            run.side_effect = None
            self.engine._ffprobe_media(self.path)
            self.assertEqual(run.call_count, 3)

    def test_changed_during_probe_and_malformed_documents_are_rejected(self):
        def changing(command, **kwargs):
            self.path.write_bytes(b"changed during inspection")
            return json.dumps(document())
        with self.engine.media_probe_session(), patch.object(download_engine, "run_cmd_capture", side_effect=changing):
            with self.assertRaisesRegex(MediaOperationError, "changed"):
                self.engine._ffprobe_media(self.path)
            self.assertFalse(self.engine._probe_cache.get().entries)
        for invalid in ("invalid json", "{}", '{"streams":"invalid","format":{"duration":2}}'):
            with self.subTest(invalid=invalid), self.engine.media_probe_session(), \
                    patch.object(download_engine, "run_cmd_capture", return_value=invalid):
                with self.assertRaises((ValueError, MediaOperationError)):
                    self.engine._ffprobe_media(self.path)
                self.assertFalse(self.engine._probe_cache.get().entries)

    def test_failed_attempt_clears_cache_before_retry(self):
        tid = self.manager.add_urls(["https://example.com/probe"], TaskOptions(folder=str(self.folder)))[0]
        def attempt(task, worker):
            self.engine._ffprobe_media(self.path)
            self.engine._ffprobe_media(self.path)
            raise RuntimeError("attempt failed")
        with patch.object(self.engine, "_process_task_attempt", side_effect=attempt), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    self.engine.process_task((tid, "https://example.com/probe"), 1)
                self.assertIsNone(self.engine._probe_cache.get())
            self.assertEqual(run.call_count, 2)
            self.assertEqual(self.manager.get_task(tid)["attempt"], 2)

    def test_parallel_contexts_share_only_their_own_attempt_cache(self):
        with patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document())) as run:
            with self.engine.media_probe_session(), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(copy_context().run, self.engine._ffprobe_media, self.path) for _ in range(2)]
                for future in futures:
                    future.result(timeout=2)
                self.assertEqual(run.call_count, 1)
            def isolated():
                with self.engine.media_probe_session():
                    self.engine._ffprobe_media(self.path)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: isolated(), range(2)))
            self.assertEqual(run.call_count, 3)

    def test_cache_is_bounded_and_missing_files_and_cancellation_are_not_cached(self):
        cache = MediaProbeCache()
        for index in range(cache.MAX_ENTRIES + 1):
            path = self.folder / f"{index}.mp4"
            path.write_bytes(b"media")
            cache.read(path, "ffprobe", document)
        self.assertEqual(len(cache.entries), cache.MAX_ENTRIES)
        loader = Mock(side_effect=document)
        missing = self.folder / "missing.mp4"
        cache.read(missing, "ffprobe", loader)
        cache.read(missing, "ffprobe", loader)
        self.assertEqual(loader.call_count, 2)
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", side_effect=DownloadCancelled()):
            with self.assertRaises(DownloadCancelled):
                self.engine._ffprobe_duration(self.path, self.engine.ffprobe)
            with self.assertRaises(DownloadCancelled):
                self.engine._ffprobe_vcodec(self.path)
            self.assertFalse(self.engine._probe_cache.get().entries)

    def test_unknown_duration_does_not_launch_another_process(self):
        for value in (None, "N/A", "nan", "inf", "-1", "0", {}, []):
            with self.subTest(value=value):
                self.assertEqual(probe_duration(document(value)), 0)
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", return_value=json.dumps(document("N/A"))) as run:
            self.engine._ffprobe_media(self.path)
            self.assertEqual(self.engine._ffprobe_duration(self.path, self.engine.ffprobe), 0)
            self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_inspection_and_legacy_trim_use_one_query_per_file(self):
        source = self.folder / "audio.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 96000)
        tid = self.manager.add_urls(["https://example.com/audio"], TaskOptions(folder=str(self.folder)))[0]
        self.engine.ffmpeg = shutil.which("ffmpeg")
        self.engine.ffprobe = shutil.which("ffprobe")
        with self.engine.media_probe_session(), \
                patch.object(download_engine, "run_cmd_capture", wraps=download_engine.run_cmd_capture) as run:
            sources = SourcePreparer(self.engine).inspect_sources((source,), 0)
            self.assertEqual(sources.duration, 2)
            self.assertEqual(self.engine._ffprobe_duration(source, self.engine.ffprobe), 2)
            self.assertEqual(run.call_count, 1)
            self.engine._maybe_trim(tid, 1, str(source), "0.25", "0.75")
            self.assertEqual(run.call_count, 2)
            self.assertAlmostEqual(self.engine._ffprobe_duration(source, self.engine.ffprobe), .5, delta=.05)
            self.assertEqual(run.call_count, 3)  # The replacement invalidated the old two-second result.


if __name__ == "__main__":
    unittest.main()
