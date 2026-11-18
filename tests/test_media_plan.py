"""Output decisions and verification without downloading or starting FFmpeg."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import unittest
from unittest.mock import Mock

from media_options import OUTPUT_DEFAULTS
from media_plan import MediaSources, SourceStream, StreamAction, build_output_plan, read_bit_depth
from media_processing import build_ffmpeg_command, verify_output
from media_sources import SourcePreparer


class OutputPlanTests(unittest.TestCase):
    def sources(self, video="h264", audio="aac", depth=None, extension="mkv"):
        streams = []
        if video:
            streams.append(SourceStream(0, 3, "video", video))
        if audio:
            streams.append(SourceStream(0, 7, "audio", audio, depth, 48000, 2))
        return MediaSources((Path("source." + extension),), tuple(streams), 30,
                            ("matroska,webm",), ("",))

    def task(self, **changes):
        return {**OUTPUT_DEFAULTS, "quality": "1080p", **changes}

    def test_matching_codecs_copy_without_encoders_or_conversion_options(self):
        plan = build_output_plan(self.task(container="mp4", video_codec="h264", audio_codec="aac"),
                                 self.sources(), set())
        self.assertEqual((plan.video.action, plan.audio.action), (StreamAction.COPY, StreamAction.COPY))
        self.assertFalse(plan.needs_encoders)
        self.assertEqual(plan.audio.options, ())
        with self.assertRaises(FrozenInstanceError):
            plan.container = "wav"

    def test_mixed_copy_and_encode_only_requires_the_audio_encoder(self):
        sources = self.sources(audio="opus")
        task = self.task(container="mp4", video_codec="h264", audio_codec="aac", audio_bitrate="256k")
        pending = build_output_plan(task, sources)
        self.assertTrue(pending.needs_encoders)
        with self.assertRaisesRegex(ValueError, "Resolve"):
            build_ffmpeg_command("ffmpeg", sources, pending, "output.mp4")
        plan = build_output_plan(task, sources, {"aac"})
        self.assertEqual(plan.video.action, StreamAction.COPY)
        self.assertEqual((plan.audio.action, plan.audio.encoder), (StreamAction.ENCODE, "aac"))
        cmd = build_ffmpeg_command("ffmpeg", sources, plan, "output.mp4")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "256k")

    def test_automatic_audio_bitrate_resolves_for_encoding_and_is_ignored_for_copy(self):
        for codec, encoder, bitrate in (('mp3', 'libmp3lame', '192k'), ('aac', 'aac', '192k'),
                                        ('opus', 'libopus', '128k')):
            with self.subTest(codec=codec):
                task = self.task(output_mode='audio', audio_codec=codec, audio_bitrate='auto')
                sources = self.sources(video=None, audio='pcm_s16le', depth=16, extension='wav')
                plan = build_output_plan(task, sources, {encoder})
                cmd = build_ffmpeg_command('ffmpeg', sources, plan, 'output.' + plan.extension)
                self.assertEqual(cmd[cmd.index('-b:a') + 1], bitrate)
                copied_sources = self.sources(video=None, audio=codec)
                copied = build_output_plan(task, copied_sources, set())
                self.assertEqual(copied.audio.action, StreamAction.COPY)
                cmd = build_ffmpeg_command('ffmpeg', copied_sources, copied, 'output.' + copied.extension)
                self.assertNotIn('-b:a', cmd)

    def test_command_uses_input_numbers_and_absolute_stream_indices(self):
        sources = MediaSources((Path("v.mkv"), Path("a.mka")), (
            SourceStream(0, 4, "video", "h264"), SourceStream(1, 2, "audio", "aac")), 30)
        plan = build_output_plan(self.task(container="mp4"), sources, set())
        cmd = build_ffmpeg_command("ffmpeg", sources, plan, "output.mp4")
        self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"], ["0:4", "1:2"])
        self.assertEqual([cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"], ["v.mkv", "a.mka"])

    def test_original_does_not_transcode_to_satisfy_incompatible_container(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be copied"):
            build_output_plan(self.task(container="webm"), self.sources(), {"libvpx-vp9", "libopus"})

    def test_missing_required_audio_and_video_fail_before_encoder_resolution(self):
        for task, sources, error in (
                (self.task(), self.sources(video=None), "no video"),
                (self.task(audio_codec="aac"), self.sources(audio=None), "requires an audio"),
                (self.task(output_mode="audio"), self.sources(audio=None), "requires an audio")):
            with self.subTest(task=task), self.assertRaisesRegex(RuntimeError, error):
                build_output_plan(task, sources)

    def test_video_with_no_original_audio_is_valid_and_explicit_drop_removes_mapping(self):
        plan = build_output_plan(self.task(), self.sources(audio=None), set())
        self.assertEqual(plan.audio.action, StreamAction.DROP)
        sources = self.sources()
        plan = build_output_plan(self.task(audio_codec="none"), sources, set())
        cmd = build_ffmpeg_command("ffmpeg", sources, plan, "output.mkv")
        self.assertIn("-an", cmd)
        self.assertNotIn("0:7", cmd)

    def test_missing_encoder_and_av1_preference_do_not_substitute_codecs(self):
        with self.assertRaisesRegex(RuntimeError, "no encoder for hevc"):
            build_output_plan(self.task(video_codec="hevc"), self.sources(), set())
        task = self.task(video_codec="av1")
        for available, expected in (({"libsvtav1", "libaom-av1"}, "libsvtav1"),
                                    ({"libaom-av1"}, "libaom-av1")):
            plan = build_output_plan(task, self.sources(), available)
            self.assertEqual(plan.video.encoder, expected)

    def test_matching_flac_depth_change_encodes_and_original_depth_copies(self):
        sources = self.sources(video=None, audio="flac", depth=24, extension="flac")
        task = self.task(output_mode="audio", audio_codec="flac", audio_bit_depth="16")
        plan = build_output_plan(task, sources, {"flac"})
        self.assertEqual(plan.audio.action, StreamAction.ENCODE)
        self.assertEqual(plan.audio.expected_bit_depth, 16)
        self.assertIn("s16", plan.audio.options)
        plan = build_output_plan({**task, "audio_bit_depth": "original"}, sources, set())
        self.assertEqual(plan.audio.action, StreamAction.COPY)
        self.assertEqual(plan.audio.expected_bit_depth, 24)

    def test_lpcm_original_precision_and_unknown_precision_fallback(self):
        task = self.task(output_mode="audio", audio_codec="lpcm")
        plan = build_output_plan(task, self.sources(video=None, audio="flac", depth=24), {"pcm_s24le"})
        self.assertEqual((plan.container, plan.audio.target_codec, plan.audio.expected_bit_depth),
                         ("wav", "pcm_s24le", 24))
        self.assertEqual(plan.warnings, ())
        plan = build_output_plan(task, self.sources(video=None), {"pcm_s24le"})
        self.assertEqual(plan.audio.expected_bit_depth, 24)
        self.assertEqual(len(plan.warnings), 1)
        self.assertNotIn("-b:a", plan.audio.options)

    def test_unsupported_original_precision_is_not_silently_reduced(self):
        with self.assertRaisesRegex(RuntimeError, "Cannot preserve"):
            build_output_plan(self.task(output_mode="audio", audio_codec="flac"),
                              self.sources(video=None, audio="pcm_s32le", depth=32), {"flac"})

    def test_original_single_audio_preserves_unlisted_container_without_command(self):
        sources = replace(self.sources(video=None, audio="pcm_s24be", depth=24, extension="aiff"),
                          format_names=("aiff",))
        plan = build_output_plan(self.task(output_mode="audio"), sources, set())
        self.assertTrue(plan.preserve_original_file)
        self.assertEqual(plan.extension, "aiff")
        with self.assertRaisesRegex(ValueError, "does not need FFmpeg"):
            build_ffmpeg_command("ffmpeg", sources, plan, "output.aiff")
        # An extra stream precludes byte-for-byte preservation, as before.
        sources = replace(sources, streams=sources.streams + (SourceStream(0, 8, "data", "bin_data"),))
        with self.assertRaisesRegex(RuntimeError, "cannot be copied"):
            build_output_plan(self.task(output_mode="audio"), sources)

    def test_single_video_preservation_requires_exact_streams_and_container_evidence(self):
        sources = replace(self.sources(), ebml_doctypes=("matroska",))
        for container in ("auto", "mkv"):
            with self.subTest(container=container):
                plan = build_output_plan(self.task(container=container), sources, set())
                self.assertTrue(plan.preserve_original_file)
                self.assertEqual(plan.original_format_names, ("matroska", "webm"))
                with self.assertRaisesRegex(ValueError, "does not need FFmpeg"):
                    build_ffmpeg_command("ffmpeg", sources, plan, "output.mkv")
        silent = replace(sources, streams=(sources.streams[0],))
        self.assertTrue(build_output_plan(self.task(audio_codec="none"), silent).preserve_original_file)
        for changes, candidate in (
                ({"trim_start": "0"}, sources), ({"trim_end": "1"}, sources),
                ({"audio_codec": "none"}, sources), ({"video_codec": "hevc"}, sources),
                ({"container": "mp4"}, sources), ({}, replace(sources, ebml_doctypes=())),
                ({}, replace(sources, streams=sources.streams + (SourceStream(0, 8, "subtitle", "ass"),))),
                ({}, replace(sources, streams=sources.streams + (SourceStream(0, 9, "audio", "aac"),))),
                ({}, replace(sources, paths=(Path("v.mkv"), Path("a.mka")))),
        ):
            with self.subTest(changes=changes, sources=candidate):
                self.assertFalse(build_output_plan(self.task(**changes), candidate).preserve_original_file)

    def test_mp4_preservation_requires_faststart_and_actual_brand(self):
        sources = replace(self.sources(extension="mp4"), format_names=("mov,mp4,m4a,3gp,3g2,mj2",),
                          major_brands=("isom",), mp4_faststart=(True,))
        self.assertTrue(build_output_plan(self.task(container="mp4", video_codec="h264", audio_codec="aac"),
                                          sources).preserve_original_file)
        for candidate in (replace(sources, mp4_faststart=()), replace(sources, mp4_faststart=(False,)),
                          replace(sources, major_brands=("qt",)), replace(sources, major_brands=("",)),
                          replace(sources, format_names=())):
            with self.subTest(sources=candidate):
                self.assertFalse(build_output_plan(self.task(container="mp4"), candidate).preserve_original_file)
        mov = replace(sources, paths=(Path("source.mov"),), major_brands=("qt",), mp4_faststart=())
        self.assertTrue(build_output_plan(self.task(), mov).preserve_original_file)

    def test_container_verification_uses_format_and_quicktime_brand(self):
        plan = build_output_plan(self.task(container="mp4"), self.sources(), set())
        probe = {"streams": [{"codec_type": "video", "codec_name": "h264"},
                             {"codec_type": "audio", "codec_name": "aac"}],
                 "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"major_brand": "isom"}}}
        verify_output(probe, plan)
        for wrong in ({"format_name": "matroska,webm"},
                      {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"major_brand": "qt  "}}):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(RuntimeError, "container"):
                verify_output({**probe, "format": wrong}, plan)

    def test_verify_rejects_wrong_codecs_extra_audio_and_wrong_precision(self):
        sources = self.sources(video=None, audio="flac", depth=24, extension="flac")
        plan = build_output_plan(self.task(output_mode="audio", audio_codec="flac"), sources, set())
        stream = {"codec_type": "audio", "codec_name": "flac", "bits_per_raw_sample": "24"}
        probe = {"streams": [stream], "format": {"format_name": "flac"}}
        verify_output(probe, plan)
        for streams in ([{**stream, "codec_name": "aac"}], [stream, stream],
                        [{**stream, "bits_per_raw_sample": "16"}],
                        [stream, {"codec_type": "video", "codec_name": "h264"}]):
            with self.subTest(streams=streams), self.assertRaises(RuntimeError):
                verify_output({**probe, "streams": streams}, plan)

    def test_decoder_float_sample_format_is_not_original_bit_depth(self):
        self.assertIsNone(read_bit_depth({"codec_name": "aac", "sample_fmt": "fltp", "bits_per_sample": 0}))
        self.assertEqual(read_bit_depth({"codec_name": "flac", "sample_fmt": "s32", "bits_per_raw_sample": "24"}), 24)


class SourcePreparationTests(unittest.TestCase):
    def test_inspection_retains_actual_indices_in_multiple_inputs(self):
        engine = Mock()
        engine._ffprobe_media.side_effect = [
            {"streams": [{"index": 4, "codec_type": "video", "codec_name": "h264"}],
             "format": {"format_name": "matroska,webm", "duration": "12"}},
            {"streams": [{"index": 2, "codec_type": "audio", "codec_name": "flac",
                          "bits_per_raw_sample": "24", "sample_rate": "48000", "channels": 2}],
             "format": {"format_name": "flac"}}]
        sources = SourcePreparer(engine).inspect_sources(("v.mkv", "a.flac"), None)
        self.assertEqual([(s.input_number, s.stream_index) for s in sources.streams], [(0, 4), (1, 2)])
        self.assertEqual((sources.duration, sources.streams[1].bit_depth, sources.streams[1].sample_rate), (12, 24, 48000))
        engine._download_format.assert_not_called()
        engine._ffprobe_duration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
