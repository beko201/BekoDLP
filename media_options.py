"""Output profiles shared by preferences, task snapshots and the UI."""
import re

VIDEO_CODECS = {"original": "Original", "h264": "H.264", "hevc": "H.265 / HEVC",
                "vp9": "VP9", "av1": "AV1"}
AUDIO_CODECS = {"original": "Original", "aac": "AAC", "mp3": "MP3", "opus": "Opus",
                "flac": "FLAC", "lpcm": "LPCM", "none": "No audio"}
CONTAINERS = {"auto": "Automatic", "mp4": "MP4", "mkv": "MKV", "webm": "WebM",
              "m4a": "M4A", "mp3": "MP3", "opus": "Ogg Opus (.opus)",
              "flac": "FLAC", "wav": "WAV"}
VIDEO_CONTAINERS = ("auto", "mp4", "mkv", "webm")
AUDIO_CONTAINERS = ("auto", "m4a", "mp3", "opus", "webm", "flac", "wav")
ENCODERS = {"h264": ("libx264",), "hevc": ("libx265",), "vp9": ("libvpx-vp9",),
            "av1": ("libsvtav1", "libaom-av1"), "aac": ("aac",), "mp3": ("libmp3lame",),
            "opus": ("libopus",), "flac": ("flac",), "lpcm": ("pcm_s16le", "pcm_s24le")}
OUTPUT_DEFAULTS = dict(output_mode="video", container="auto", video_codec="original",
                       audio_codec="original", audio_bit_depth="original", audio_bitrate="192k", video_quality="balanced",
                       trim_download_mode='section')
AUDIO_BIT_DEPTHS = ("original", "16", "24")
AUTOMATIC_AUDIO_BITRATES = {"mp3": "192k", "aac": "192k", "opus": "128k"}
AUDIO_BITRATES = ("auto", "64k", "96k", "128k", "160k", "192k", "256k", "320k")


def canonical_codec(value):
    value = (value or "").lower()
    for prefix, codec in (("avc1", "h264"), ("hev1", "hevc"), ("hvc1", "hevc"),
                          ("vp09", "vp9"), ("av01", "av1"), ("mp4a", "aac")):
        if value.startswith(prefix):
            return codec
    return value


def resolution_height(value):
    match = re.fullmatch(r"(144|240|360|480|720|1080|1440|2160)p(?:\([24]K\))?", value or "")
    if match is None:
        raise ValueError("Invalid resolution limit.")
    return int(match[1])


def allowed_codecs(mode, container):
    if mode == "audio":
        audio = {"m4a": {"original", "aac"},
                 "mp3": {"original", "mp3"}, "opus": {"original", "opus"},
                 "webm": {"original", "opus"},
                 "flac": {"original", "flac"}, "wav": {"original", "lpcm"}}
        return {"original"}, audio.get(container, set(AUDIO_CODECS) - {"none"})
    if container == "mp4":
        return {"original", "h264", "hevc", "av1"}, {"original", "aac", "mp3", "none"}
    if container == "webm":
        return {"original", "vp9", "av1"}, {"original", "opus", "none"}
    return set(VIDEO_CODECS), {"original", "aac", "opus", "mp3", "flac", "lpcm", "none"}


def validate_output_options(data):
    if data.get('trim_download_mode', 'full') not in ('section', 'full'):
        raise ValueError('Unknown trim download mode.')
    mode = data.get("output_mode", "legacy")
    if mode == "legacy":
        return
    if mode not in ("video", "audio"):
        raise ValueError("Unknown output mode.")
    if mode != "audio":
        resolution_height(data.get("quality", "1080p"))
    container = data.get("container", "auto")
    video = data.get("video_codec", "original")
    audio = data.get("audio_codec", "original")
    containers = AUDIO_CONTAINERS if mode == "audio" else VIDEO_CONTAINERS
    if container not in containers:
        raise ValueError("The selected container is not available for this output mode.")
    if video not in VIDEO_CODECS or audio not in AUDIO_CODECS:
        raise ValueError("Unknown video or audio codec.")
    videos, audios = allowed_codecs(mode, container)
    if video not in videos or audio not in audios:
        selected = AUDIO_CODECS[audio] if mode == "audio" else f"{VIDEO_CODECS[video]} / {AUDIO_CODECS[audio]}"
        raise ValueError(f"{selected} is not supported by the {CONTAINERS[container]} profile. Choose compatible codecs or a different container.")
    if data.get("audio_bit_depth", "original") not in AUDIO_BIT_DEPTHS:
        raise ValueError("Invalid audio bit depth.")
    if data.get("audio_bitrate", "192k") not in AUDIO_BITRATES:
        raise ValueError("Invalid audio bitrate.")
    if data.get("video_quality", "balanced") not in ("high", "balanced", "small"):
        raise ValueError("Invalid video quality preset.")


def can_mux(container, video, audio):
    """Conservative application profiles; this is not an exhaustive muxer specification."""
    video, audio = canonical_codec(video), canonical_codec(audio)
    if container in ("mkv", "mka"):
        return True
    if container == "m4a":
        return not video and audio in {"aac", "alac"}
    if container in ("mp4", "mov"):
        return (not video or video in {"h264", "hevc", "av1", "mpeg4"}) and (
            not audio or audio in {"aac", "mp3", "alac", "ac3", "eac3"})
    if container == "webm":
        return (not video or video in {"vp8", "vp9", "av1"}) and (not audio or audio in {"opus", "vorbis"})
    return not video and audio in {"aac": {"aac"}, "mp3": {"mp3"}, "opus": {"opus"},
                                    "ogg": {"opus", "vorbis", "flac"}, "flac": {"flac"},
                                    "wav": {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le", "pcm_u8"}}.get(container, set())
