"""Single-pass configured trimming with real media and recoverable failures."""
from dataclasses import asdict
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import download_engine
from errors import MediaOperationError
from media_processing import ConfiguredOutput
from media_sources import SourcePreparer
from task_manager import TaskManager, TaskOptions
from errors import DownloadCancelled, InsufficientDiskSpace
from process_utils import NO_WINDOW


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class ConfiguredTrimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixtures.name)
        cls.video = cls.root / "video.mp4"
        cls.audio = cls.root / "audio.wav"
        cls.ffmpeg = shutil.which("ffmpeg")
        for args, output in (
            (["-f", "lavfi", "-i", "testsrc2=s=128x96:r=10:d=4", "-c:v", "libx264",
              "-threads", "2", "-g", "10", "-pix_fmt", "yuv420p"], cls.video),
            (["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
              "-c:a", "pcm_s16le"], cls.audio),
        ):
            subprocess.run([cls.ffmpeg, "-y", *args, str(output)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.manager = TaskManager()
        self.engine = self.manager.engine
        self.addCleanup(self.manager.close)

    def prepare(self, *, inputs=None, **profile):
        options = TaskOptions(folder=str(self.folder), output_mode=profile.pop("output_mode", "audio"), **profile)
        tid = self.manager.add_urls(["https://example.com/trim"], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        self.manager.update_metadata(tid, duration=4)
        private = self.folder / "private"
        private.mkdir()
        paths = []
        for index, source in enumerate(inputs or (self.audio,)):
            target = private / f"source{index}{source.suffix}"
            shutil.copyfile(source, target)
            paths.append(target)
        sources = SourcePreparer(self.engine).inspect_sources(paths, 4)
        configured = ConfiguredOutput(self.engine)
        configured.sources.prepare = lambda *args: sources
        return configured, tid, asdict(options), sources, private

    def process(self, configured, tid, task, sources, private):
        return Path(configured.process(tid, 1, "offline", SimpleNamespace(duration=4),
                                       task, str(private), "Output"))

    def duration(self, path):
        return self.engine._ffprobe_duration(str(path), self.engine.ffprobe)

    def test_audio_conversion_cuts_once_and_verifies_the_final_codec(self):
        args = self.prepare(audio_codec="mp3", trim_start="1", trim_end="2")
        with patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(*args)
        self.assertEqual(runner.call_count, 1)
        self.assertAlmostEqual(runner.call_args.kwargs["duration"], 1)
        self.assertAlmostEqual(self.duration(output), 1, delta=.1)
        self.assertEqual(self.engine._ffprobe_media(str(output))["streams"][0]["codec_name"], "mp3")
        self.assertTrue(all(path.exists() for path in args[3].paths))
        self.assertFalse(self.manager.get_task(args[1]).get("warnings"))

    def test_unknown_source_duration_and_final_cut_need_only_two_probe_processes(self):
        args = self.prepare(audio_codec="mp3", trim_start="1", trim_end="2")
        configured, _, _, sources, _ = args
        configured.sources.prepare = lambda *unused: configured.sources.inspect_sources(sources.paths, 0)
        with patch('engine_conversion.run_cmd_capture', wraps=download_engine.run_cmd_capture) as run, \
                patch.object(self.engine, "_ffprobe_duration", side_effect=AssertionError("separate duration query")):
            output = self.process(*args)
        probes = [call.args[0] for call in run.call_args_list if call.args[0][0] == self.engine.ffprobe]
        self.assertEqual(len(probes), 2)
        self.assertEqual(Path(probes[0][-1]), sources.paths[0])
        self.assertEqual(Path(probes[1][-1]), output)
        self.assertAlmostEqual(self.duration(output), 1, delta=.1)

    def test_start_only_and_end_only_cuts_use_the_requested_interval(self):
        args = self.prepare(audio_codec="mp3", trim_start="3")
        output = self.process(*args)
        self.assertAlmostEqual(self.duration(output), 1, delta=.1)
        task = {**args[2], "trim_start": "", "trim_end": "0.75"}
        output = self.process(args[0], args[1], task, args[3], args[4])
        self.assertAlmostEqual(self.duration(output), .75, delta=.1)

    def test_original_audio_is_cut_with_stream_copy_without_encoder_probe(self):
        args = self.prepare(trim_start="1", trim_end="2")
        with patch.object(self.engine, "available_encoders", side_effect=AssertionError("copy needs no encoder")), \
                patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(*args)
        self.assertEqual(runner.call_count, 1)
        cmd = runner.call_args.args[2]
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertAlmostEqual(self.duration(output), 1, delta=.05)
        self.assertTrue(args[3].paths[0].exists())

    def test_separate_inputs_seek_together_with_video_copy_and_audio_conversion(self):
        args = self.prepare(inputs=(self.video, self.audio), output_mode="video", container="mp4",
                            audio_codec="aac", trim_start="1", trim_end="2")
        with patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(*args)
        self.assertEqual(runner.call_count, 1)
        cmd = runner.call_args.args[2]
        for index, value in enumerate(cmd):
            if value == "-i":
                self.assertEqual(cmd[index - 2:index], ["-ss", "1.0"])
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertAlmostEqual(self.duration(output), 1, delta=.3)
        probe = self.engine._ffprobe_media(str(output))
        self.assertEqual([s["codec_name"] for s in probe["streams"]], ["h264", "aac"])

    def test_failed_cut_rebuilds_full_output_without_a_second_download(self):
        args = self.prepare(audio_codec="mp3", trim_start="1", trim_end="2")
        original = self.engine._run_ffmpeg
        commands = []
        def fail_cut(tid, worker, cmd, **kwargs):
            commands.append(cmd)
            if "-t" in cmd:
                Path(cmd[-1]).write_bytes(b"partial")
                raise MediaOperationError("cut failed")
            return original(tid, worker, cmd, **kwargs)
        with patch.object(self.engine, "_run_ffmpeg", side_effect=fail_cut):
            output = self.process(*args)
        self.assertEqual(len(commands), 2)
        self.assertNotIn("-ss", commands[-1])
        self.assertNotIn("-t", commands[-1])
        self.assertAlmostEqual(self.duration(output), 4, delta=.1)
        self.assertIn("full file was saved", self.manager.get_task(args[1])["warnings"][0])

    def test_video_is_encoded_only_for_the_cut_range(self):
        args = self.prepare(inputs=(self.video,), output_mode="video", container="webm",
                            video_codec="vp9", audio_codec="none", trim_start="1", trim_end="2")
        with patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(*args)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.kwargs["duration"], 1)
        self.assertAlmostEqual(self.duration(output), 1, delta=.15)
        self.assertEqual(self.engine._ffprobe_media(str(output))["streams"][0]["codec_name"], "vp9")

    def test_invalid_cut_codec_is_rejected_before_full_output_fallback(self):
        args = self.prepare(audio_codec="mp3", trim_end="1")
        original_probe = self.engine._ffprobe_media
        calls = []
        def wrong_first(path, **kwargs):
            probe = original_probe(path, **kwargs)
            calls.append(path)
            if len(calls) == 1:
                probe["streams"][0]["codec_name"] = "aac"
            return probe
        with patch.object(self.engine, "_ffprobe_media", side_effect=wrong_first), \
                patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(*args)
        self.assertEqual(runner.call_count, 2)
        self.assertAlmostEqual(self.duration(output), 4, delta=.1)
        self.assertIn("full file was saved", self.manager.get_task(args[1])["warnings"][0])

    def test_empty_cut_falls_back_and_an_out_of_range_start_never_encodes_a_cut(self):
        args = self.prepare(audio_codec="mp3", trim_start="1", trim_end="2")
        original_probe = self.engine._ffprobe_duration
        original_media = self.engine._ffprobe_media
        calls = []
        def empty_first(path, **kwargs):
            probe = original_media(path, **kwargs)
            calls.append(path)
            if len(calls) == 1:
                probe["format"]["duration"] = "0"
            return probe
        with patch.object(self.engine, "_ffprobe_media", side_effect=empty_first), \
                patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(*args)
        self.assertEqual(runner.call_count, 2)
        self.assertAlmostEqual(original_probe(str(output), self.engine.ffprobe), 4, delta=.1)
        task = {**args[2], "trim_start": "10", "trim_end": "12"}
        with patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            output = self.process(args[0], args[1], task, args[3], args[4])
        self.assertEqual(runner.call_count, 1)
        self.assertNotIn("-ss", runner.call_args.args[2])
        self.assertAlmostEqual(self.duration(output), 4, delta=.1)

    def test_cancel_and_disk_exhaustion_never_start_full_output_fallback(self):
        args = self.prepare(audio_codec="mp3", trim_end="1")
        for error in (DownloadCancelled(), InsufficientDiskSpace("disk full")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.engine, "_run_ffmpeg", side_effect=error) as runner:
                with self.assertRaises(type(error)):
                    self.process(*args)
                self.assertEqual(runner.call_count, 1)
        self.assertFalse(self.manager.get_task(args[1]).get("warnings"))

    def test_engine_publishes_the_cut_without_trimming_it_twice(self):
        args = self.prepare(audio_codec="mp3", trim_start="1", trim_end="2")
        _, tid, _, sources, _ = args
        info = SimpleNamespace(title="Published", duration=4, thumbnail_url=None)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=info), \
                patch.object(SourcePreparer, "prepare", return_value=sources), \
                patch.object(self.engine, "_maybe_trim", side_effect=AssertionError("already cut")), \
                patch.object(self.engine, "_run_ffmpeg", wraps=self.engine._run_ffmpeg) as runner:
            self.manager._process_task((tid, "https://example.com/trim"), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task["status"].startswith("Completed"), task["status"])
        self.assertEqual(task["progress"], 100)
        self.assertEqual(runner.call_count, 1)
        self.assertAlmostEqual(self.duration(task["output_path"]), 1, delta=.1)


if __name__ == "__main__":
    unittest.main()
