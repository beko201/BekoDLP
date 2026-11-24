"""Output-profile contracts and real, offline FFmpeg stream verification."""
from dataclasses import asdict
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import download_engine
from media_options import OUTPUT_DEFAULTS, validate_output_options
from media_processing import ConfiguredOutput, verify_output
from media_plan import MediaSources, SourceStream, build_output_plan, read_bit_depth
from media_sources import SourcePreparer, read_ebml_doctype, read_mp4_faststart
from errors import MediaOperationError
from settings import validate_settings
from task_manager import TaskManager, TaskOptions
from utils import NO_WINDOW


class ProfileValidationTests(unittest.TestCase):
    def test_automatic_forbids_hidden_conversion_and_incompatible_profiles_fail_at_submission(self):
        for changes in ({"output_mode": "automatic", "video_codec": "h264"},
                        {"output_mode": "video", "container": "webm", "video_codec": "h264"},
                        {"output_mode": "video", "container": "mp4", "audio_codec": "opus"},
                        {"output_mode": "audio", "container": "wav", "audio_codec": "aac"},
                        {"output_mode": "audio", "audio_codec": "none"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                TaskOptions(folder=".", **changes)
        validate_output_options(OUTPUT_DEFAULTS)

    def test_legacy_settings_migrate_without_confusing_codec_and_container(self):
        audio, issues = validate_settings({"format": "MP3", "mp3_bitrate": "320k"})
        self.assertFalse(issues)
        self.assertEqual((audio["output_mode"], audio["container"], audio["audio_codec"], audio["audio_bitrate"]),
                         ("audio", "mp3", "mp3", "320k"))
        video, issues = validate_settings({"format": "MP4", "codec_mode": "H.264 (Compatibility)"})
        self.assertFalse(issues)
        self.assertEqual((video["output_mode"], video["container"], video["video_codec"], video["audio_codec"]),
                         ("video", "mp4", "h264", "aac"))

    def test_removed_preferences_migrate_without_losing_pcm_precision(self):
        for old, expected in ((dict(output_mode="automatic"), dict(output_mode="video")),
                              (dict(output_mode="audio", container="aac", audio_codec="aac"), dict(container="m4a")),
                              (dict(output_mode="audio", container="ogg", audio_codec="opus"), dict(container="opus")),
                              (dict(output_mode="audio", container="ogg"), dict(container="auto")),
                              (dict(output_mode="audio", container="wav", audio_codec="pcm_s16le"),
                               dict(audio_codec="lpcm", audio_bit_depth="16"))):
            with self.subTest(old=old):
                data, issues = validate_settings(old)
                self.assertFalse(issues)
                for key, value in expected.items():
                    self.assertEqual(data[key], value)
        for removed in (dict(output_mode="automatic"), dict(output_mode="audio", container="aac"),
                        dict(output_mode="audio", container="ogg"), dict(output_mode="audio", audio_bit_depth="32")):
            with self.subTest(removed=removed), self.assertRaises(ValueError):
                TaskOptions(folder=".", **removed)

    def test_invalid_saved_combinations_recover_to_copying_defaults(self):
        data, issues = validate_settings(dict(output_mode="audio", container="wav", audio_codec="aac"))
        self.assertIn("output profile", issues)
        for key, value in OUTPUT_DEFAULTS.items():
            self.assertEqual(data[key], value)
        # Bad historical codec values must not cause an unhashable-key error.
        self.assertIn("codec_mode", validate_settings({"codec_mode": []})[1])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class RealOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ffmpeg = shutil.which("ffmpeg")
        cls.source = cls.root / "source.mp4"
        subprocess.run([cls.ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=128x96:r=10:d=0.6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.6",
                        "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", str(cls.source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        cls.audio = cls.root / "audio.m4a"
        subprocess.run([cls.ffmpeg, "-y", "-i", str(cls.source), "-vn", "-c:a", "copy", str(cls.audio)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        cls.mkv = cls.root / "source.mkv"
        subprocess.run([cls.ffmpeg, "-y", "-i", str(cls.source), "-c", "copy", str(cls.mkv)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        cls.webm = cls.root / "source.webm"
        subprocess.run([cls.ffmpeg, "-y", "-i", str(cls.source), "-c:v", "libvpx-vp9", "-threads", "2",
                        "-cpu-used", "8", "-c:a", "libopus", str(cls.webm)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        cls.faststart = cls.root / "faststart.mp4"
        subprocess.run([cls.ffmpeg, "-y", "-i", str(cls.source), "-c", "copy", "-movflags", "+faststart",
                        str(cls.faststart)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.manager = TaskManager()
        self.engine = self.manager.engine
        self.commands = []
        runner = self.engine._run_ffmpeg
        def limited(tid, wid, cmd, *args, **kwargs):
            self.commands.append(list(cmd))
            # Keep real encoding tests lightweight on large multicore hosts.
            if "libx265" in cmd:
                cmd = cmd[:-1] + ["-x265-params", "pools=2:frame-threads=2"] + cmd[-1:]
            if "libsvtav1" in cmd:
                cmd = cmd[:-1] + ["-svtav1-params", "lp=2"] + cmd[-1:]
            cmd = cmd[:-1] + ["-threads", "2"] + cmd[-1:]
            return runner(tid, wid, cmd, *args, **kwargs)
        patcher = patch.object(self.engine, "_run_ffmpeg", side_effect=limited)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.manager.close)

    def info(self, path, audio_only=False, video_codec="avc1", audio_codec="mp4a"):
        return download_engine.YTInfo("offline", {"title": "Sample", "duration": .6, "formats": [
            dict(format_id="source", ext=path.suffix[1:], vcodec="none" if audio_only else video_codec,
                 acodec=audio_codec, height=None if audio_only else 96, filesize=path.stat().st_size)]})

    def process(self, *, source=None, audio_only=False, **profile):
        source = source or (self.audio if audio_only else self.source)
        options = TaskOptions(folder=str(self.folder), format="MP3" if audio_only else "MP4",
                              output_mode="audio" if audio_only else "video", **profile)
        tid = self.manager.add_urls([f"https://example.com/{len(self.manager.list_tasks())}"], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        private = self.folder / f"private-{tid}"
        private.mkdir()
        def download(tid, key, url, stream, filename, *, folder):
            destination = Path(folder, filename)
            shutil.copyfile(source, destination)
            return str(destination)
        with patch.object(self.engine, "_download_format", side_effect=download):
            path = ConfiguredOutput(self.engine).process(tid, 1, "offline", self.info(source, audio_only),
                                                       asdict(options), str(private), "Output")
        self.manager.tid_to_workerid.pop(tid, None)
        return Path(path), self.engine._ffprobe_media(path)

    @staticmethod
    def codecs(probe):
        return [(s["codec_type"], s["codec_name"]) for s in probe["streams"]]

    def test_mp4_matching_codecs_are_copied_without_any_encoder_requirement(self):
        with patch.object(self.engine, "available_encoders", side_effect=AssertionError("copy must not probe encoders")):
            path, probe = self.process(container="mp4", video_codec="h264", audio_codec="aac")
        cmd = self.commands[-1]
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertEqual(self.codecs(probe), [("video", "h264"), ("audio", "aac")])
        for stream in ("v:0", "a:0"):
            def packets(file):
                return subprocess.check_output([self.engine.ffprobe, "-v", "error", "-select_streams", stream,
                    "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=data_hash",
                    "-of", "csv=p=0", str(file)], **NO_WINDOW)
            self.assertEqual(packets(self.source), packets(path))
        self.assertTrue(read_mp4_faststart(path))

    def test_suitable_mp4_mkv_and_webm_are_preserved_byte_for_byte(self):
        for source, container in ((self.faststart, "mp4"), (self.mkv, "mkv"), (self.webm, "webm")):
            for requested in ("auto", container):
                with self.subTest(source=source.name, requested=requested), \
                        patch.object(self.engine, "available_encoders", side_effect=AssertionError("must preserve")):
                    path, probe = self.process(source=source, container=requested)
                    self.assertEqual(path.read_bytes(), source.read_bytes())
                    self.assertEqual(self.commands, [])
                    self.assertEqual(path.suffix, "." + container)
                    self.assertTrue(probe["streams"])
        path, _ = self.process(source=self.faststart, container="mp4", video_codec="h264", audio_codec="aac")
        self.assertEqual(path.read_bytes(), self.faststart.read_bytes())
        self.assertEqual(self.commands, [])

    def test_extra_subtitle_stream_requires_remux_and_is_removed(self):
        subtitles = self.folder / "subtitles.srt"
        subtitles.write_text("1\n00:00:00,000 --> 00:00:00,500\nExample\n", encoding="utf-8")
        source = self.folder / "with-subtitles.mkv"
        subprocess.run([self.ffmpeg, "-y", "-i", str(self.mkv), "-i", str(subtitles),
                        "-map", "0", "-map", "1", "-c", "copy", str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        path, probe = self.process(source=source, container="mkv")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.codecs(probe), [("video", "h264"), ("audio", "aac")])
        self.assertNotEqual(path.read_bytes(), source.read_bytes())

    def test_matroska_named_webm_is_remuxed_to_the_requested_document_type(self):
        source = self.folder / "disguised.webm"
        subprocess.run([self.ffmpeg, "-y", "-i", str(self.webm), "-c", "copy", "-f", "matroska",
                        str(source)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        path, _ = self.process(source=source, container="webm")
        self.assertEqual(read_ebml_doctype(source), "matroska")
        self.assertEqual(read_ebml_doctype(path), "webm")
        self.assertEqual(len(self.commands), 1)

    def test_preserved_video_verification_failure_never_publishes(self):
        options = TaskOptions(folder=str(self.folder), output_mode="video", container="mp4")
        tid = self.manager.add_urls(["https://example.com/verify"], options)[0]
        protected = self.folder / "Sample.mp4"
        protected.write_bytes(b"existing output")
        original_probe = self.engine._ffprobe_media
        def probe(path, **kwargs):
            if Path(path).name == "Sample.mp4":
                raise MediaOperationError("output verification failed")
            return original_probe(path, **kwargs)
        def download(tid, key, url, stream, filename, *, folder):
            path = Path(folder, filename)
            shutil.copyfile(self.faststart, path)
            return str(path)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info(self.faststart)), \
                patch.object(self.engine, "_download_format", side_effect=download), \
                patch.object(self.engine, "_ffprobe_media", side_effect=probe):
            self.manager._process_task((tid, "https://example.com/verify"), 1)
        self.assertTrue(self.manager.get_task(tid)["status"].startswith("Error"))
        self.assertIn("output verification failed", self.manager.get_task(tid)["status"])
        self.assertEqual(list(self.folder.iterdir()), [protected])
        self.assertEqual(protected.read_bytes(), b"existing output")
        self.assertEqual(self.commands, [])

    def test_vp9_matroska_and_webm_need_matching_doctypes_despite_identical_codecs(self):
        disguised = self.folder / "matroska-with-webm-extension.webm"
        subprocess.run([self.ffmpeg, "-y", "-i", str(self.webm), "-c", "copy", "-f", "matroska", str(disguised)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        sources = SourcePreparer(self.engine).inspect_sources((self.webm,), .6)
        probes = [self.engine._ffprobe_media(str(path)) for path in (self.webm, disguised)]
        self.assertEqual(self.codecs(probes[0]), self.codecs(probes[1]))
        self.assertEqual(probes[0]["format"]["format_name"], probes[1]["format"]["format_name"])
        for container, good, wrong in (("webm", 0, 1), ("mkv", 1, 0)):
            plan = build_output_plan({**OUTPUT_DEFAULTS, "container": container}, sources, set())
            paths = (self.webm, disguised)
            verify_output(probes[good], plan, ebml_doctype=read_ebml_doctype(paths[good]))
            with self.assertRaisesRegex(RuntimeError, "container"):
                verify_output(probes[wrong], plan, ebml_doctype=read_ebml_doctype(paths[wrong]))

    def test_hevc_vp9_and_av1_are_encoded_and_verified(self):
        for codec, container in (("hevc", "mp4"), ("vp9", "webm"), ("av1", "mkv")):
            with self.subTest(codec=codec):
                path, probe = self.process(container=container, video_codec=codec,
                                           audio_codec="opus" if container == "webm" else "aac")
                self.assertEqual(path.suffix, "." + container)
                self.assertEqual(self.codecs(probe), [("video", codec), ("audio", "opus" if container == "webm" else "aac")])

    def test_audio_codec_and_container_are_independent_and_video_is_removed(self):
        for codec, container, actual in (("aac", "m4a", "aac"),
                                        ("mp3", "mp3", "mp3"), ("opus", "opus", "opus"),
                                        ("opus", "webm", "opus"), ("flac", "flac", "flac"),
                                        ("lpcm", "wav", "pcm_s24le")):
            with self.subTest(codec=codec, container=container):
                path, probe = self.process(audio_only=True, container=container, audio_codec=codec)
                self.assertEqual(path.suffix, "." + container)
                self.assertEqual(self.codecs(probe), [("audio", actual)])

    def test_lossless_depth_is_encoded_and_verified_for_both_codecs(self):
        for codec in ("flac", "lpcm"):
            for depth in ("16", "24"):
                with self.subTest(codec=codec, depth=depth):
                    _, probe = self.process(audio_only=True, audio_codec=codec, audio_bit_depth=depth)
                    self.assertEqual(read_bit_depth(probe["streams"][0]), int(depth))
                    self.assertNotIn("-b:a", self.commands[-1])

    def test_original_depth_preserves_24_and_explicit_16_reencodes_matching_flac(self):
        source, _ = self.process(audio_only=True, audio_codec="flac", audio_bit_depth="24")
        for codec in ("flac", "lpcm"):
            with self.subTest(codec=codec):
                _, probe = self.process(source=source, audio_only=True, audio_codec=codec, audio_bit_depth="original")
                self.assertEqual(read_bit_depth(probe["streams"][0]), 24)
        self.process(source=source, audio_only=True, audio_codec="flac", audio_bit_depth="original")
        self.assertEqual(self.commands[-1][self.commands[-1].index("-c:a") + 1], "copy")
        _, probe = self.process(source=source, audio_only=True, audio_codec="flac", audio_bit_depth="16")
        self.assertEqual(read_bit_depth(probe["streams"][0]), 16)
        self.assertEqual(self.commands[-1][self.commands[-1].index("-c:a") + 1], "flac")

    def test_unknown_lossy_precision_uses_24_with_a_warning(self):
        with patch.object(self.manager, "add_warning", wraps=self.manager.add_warning) as warning:
            _, probe = self.process(audio_only=True, audio_codec="lpcm")
        self.assertEqual(read_bit_depth(probe["streams"][0]), 24)
        self.assertIn("Source bit depth is unavailable", warning.call_args.args[1])

    def test_depth_verification_rejects_incorrect_encoder_output(self):
        with self.assertRaisesRegex(RuntimeError, "bit depth"):
            sources = MediaSources((self.audio,), (SourceStream(0, 0, "audio", "aac"),), .6)
            plan = build_output_plan(dict(output_mode="audio", container="auto", audio_codec="lpcm",
                                          audio_bit_depth="24"), sources, {"pcm_s24le"})
            probe = self.engine._ffprobe_media(self.audio)
            probe["streams"][0]["codec_name"] = "pcm_s24le"
            probe["streams"][0]["bits_per_sample"] = 16
            verify_output(probe, plan)

    def test_no_audio_and_original_audio_only_remuxes(self):
        _, probe = self.process(container="mp4", audio_codec="none")
        self.assertEqual(self.codecs(probe), [("video", "h264")])
        commands_before = len(self.commands)
        path, probe = self.process(audio_only=True)
        self.assertEqual(path.suffix, ".m4a")
        self.assertEqual(self.codecs(probe), [("audio", "aac")])
        self.assertEqual(len(self.commands), commands_before)
        self.assertEqual(path.read_bytes(), self.audio.read_bytes())

    def test_matching_separate_streams_are_preferred_before_encoding(self):
        video = self.folder / "video-only.mp4"
        subprocess.run([self.ffmpeg, "-y", "-i", str(self.source), "-c:v", "copy", "-an", str(video)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **NO_WINDOW)
        info = download_engine.YTInfo("offline", {"title": "Preferred", "duration": .6, "formats": [
            dict(format_id="progressive", ext="webm", vcodec="av01", acodec="opus", height=96),
            dict(format_id="h264", ext="mp4", vcodec="avc1", acodec="none", height=96),
            dict(format_id="opus", ext="webm", vcodec="none", acodec="opus", abr=192),
            dict(format_id="aac", ext="m4a", vcodec="none", acodec="mp4a", abr=128)]})
        options = TaskOptions(folder=str(self.folder), output_mode="video", container="mp4",
                              video_codec="h264", audio_codec="aac")
        tid = self.manager.add_urls(["https://example.com/preferred"], options)[0]
        self.manager.tid_to_workerid[tid] = 1
        private = self.folder / "private"
        private.mkdir()
        downloaded = []
        def download(tid, key, url, stream, filename, *, folder, stop_event=None):
            downloaded.append(stream.format_id)
            destination = Path(folder, filename)
            shutil.copyfile(video if stream.format_id == "h264" else self.audio, destination)
            return str(destination)
        with patch.object(self.engine, "_download_format", side_effect=download), \
                patch.object(self.engine, "available_encoders", side_effect=AssertionError("must copy")):
            result = ConfiguredOutput(self.engine).process(tid, 1, "offline", info, asdict(options), str(private), "Preferred")
        self.assertEqual(set(downloaded), {"h264", "aac"})
        self.assertEqual(self.codecs(self.engine._ffprobe_media(result)), [("video", "h264"), ("audio", "aac")])

    def test_original_does_not_silently_transcode_into_an_incompatible_container(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be copied"):
            self.process(container="webm")
        self.assertEqual(self.commands, [])
        self.assertTrue(self.source.is_file())

    def test_missing_encoder_fails_explicitly_and_never_substitutes_a_codec(self):
        with patch.object(self.engine, "available_encoders", return_value=set()), \
                self.assertRaisesRegex(RuntimeError, "no encoder for hevc"):
            self.process(container="mp4", video_codec="hevc")
        self.assertEqual(self.commands, [])

    def test_h264_and_aac_conversion_uses_real_stream_codecs_not_incorrect_metadata(self):
        _, probe = self.process(source=self.webm, container="mp4", video_codec="h264", audio_codec="aac")
        self.assertEqual(self.codecs(probe), [("video", "h264"), ("audio", "aac")])
        self.assertIn("libx264", self.commands[-1])
        self.assertEqual(self.commands[-1][self.commands[-1].index("-c:a") + 1], "aac")

    def test_video_original_worker_preserves_original_container_and_publishes_only_on_success(self):
        options = TaskOptions(folder=str(self.folder), output_mode="video")
        tid = self.manager.add_urls(["https://example.com/automatic"], options)[0]
        source = self.mkv
        def download(tid, key, url, stream, filename, *, folder):
            path = Path(folder, filename)
            shutil.copyfile(source, path)
            return str(path)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info(source)), \
                patch.object(self.engine, "_download_format", side_effect=download):
            self.manager._process_task((tid, "https://example.com/automatic"), 1)
        task = self.manager.get_task(tid)
        self.assertTrue(task["status"].startswith("Completed"), task["status"])
        self.assertEqual(task["progress"], 100)
        self.assertEqual(Path(task["output_path"]).suffix, ".mkv")
        self.assertEqual(self.commands, [])
        self.assertEqual(Path(task["output_path"]).read_bytes(), source.read_bytes())
        self.assertEqual(list(self.folder.iterdir()), [Path(task["output_path"])])

    def test_custom_failure_cleans_workspace_and_can_retry_with_same_profile(self):
        options = TaskOptions(folder=str(self.folder), output_mode="video", container="mp4", video_codec="hevc")
        tid = self.manager.add_urls(["https://example.com/fail"], options)[0]
        def download(tid, key, url, stream, filename, *, folder):
            path = Path(folder, filename)
            shutil.copyfile(self.source, path)
            return str(path)
        with patch('engine_metadata.fetch_ytinfo_with_retry', return_value=self.info(self.source)), \
                patch.object(self.engine, "_download_format", side_effect=download), \
                patch.object(self.engine, "available_encoders", return_value=set()):
            self.manager._process_task((tid, "https://example.com/fail"), 1)
        self.assertTrue(self.manager.get_task(tid)["status"].startswith("Error"))
        self.assertEqual(list(self.folder.iterdir()), [])
        self.manager.retry_failed([tid])
        self.assertEqual(self.manager.get_task(tid)["video_codec"], "hevc")
        self.assertEqual(self.manager.get_task(tid)["progress"], 0)


if __name__ == "__main__":
    unittest.main()
