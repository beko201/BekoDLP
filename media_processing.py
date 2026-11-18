"""Execute output plans with the engine's existing process and file management."""
import os
import math

from media_options import canonical_codec, validate_output_options
from media_plan import StreamAction, build_output_plan, read_bit_depth
from media_probe import probe_duration
from media_sources import SourcePreparer, read_ebml_doctype, read_mp4_faststart
from utils import DownloadCancelled, InsufficientDiskSpace, validate_trim_range, log_warning
from errors import MediaOperationError


def build_ffmpeg_command(ffmpeg, sources, plan, destination, *, trim=None):
    """Translate a resolved plan into arguments without touching files or processes."""
    if plan.preserve_original_file and trim is None:
        raise ValueError("An original-file preservation plan does not need FFmpeg.")
    if plan.needs_encoders:
        raise ValueError("Resolve the required encoders before building the FFmpeg command.")
    cmd = [ffmpeg, "-y"]
    for path in sources.paths:
        # Seek every input to the same source time before decoding/encoding.
        if trim is not None and trim[0] is not None:
            cmd += ["-ss", str(trim[0])]
        cmd += ["-i", str(path)]
    if trim is not None:
        # Discard audio packet/cluster preroll as well as encoder delay. Copied
        # video must already have been aligned to a keyframe by the executor.
        cmd += ['-ss', '0']
    if trim is not None and trim[1] is not None:
        cmd += ["-t", str(trim[1] - (trim[0] or 0))]
    for kind, stream in (("v", plan.video), ("a", plan.audio)):
        if stream.action == StreamAction.DROP:
            cmd += ["-vn" if kind == "v" else "-an"]
            continue
        source = stream.source
        cmd += ["-map", f"{source.input_number}:{source.stream_index}", "-c:" + kind,
                "copy" if stream.action == StreamAction.COPY else stream.encoder]
        cmd += stream.options
    if plan.container in ("mp4", "m4a"):
        cmd += ["-movflags", "+faststart"]
    if plan.container in ("opus", "ogg") and plan.audio.target_codec == "opus":
        cmd += ["-strict", "-2"]
    return cmd + [str(destination)]


def verify_output(probe, plan, *, ebml_doctype=None):
    """Check stream/format evidence; Matroska/WebM also require the actual EBML DocType."""
    streams = probe.get("streams") or []
    if plan.preserve_original_file and plan.video.action != StreamAction.DROP:
        expected_count = sum(stream.action != StreamAction.DROP for stream in (plan.video, plan.audio))
        if len(streams) != expected_count:
            raise MediaOperationError("The preserved video contains unexpected extra streams.")
    for kind, expected in (("video", plan.video), ("audio", plan.audio)):
        actual = [s for s in streams if s.get("codec_type") == kind]
        codecs = [canonical_codec(s.get("codec_name")) for s in actual]
        if codecs != ([expected.target_codec] if expected.action != StreamAction.DROP else []):
            raise MediaOperationError("The output streams do not match the requested codecs.")
        if expected.expected_bit_depth is not None and read_bit_depth(actual[0]) != expected.expected_bit_depth:
            raise MediaOperationError("The output audio bit depth does not match the requested bit depth.")
    container = probe.get("format") or {}
    names = set((container.get("format_name") or "").split(","))
    brand = (container.get("tags", {}).get("major_brand") or "").strip().lower()
    matches = True
    if plan.preserve_original_file:
        matches = names == set(plan.original_format_names) and brand == plan.original_major_brand
    if not plan.preserve_original_file or plan.video.action != StreamAction.DROP:
        demuxer = {"mkv": "matroska", "mka": "matroska", "opus": "ogg", "m4a": "mp4"}.get(
            plan.container, plan.container)
        matches = matches and demuxer in names
        # MOV and MP4 share demuxer aliases. The major brand distinguishes QuickTime.
        if plan.container in ("mp4", "m4a"):
            matches = matches and brand != "qt"
        elif plan.container == "mov":
            matches = matches and brand == "qt"
        elif plan.container in ("webm", "mkv", "mka"):
            expected_doctype = "webm" if plan.container == "webm" else "matroska"
            matches = matches and ebml_doctype == expected_doctype
    if not matches:
        raise MediaOperationError("The output container does not match the requested output plan.")


class ConfiguredOutput:
    def __init__(self, engine):
        self.engine = engine
        self.manager = engine.manager
        self.sources = SourcePreparer(engine)

    def process(self, tid, worker, url, info, task, folder, title):
        with self.engine.media_probe_session():
            return self._process(tid, worker, url, info, task, folder, title)

    def _process(self, tid, worker, url, info, task, folder, title):
        validate_output_options(task)
        start, end = validate_trim_range(task.get("trim_start", ""), task.get("trim_end", ""))
        trim = (start, end) if start is not None or end is not None else None
        sources = self.sources.prepare(tid, worker, url, info, task, folder)
        self.check_cancelled(tid)
        plan = build_output_plan(task, sources)
        if plan.needs_encoders:
            plan = build_output_plan(task, sources, self.engine.available_encoders())
        for warning in plan.warnings:
            self.manager.add_warning(tid, warning)
        destination = self.reserve_destination(tid, folder, title, plan)
        if trim is None:
            self.execute_plan(tid, worker, sources, plan, destination, task)
            self.verify_destination(tid, destination, plan)
        else:
            try:
                if sources.duration and (start or 0) >= sources.duration:
                    raise MediaOperationError("The trim start must be earlier than the source duration.")
                effective_trim = trim
                aligned_start = start
                if start and plan.video.action == StreamAction.COPY:
                    video = plan.video.source
                    aligned_start = self.engine._ffprobe_copy_start(
                        sources.paths[video.input_number], video.stream_index, start)
                    self.check_cancelled(tid)
                    effective_trim = (aligned_start, end)
                self.execute_plan(tid, worker, sources, plan, destination, task, trim=effective_trim)
                probe = self.verify_destination(tid, destination, plan)
                duration = probe_duration(probe)
                if not math.isfinite(duration) or duration <= 0:
                    raise MediaOperationError("The trimmed file does not have a positive finite duration.")
                self.check_cancelled(tid)
                if start and aligned_start is not None and start - aligned_start >= .05:
                    self.manager.add_warning(tid,
                        f'Original video trimming starts at {aligned_start:.3f}s instead of {start:.3f}s '
                        'to keep audio and video synchronized at a keyframe. '
                        'Frame-exact trimming requires video re-encoding.')
            except (DownloadCancelled, InsufficientDiskSpace):
                raise
            except Exception as error:
                # Source files are retained until publication, so a failed cut can
                # reconstruct the full requested output without downloading again.
                self.check_cancelled(tid)
                log_warning("Trimming failed; preserving the full file", error,
                            [("task", tid), ("file", destination)])
                if os.path.exists(destination):
                    os.remove(destination)
                self.execute_plan(tid, worker, sources, plan, destination, task)
                self.verify_destination(tid, destination, plan)
                self.manager.add_warning(tid, "Trimming could not be applied; the full file was saved")
            self.manager.report_stage_progress(tid, "trimming", 1)
        self.manager.report_stage_progress(tid, "verification", 1)
        return destination

    def verify_destination(self, tid, destination, plan):
        self.check_cancelled(tid)
        probe = self.engine._ffprobe_media(destination, fresh=True)
        doctype = (read_ebml_doctype(destination)
                   if plan.container in ("webm", "mkv", "mka") else None)
        verify_output(probe, plan, ebml_doctype=doctype)
        if (plan.preserve_original_file and plan.video.action != StreamAction.DROP
                and plan.container == "mp4" and not read_mp4_faststart(destination)):
            raise MediaOperationError("The preserved MP4 does not have faststart layout.")
        self.check_cancelled(tid)
        return probe

    def check_cancelled(self, tid):
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()

    def reserve_destination(self, tid, folder, title, plan):
        suffix = "." + plan.extension if plan.extension else ""
        return self.engine._dedupe_path(os.path.join(folder, title + suffix), tid)

    def execute_plan(self, tid, worker, sources, plan, destination, task, *, trim=None):
        if plan.preserve_original_file and trim is None:
            self.engine._replace_media(sources.paths[0], destination)
            return
        command = build_ffmpeg_command(self.engine.ffmpeg, sources, plan, destination, trim=trim)
        converting = any(s.action == StreamAction.ENCODE for s in (plan.video, plan.audio))
        reserve = sum(os.path.getsize(path) for path in sources.paths) * (4 if converting else 1.1)
        audio = plan.audio
        if (audio.target_codec or "").startswith("pcm_") and audio.source:
            reserve = max(reserve, (sources.duration or 0) * (audio.source.sample_rate or 192000)
                          * (audio.source.channels or 8) * ((audio.expected_bit_depth or 64) / 8))
        # The output is cut in this pass; a second full-size trim file is unnecessary.
        self.engine._check_disk_space(os.path.dirname(destination), reserve)
        label = "Converting selected codecs" if converting else "Copying streams (remuxing)"
        duration = sources.duration or 0
        if trim is not None:
            start, end = trim
            stop = end if end is not None else duration
            if duration:
                stop = min(stop, duration)
            duration = max(0, stop - (start or 0))
            label += " and trimming"
        self.engine._run_ffmpeg(tid, worker, command, duration=duration, status_label=label)
