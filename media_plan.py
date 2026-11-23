"""Immutable media descriptions and output decisions without file or process I/O."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re

from media_options import AUTOMATIC_AUDIO_BITRATES, ENCODERS, can_mux, validate_output_options
from errors import MediaOperationError


@dataclass(frozen=True)
class SourceStream:
    input_number: int
    stream_index: int
    kind: str
    codec: str
    bit_depth: int | None = None
    sample_rate: int | None = None
    channels: int | None = None


@dataclass(frozen=True)
class MediaSources:
    paths: tuple[Path, ...]
    streams: tuple[SourceStream, ...]
    duration: float | None
    format_names: tuple[str, ...] = ()
    major_brands: tuple[str, ...] = ()
    ebml_doctypes: tuple[str | None, ...] = ()
    mp4_faststart: tuple[bool, ...] = ()
    timeline_origin: float = 0.0
    section_downloaded: bool = False


class StreamAction(Enum):
    COPY = "copy"
    ENCODE = "encode"
    DROP = "drop"


@dataclass(frozen=True)
class StreamPlan:
    source: SourceStream | None
    action: StreamAction
    target_codec: str | None
    encoder: str | None = None
    options: tuple[str, ...] = ()
    expected_bit_depth: int | None = None


@dataclass(frozen=True)
class OutputPlan:
    container: str
    extension: str
    video: StreamPlan
    audio: StreamPlan
    preserve_original_file: bool = False
    warnings: tuple[str, ...] = ()
    original_format_names: tuple[str, ...] = ()
    original_major_brand: str = ""

    @property
    def needs_encoders(self):
        return any(s.action == StreamAction.ENCODE and s.encoder is None for s in (self.video, self.audio))


def read_bit_depth(stream):
    """Read encoded precision, not the sample format used by a lossy decoder."""
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            bits = int(stream.get(key) or 0)
        except (ValueError, TypeError):
            continue
        if bits > 0:
            return bits
    match = re.fullmatch(r"pcm_[suf](8|16|24|32|64)(?:le|be)?", stream.get("codec_name", ""))
    return int(match[1]) if match else None


def select_merge_container(video_codecs, audio_codecs):
    """Shared automatic merge policy, also used by the legacy engine path."""
    videos, audios = set(video_codecs), set(audio_codecs)
    if videos and audios:
        if videos <= {"vp8", "vp9", "av1"} and audios <= {"opus", "vorbis"}:
            return "webm"
        if (videos <= {"h264", "hevc", "av1", "mpeg4"}
                and audios <= {"aac", "mp3", "ac3", "eac3", "alac"}):
            return "mp4"
    return "mkv"


def _container(task, sources, video, audio):
    requested = task.get("container", "auto")
    if requested != "auto":
        return requested
    audio_containers = {"aac": "m4a", "mp3": "mp3", "opus": "opus", "flac": "flac"}
    if task["output_mode"] == "audio" and task["audio_codec"] != "original":
        return "wav" if audio.startswith("pcm_") else audio_containers[audio]
    extension = sources.paths[0].suffix[1:].lower()
    if len(sources.paths) == 1 and can_mux(extension, video, audio):
        return extension
    if not video:
        return "wav" if audio.startswith("pcm_") else {**audio_containers, "vorbis": "ogg"}.get(audio, "mka")
    return select_merge_container([video], [audio] if audio else [])


def _video_args(encoder, quality):
    index = ("high", "balanced", "small").index(quality)
    if encoder in ("libx264", "libx265"):
        return ("-crf", str((18, 23, 28)[index]), "-preset", "medium")
    if encoder == "libvpx-vp9":
        return ("-crf", str((24, 32, 40)[index]), "-b:v", "0", "-deadline", "good", "-cpu-used", "2")
    if encoder == "libsvtav1":
        return ("-crf", str((24, 32, 40)[index]), "-preset", "8")
    return ("-crf", str((24, 32, 40)[index]), "-b:v", "0", "-cpu-used", "6")


def _stream_plan(source, target, task, encoders, depth=None):
    if not target:
        return StreamPlan(source, StreamAction.DROP, None)
    convert = target != source.codec or (depth is not None and depth != source.bit_depth)
    if not convert:
        return StreamPlan(source, StreamAction.COPY, target, expected_bit_depth=depth)
    encoder = None
    if encoders is not None:
        choices = ENCODERS.get(target, (target,))
        encoder = next((e for e in choices if e in encoders), None)
        if encoder is None:
            raise MediaOperationError(f"FFmpeg has no encoder for {target}. Install an FFmpeg distribution with "
                               f"{' or '.join(choices)}, or keep the original codec.")
    options = ()
    if source.kind == "video" and encoder is not None:
        options = _video_args(encoder, task["video_quality"])
    elif source.kind == "audio":
        if target in ("aac", "opus", "mp3"):
            bitrate = task["audio_bitrate"]
            if bitrate == "auto":
                bitrate = AUTOMATIC_AUDIO_BITRATES[target]
            options = ("-b:a", bitrate)
        elif target == "flac":
            options = ("-sample_fmt", "s16" if depth == 16 else "s32", "-bits_per_raw_sample", str(depth))
    return StreamPlan(source, StreamAction.ENCODE, target, encoder, options, depth)


def _can_preserve_video(task, sources, container, video, audio):
    """Require actual container evidence and exactly the selected, unchanged streams."""
    if (task["output_mode"] != "video" or len(sources.paths) != 1
            or (task.get("trim_start") or "").strip() or (task.get("trim_end") or "").strip()
            or video.action != StreamAction.COPY
            or audio.action == StreamAction.ENCODE
            or audio.action == StreamAction.DROP and audio.source is not None
            or len(sources.streams) != (2 if audio.source is not None else 1)
            or not all(stream in (video.source, audio.source) for stream in sources.streams)
            or any(stream.input_number != 0 for stream in sources.streams)
            or sources.paths[0].suffix[1:].lower() != container
            or not sources.format_names):
        return False
    names = set(sources.format_names[0].split(","))
    brand = sources.major_brands[0] if sources.major_brands else ""
    if container == "mp4":
        return ("mp4" in names and bool(brand) and brand != "qt"
                and bool(sources.mp4_faststart) and sources.mp4_faststart[0])
    if container == "mov":
        return "mov" in names and brand == "qt"
    if container in ("mkv", "webm"):
        expected = "matroska" if container == "mkv" else "webm"
        return (expected in names and bool(sources.ebml_doctypes)
                and sources.ebml_doctypes[0] == expected)
    return False


def build_output_plan(task, sources, available_encoders=None):
    """Plan using actual streams. None defers encoder resolution until encoding is needed."""
    validate_output_options(task)
    audio_only = task["output_mode"] == "audio"
    want_video, want_audio = task.get("video_codec", "original"), task.get("audio_codec", "original")
    video = next((s for s in sources.streams if s.kind == "video"), None)
    audio = next((s for s in sources.streams if s.kind == "audio"), None)
    if not audio_only and video is None:
        raise MediaOperationError("The source has no video stream. Choose Audio output.")
    if (audio_only or want_audio not in ("original", "none")) and audio is None:
        raise MediaOperationError("The requested output requires an audio stream, but the source has none.")
    source_video, source_audio = video.codec if video else "", audio.codec if audio else ""
    target_video = "" if audio_only else source_video if want_video == "original" else want_video
    target_audio = "" if want_audio == "none" or audio is None else source_audio if want_audio == "original" else want_audio
    depth, warnings = None, ()
    if want_audio in ("flac", "lpcm") and audio:
        requested = task.get("audio_bit_depth", "original")
        depth = audio.bit_depth if requested == "original" else int(requested)
        if depth is None and not (want_audio == "flac" and source_audio == "flac"):
            depth = 24
            warnings = ("Source bit depth is unavailable; using 24-bit output.",)
        if want_audio == "lpcm":
            target_audio = (source_audio if requested == "original" and source_audio.startswith("pcm_")
                            else {8: "pcm_u8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}.get(depth))
            if target_audio is None:
                raise MediaOperationError("Cannot preserve this source bit depth as LPCM. Choose 16-bit or 24-bit.")
    preserve = (audio_only and task["container"] == "auto" and want_audio == "original"
                and len(sources.paths) == 1 and len(sources.streams) == 1 and not video)
    if preserve:
        extension = sources.paths[0].suffix[1:]
        formats = tuple(sources.format_names[0].split(",")) if sources.format_names else ()
        brand = sources.major_brands[0] if sources.major_brands else ""
        return OutputPlan(formats[0] if formats else extension, extension,
                          StreamPlan(None, StreamAction.DROP, None),
                          StreamPlan(audio, StreamAction.COPY, source_audio), True,
                          original_format_names=formats, original_major_brand=brand)
    container = _container(task, sources, target_video, target_audio)
    if not can_mux(container, target_video, target_audio):
        raise MediaOperationError(f"The original streams ({source_video or 'no video'} / {source_audio or 'no audio'}) "
                           f"cannot be copied into the {container.upper()} output profile. "
                           "Select compatible codecs or use Automatic/MKV.")
    convert_audio = bool(target_audio and (target_audio != source_audio or (depth is not None and depth != audio.bit_depth)))
    if convert_audio and target_audio == "flac" and depth not in (16, 24):
        raise MediaOperationError("Cannot preserve this source bit depth with FLAC conversion. Choose 16-bit or 24-bit.")
    video_plan = _stream_plan(video, target_video, task, available_encoders)
    audio_plan = _stream_plan(audio, target_audio, task, available_encoders, depth)
    preserve_video = _can_preserve_video(task, sources, container, video_plan, audio_plan)
    return OutputPlan(container, container, video_plan, audio_plan, preserve_video, warnings=warnings,
                      original_format_names=tuple(sources.format_names[0].split(",")) if preserve_video else (),
                      original_major_brand=(sources.major_brands[0] if sources.major_brands else "")
                      if preserve_video else "")
