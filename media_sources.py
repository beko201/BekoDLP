"""Source selection, managed downloads and inspection of actual downloaded media."""
from pathlib import Path
import os
import re
import traceback

from media_options import canonical_codec, resolution_height
from media_plan import MediaSources, SourceStream, read_bit_depth
from media_probe import probe_duration
from utils import DownloadCancelled, _network_options, safe_filesize
from errors import MediaOperationError


MAX_EBML_HEADER_BYTES = 64 * 1024
MAX_MP4_ATOMS = 1024


def read_mp4_faststart(path):
    """Check moov-before-mdat using bounded atom headers and seeks, never payload reads."""
    try:
        with open(path, "rb") as stream:
            file_size = os.fstat(stream.fileno()).st_size
            offset = 0
            moov = False
            for _ in range(MAX_MP4_ATOMS):
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    return False
                size, kind = int.from_bytes(header[:4], "big"), header[4:]
                header_size = 8
                if size == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        return False
                    size = int.from_bytes(extended, "big")
                    header_size = 16
                elif size == 0:
                    size = file_size - offset
                if size < header_size or offset + size > file_size:
                    return False
                if kind == b"mdat":
                    return moov
                if kind == b"moov":
                    if moov:
                        return False
                    moov = True
                offset += size
    except OSError:
        pass
    return False


def _ebml_vint(data, offset, *, element_id=False):
    if offset >= len(data) or data[offset] == 0:
        raise ValueError("Missing or invalid EBML variable-length integer.")
    first = data[offset]
    width = 8 - first.bit_length() + 1
    if width > (4 if element_id else 8) or offset + width > len(data):
        raise ValueError("Invalid or truncated EBML variable-length integer.")
    value = int.from_bytes(data[offset:offset + width], "big")
    if not element_id:
        value &= (1 << (7 * width)) - 1
        if value == (1 << (7 * width)) - 1:
            raise ValueError("Unknown-sized elements cannot identify the EBML header.")
    return value, offset + width


def parse_ebml_doctype(data):
    """Inspect only the bounded EBML header, not payload strings or the media body."""
    if data[:4] != b"\x1a\x45\xdf\xa3":
        raise ValueError("Missing EBML header.")
    size, offset = _ebml_vint(data, 4)
    if size > MAX_EBML_HEADER_BYTES:
        raise ValueError("EBML header exceeds the inspection limit.")
    end = offset + size
    if end > len(data):
        raise ValueError("Truncated EBML header.")
    header = data[:end]
    doctype = None
    while offset < end:
        element_id, offset = _ebml_vint(header, offset, element_id=True)
        size, offset = _ebml_vint(header, offset)
        if offset + size > end:
            raise ValueError("EBML element extends beyond its header.")
        if element_id == 0x4282:
            if doctype is not None:
                raise ValueError("Duplicate EBML DocType.")
            doctype = header[offset:offset + size].rstrip(b"\x00").decode("ascii")
            if not doctype:
                raise ValueError("Empty EBML DocType.")
        offset += size
    if doctype is None:
        raise ValueError("Missing EBML DocType.")
    return doctype


def read_ebml_doctype(path):
    """Read enough for the header plus its maximum 12-byte ID/size prefix."""
    try:
        with open(path, "rb") as stream:
            return parse_ebml_doctype(stream.read(MAX_EBML_HEADER_BYTES + 12))
    except (OSError, ValueError) as error:
        raise MediaOperationError(f"EBML DocType could not be verified: {error}") from error


class SourcePreparer:
    def __init__(self, engine):
        self.engine = engine
        self.manager = engine.manager

    def prepare(self, tid, worker, url, info, task, folder):
        selection = self.select_sources(info, task)
        paths = self.download_sources(tid, worker, url, info, selection, task, folder)
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()
        sources = self.inspect_sources(paths, info.duration)
        if sources.duration and sources.duration != info.duration:
            info.duration = sources.duration
            self.manager.update_metadata(tid, duration=info.duration)
        return sources

    @staticmethod
    def select_sources(info, task):
        want_video, want_audio = task.get("video_codec", "original"), task.get("audio_codec", "original")
        if task["output_mode"] == "audio":
            stream = SourcePreparer._prefer(info.audio_only_formats(), want_audio, "audio_codec")
            return (stream,) if stream else ()
        height = resolution_height(task["quality"])
        progressive = info.progressive_formats(max_height=height)
        videos = info.video_only_formats(max_height=height)
        best = max((s.height for s in progressive + videos), default=None)
        progressive = [s for s in progressive if s.height == best]
        videos = [s for s in videos if s.height == best]
        candidates = progressive + videos
        if want_video != "original":
            matching = [s for s in candidates if canonical_codec(s.video_codec) == want_video]
            candidates = matching or candidates
        complete = next((s for s in candidates if s.is_progressive and want_audio != "none"
                         and (want_audio == "original" or canonical_codec(s.audio_codec) == want_audio)), None)
        video = complete or next((s for s in candidates if s.only_video), None) or next(iter(candidates), None)
        if video is None:
            return ()
        if video.only_video and want_audio != "none":
            audio = SourcePreparer._prefer(info.audio_only_formats(), want_audio, "audio_codec")
            if audio:
                return video, audio
        return (video,)

    def download_sources(self, tid, worker, url, info, selection, task, folder):
        if not selection:
            return (self._selector(tid, worker, url, info, task, folder),)
        if len(selection) == 2:
            video, audio = selection
            self.engine._check_disk_space(folder, safe_filesize(video) + safe_filesize(audio))
            return tuple(self.engine._download_streams_parallel(
                tid, url, video, audio, "video." + self._extension(video.ext),
                "audio." + self._extension(audio.ext), folder=folder))
        stream = selection[0]
        self.engine._check_disk_space(folder, safe_filesize(stream))
        return (self.engine._download_format(
            tid, "audio" if task["output_mode"] == "audio" else "video", url, stream,
            "source." + self._extension(stream.ext), folder=folder),)

    def inspect_sources(self, paths, duration):
        probes = [self.engine._ffprobe_media(str(path)) for path in paths]
        duration = probe_duration(probes[0]) or duration or 0
        streams = tuple(SourceStream(
            input_number, stream["index"], stream.get("codec_type", ""), canonical_codec(stream.get("codec_name")),
            read_bit_depth(stream), self._positive_int(stream.get("sample_rate")),
            self._positive_int(stream.get("channels")))
            for input_number, probe in enumerate(probes) for stream in probe["streams"])
        names = tuple(probe["format"].get("format_name", "") for probe in probes)
        doctype, faststart = None, False
        # Only a single video input can bypass processing. Missing optional
        # evidence conservatively keeps the normal remux/conversion path.
        if len(paths) == 1 and any(stream.kind == "video" for stream in streams):
            if {"matroska", "webm"}.intersection(names[0].split(",")):
                try:
                    doctype = read_ebml_doctype(paths[0])
                except MediaOperationError:
                    pass
            if "mp4" in names[0].split(","):
                faststart = read_mp4_faststart(paths[0])
        return MediaSources(tuple(Path(path) for path in paths), streams, duration,
                            names,
                            tuple((probe["format"].get("tags", {}).get("major_brand") or "").strip().lower()
                                  for probe in probes),
                            ebml_doctypes=(doctype,) if len(paths) == 1 else (),
                            mp4_faststart=(faststart,) if len(paths) == 1 else ())

    @staticmethod
    def _positive_int(value):
        try:
            value = int(value or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _prefer(streams, codec, attribute):
        return next((s for s in streams if (canonical_codec(getattr(s, attribute, "")) == codec
                    or codec == "lpcm" and canonical_codec(getattr(s, attribute, "")).startswith("pcm_"))),
                    None) or next(iter(streams), None)

    @staticmethod
    def _extension(value):
        return value if re.fullmatch(r"[a-zA-Z0-9_]{1,12}", value or "") else "bin"

    def _selector(self, tid, worker, url, info, task, folder):
        from download_engine import ManagedYoutubeDL
        height = 0 if task["output_mode"] == "audio" else resolution_height(task["quality"])
        selector = "ba/b" if task["output_mode"] == "audio" else (
            f"bv*[height<={height}]+ba/b[height<={height}]" if task["audio_codec"] != "none"
            else f"bv[height<={height}]/b[height<={height}]")
        def hook(data):
            self.engine._wait_for_download_resume(tid)
            if data.get("status") in ("downloading", "finished"):
                finished = data.get("status") == "finished"
                downloaded = data.get("downloaded_bytes") or 0
                total = downloaded if finished else data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                self.engine._check_download_disk_space(tid, folder, downloaded, total)
                self.manager.report_progress(tid, (data.get("info_dict") or {}).get("format_id") or "source",
                                             downloaded, total, force=finished)
        options = dict(quiet=True, noprogress=True, noplaylist=True, format=selector,
                       outtmpl=os.path.join(folder, "source.%(ext)s"), progress_hooks=[hook],
                       merge_output_format="mkv", ffmpeg_location=self.engine.ffmpeg)
        options.update(_network_options(self.manager.task_control(tid)[0]))
        try:
            streams = 2 if task["output_mode"] != "audio" and task["audio_codec"] != "none" else 1
            with self.engine.media_download_session(tid, streams=streams) as network:
                options.update(network)
                with ManagedYoutubeDL(options, lambda cmd: self.engine._run_ffmpeg(
                        tid, worker, cmd, info.duration, "Preparing source streams", check=False,
                        progress_stage="downloading"),
                        request_check=lambda: self.engine._wait_for_download_resume(tid)) as ydl:
                    result = self.engine._download_from_info(ydl, url, info._media_info,
                                                             self.manager.task_control(tid)[0], tid=tid)
        except BaseException as error:
            traceback.clear_frames(error.__traceback__)
            raise
        source = next((str(path) for path in Path(folder).glob("source.*")
                       if path.suffix not in (".part", ".ytdl", ".temp") and len(path.suffixes) == 1), None)
        if source is None:
            raise MediaOperationError("The downloaded source was not found in its private workspace.")
        if os.path.dirname(os.path.realpath(source)) != os.path.realpath(folder):
            raise RuntimeError("The downloaded source is outside the task workspace.")
        if (result or {}).get("duration"):
            self.manager.update_metadata(tid, duration=result["duration"])
            info.duration = result["duration"]
        return source
