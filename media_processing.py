"""Execute output plans with the engine's existing process and file management."""
import os
import math
from copy import copy

from media_options import canonical_codec, validate_output_options
from media_plan import StreamAction, build_output_plan, read_bit_depth, retry_output_plan
from media_probe import probe_duration
from media_sources import SourcePreparer, read_ebml_doctype, read_mp4_faststart
from errors import DownloadCancelled, InsufficientDiskSpace
from time_utils import validate_trim_range
from app_logging import log_warning
from errors import MediaOperationError
from chapter_options import ChapterBatch, ChapterPart, chapter_ranges
from naming_templates import render_naming
from file_utils import safe_filename
from range_options import validate_range_options


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
        ranges = validate_range_options(task)
        if ranges:
            return self.process_ranges(tid, worker, url, info, task, folder, ranges)
        start, end = validate_trim_range(task.get("trim_start", ""), task.get("trim_end", ""))
        trim = (start, end) if start is not None or end is not None else None
        sources = self.sources.prepare(tid, worker, url, info, task, folder)
        origin = sources.timeline_origin if sources.section_downloaded else 0
        if sources.section_downloaded:
            start = max(0, (start or 0) - origin)
            end = max(0, end - origin) if end is not None else None
            trim = (start, end)
            if task.get('subtitle_mode', 'none') != 'none' or task.get('chapter_mode', 'none') != 'none':
                self.engine._record_subtitle_start(tid, origin)
        self.check_cancelled(tid)
        plan = build_output_plan(task, sources)
        if plan.needs_encoders:
            plan = build_output_plan(task, sources, self.engine.available_encoders())
        for warning in plan.warnings:
            self.manager.add_warning(tid, warning)
        if task.get('chapter_mode', 'none') == 'split':
            batch = self.split_chapters(tid, worker, info, task, folder, sources, plan, origin, start, end)
            if batch is not None:
                return batch
        destination = self.reserve_destination(tid, folder, title, plan)
        if trim is None:
            destination, plan = self.execute_with_fallback(tid, worker, sources, plan, destination, task, folder, title)
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
                if task.get('subtitle_mode', 'none') != 'none' or task.get('chapter_mode', 'none') != 'none':
                    self.engine._record_subtitle_start(tid, origin + (effective_trim[0] or 0))
                if start and aligned_start is not None and start - aligned_start >= .05:
                    self.manager.add_warning(tid,
                        f'Original video trimming starts at {origin + aligned_start:.3f}s instead of {origin + start:.3f}s '
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
                self.manager.add_warning(tid, 'Trimming could not be applied; the downloaded section was saved'
                    if sources.section_downloaded else 'Trimming could not be applied; the full file was saved')
            self.manager.report_stage_progress(tid, "trimming", 1)
        self.manager.report_stage_progress(tid, "verification", 1)
        return destination

    def process_ranges(self, tid, worker, url, info, task, folder, ranges):
        """Download each requested section, or reuse one full source, without cut fallback."""
        original_duration = info.duration
        if original_duration and any(start >= original_duration for start, _ in ranges):
            raise MediaOperationError('A time range starts beyond the source duration.')
        metadata, _ = info._media_info.snapshot()
        name, title_budget = self.batch_naming(task, metadata or {})
        shared = None
        section = task.get('trim_download_mode', 'full') == 'section'
        if not section:
            full_task = dict(task, trim_ranges='', trim_start='', trim_end='')
            shared = self.sources.prepare(tid, worker, url, copy(info), full_task, folder)
        parts = []
        for index, (start, end) in enumerate(ranges, 1):
            self.check_cancelled(tid)
            self.engine._wait_for_download_resume(tid)
            part_folder = os.path.join(folder, f'range-{index:03d}')
            os.mkdir(part_folder)
            subtask = dict(task, trim_ranges='', trim_start=str(start), trim_end=str(end))
            source_info = copy(info)
            source_info.duration = original_duration
            sources = (self.sources.prepare(tid, worker, url, source_info, subtask, part_folder)
                       if section else shared)
            origin = sources.timeline_origin if sources.section_downloaded else 0
            begin = max(0, start - origin)
            finish = end - origin
            if sources.duration:
                if begin >= sources.duration:
                    raise MediaOperationError('A time range starts beyond the source duration.')
                finish = min(finish, sources.duration)
            if finish <= begin:
                raise MediaOperationError('A time range has no media to save.')
            plan = build_output_plan(subtask, sources)
            if plan.needs_encoders:
                plan = build_output_plan(subtask, sources, self.engine.available_encoders())
            for warning in plan.warnings:
                self.manager.add_warning(tid, warning)
            aligned = begin
            if begin and plan.video.action == StreamAction.COPY:
                video = plan.video.source
                aligned = self.engine._ffprobe_copy_start(sources.paths[video.input_number], video.stream_index, begin)
                if begin - aligned >= .05:
                    self.manager.add_warning(tid, f'Range {index} starts at {origin + aligned:.3f}s instead of '
                        f'{start:.3f}s to preserve copied video at a keyframe. Re-encode video for exact cuts.')
            title = safe_filename(f'{index:03d} - {start:.3f}-{end:.3f}')[:title_budget]
            destination = self.reserve_destination(tid, part_folder, title, plan)
            self.execute_plan(tid, worker, sources, plan, destination, subtask,
                              trim=(aligned, finish), strip_chapters=True)
            probe = self.verify_destination(tid, destination, plan)
            duration = probe_duration(probe)
            if not math.isfinite(duration) or duration <= 0:
                raise MediaOperationError('A time range produced an empty file.')
            parts.append(ChapterPart(destination, origin + aligned))
        return ChapterBatch(tuple(parts), name, 'clips')

    @staticmethod
    def batch_naming(task, metadata):
        folders, name = render_naming(task, metadata)
        root_size = len(os.path.abspath(task['folder']).encode('utf-16-le')) // 2
        budget = 240 - root_size - sum(len(part) + 1 for part in folders) - 40
        if budget < 28:
            raise MediaOperationError('The output path is too long. Use a shorter folder template or download folder.')
        name = safe_filename(name[:min(80, budget - 16)])
        return name, min(100, budget - len(name))

    def split_chapters(self, tid, worker, info, task, folder, sources, plan, origin, start, end):
        metadata, _ = info._media_info.snapshot()
        metadata = metadata or {}
        stop = min(end, sources.duration) if end is not None and sources.duration else end or sources.duration
        if not stop or not math.isfinite(stop):
            raise MediaOperationError('The source duration is required to split chapters.')
        chapters = chapter_ranges(metadata, origin + (start or 0), origin + stop)
        if not chapters:
            self.manager.add_warning(tid, 'No source chapters were available in this range; the normal output was kept.')
            return None
        if len(metadata.get('chapters') or []) > 1000:
            self.manager.add_warning(tid, 'Only the first 1000 source chapters were processed.')
        name, title_budget = self.batch_naming(task, metadata)
        parts = []
        for chapter in chapters:
            self.check_cancelled(tid)
            begin, finish = chapter.start - origin, chapter.end - origin
            aligned = begin
            if begin and plan.video.action == StreamAction.COPY:
                video = plan.video.source
                aligned = self.engine._ffprobe_copy_start(sources.paths[video.input_number], video.stream_index, begin)
                if begin - aligned >= .05:
                    self.manager.add_warning(tid, f'Chapter {chapter.number} starts at {origin + aligned:.3f}s instead of '
                        f'{chapter.start:.3f}s to preserve copied video at a keyframe. Re-encode video for exact cuts.')
            title = f'{chapter.number:03d} - ' + safe_filename(chapter.title)[:title_budget]
            destination = self.reserve_destination(tid, folder, title, plan)
            self.execute_plan(tid, worker, sources, plan, destination, task, trim=(aligned, finish))
            probe = self.verify_destination(tid, destination, plan)
            duration = probe_duration(probe)
            if not math.isfinite(duration) or duration <= 0:
                raise MediaOperationError('A chapter file does not have a positive finite duration.')
            parts.append(ChapterPart(destination, origin + aligned))
        return ChapterBatch(tuple(parts), name)

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

    def execute_with_fallback(self, tid, worker, sources, plan, destination, task, folder, title):
        try:
            self.execute_plan(tid, worker, sources, plan, destination, task)
        except (DownloadCancelled, InsufficientDiskSpace):
            raise
        except MediaOperationError:
            self.check_cancelled(tid)
            retry = retry_output_plan(task, plan)
            if retry is None:
                raise
            if retry.needs_encoders:
                retry = retry_output_plan(task, plan, self.engine.available_encoders())
            if os.path.exists(destination):
                os.remove(destination)
            self.engine._release_output_reservation(destination, tid)
            destination = self.reserve_destination(tid, folder, title, retry)
            self.execute_plan(tid, worker, sources, retry, destination, task)
            plan = retry
        return destination, plan

    def reserve_destination(self, tid, folder, title, plan):
        suffix = "." + plan.extension if plan.extension else ""
        return self.engine._dedupe_path(os.path.join(folder, title + suffix), tid)

    def execute_plan(self, tid, worker, sources, plan, destination, task, *, trim=None, strip_chapters=False):
        if plan.preserve_original_file and trim is None:
            self.engine._replace_media(sources.paths[0], destination)
            return
        command = build_ffmpeg_command(self.engine.ffmpeg, sources, plan, destination, trim=trim)
        if strip_chapters or task.get('chapter_mode', 'none') == 'split':
            command[-1:-1] = ['-map_chapters', '-1']
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
        result = self.engine._run_ffmpeg(tid, worker, command, duration=duration, status_label=label)
        if result not in (None, 0):
            self.check_cancelled(tid)
            raise MediaOperationError(f'FFmpeg output processing failed ({result}).')
