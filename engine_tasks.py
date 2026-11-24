"""Download task orchestration; the engine supplies shared services and state."""
import os
import math
import tempfile
from errors import MediaOperationError
from cookie_options import cookie_selection, cookie_kwargs
from network_options import network_selection, network_kwargs
from naming_templates import NAMING_DEFAULTS, render_naming, ensure_naming_folder
from utils import (
    DownloadCancelled,
    safe_filename,
    safe_filesize,
    validate_trim_range,
    remove_leftovers,
    part_matcher,
    log_context,
)
from engine_metadata import YTInfo
import engine_metadata


class TaskDownloadMixin:
    def _process_task_attempt(self, task_tuple, worker_id: int):
        tid, url = task_tuple
        video_tmp = None
        audio_tmp = None
        final_path = None
        out_path = None
        output_paths = []  # Track only output paths reserved for this task.
        output_complete = False
        work_dir = None
        task = self.manager.get_task(tid)
        if task is None:
            return
        output_folder = task["folder"]
        folder = output_folder
        trim_start = task.get("trim_start", "")
        trim_end = task.get("trim_end", "")
        try:
            if self.manager.is_cancelled(tid):
                self.manager.update_task(tid, status="Cancelled")
                return

            validate_trim_range(trim_start, trim_end)
            from media_options import validate_output_options
            validate_output_options(task)
            self._wait_for_download_resume(tid)
            self.manager.update_task(tid, status=f"Worker{worker_id}: Starting", progress=0)
            if not folder:
                raise MediaOperationError("No output folder was selected")

            # Isolate each attempt so reused task IDs cannot affect older or unrelated files.
            work_dir = tempfile.mkdtemp(prefix=f".yd-{tid}-", dir=output_folder)
            folder = work_dir

            resolved = self.manager.take_resolved_info(tid)
            yt = (YTInfo(url, resolved, cookie_selection(task), network_selection(task)) if resolved is not None else
                  engine_metadata.fetch_ytinfo_with_retry(url, cancel_event=self.manager.task_control(tid)[0], **cookie_kwargs(task), **network_kwargs(task)))
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

            self.manager.update_metadata(tid, duration=getattr(yt, 'duration', 0) or 0)
            raw_title = yt.title or "Unnamed"
            if (task.get('subtitle_mode', 'none') != 'none' or task.get('embed_cover', 'off') == 'on'
                    or task.get('chapter_mode', 'none') != 'none'):
                with self.resource_lock:
                    self._subtitle_context[tid] = {'metadata': yt._media_info, 'start': 0}
            title_safe = safe_filename(raw_title)
            if any(task.get(key, default) != default for key, default in NAMING_DEFAULTS.items()):
                folders, stem = render_naming(task, yt._media_info.info)
                with self.resource_lock:
                    self._output_naming[tid] = (folders, stem)
            self.manager.update_task(tid, title=raw_title)
            # Let the UI load the thumbnail asynchronously without blocking the download.
            self.manager.emit("thumbnail", tid, url=getattr(yt, 'thumbnail_url', None))

            fmt = task["format"]
            quality = task["quality"]
            codec_mode = task.get("codec_mode", "Original (Automatic)")
            mode = task.get("output_mode", "legacy")
            if mode in ("video", "audio"):
                from media_processing import ConfiguredOutput
                final_path = ConfiguredOutput(self).process(tid, worker_id, url, yt, task, folder, title_safe)
                from chapter_options import ChapterBatch
                if isinstance(final_path, ChapterBatch):
                    from chapter_processing import publish_chapter_folder
                    batch = final_path
                    stage = tempfile.mkdtemp(prefix='.chapter-output-', dir=folder)
                    for part in batch.parts:
                        self._record_subtitle_start(tid, part.start)
                        self._publish_output(tid, part.path, stage, apply_naming=False)
                    with self.resource_lock:
                        naming = self._output_naming.get(tid)
                    parent = ensure_naming_folder(output_folder, naming[0]) if naming else output_folder
                    final_path = publish_chapter_folder(self, tid, stage, parent, batch.directory_name)
                    output_paths.append(final_path)
                    output_complete = True
                    label = f'{len(batch.parts)} {batch.group_label}'
                    self.manager.update_task(tid, status=f'Completed ({label})', progress=100, output_path=final_path)
                    self.manager.notify(f'{raw_title} downloaded ({label}).')
                    return
                final_path = self._publish_output(tid, final_path, output_folder)
                output_paths.append(final_path)
                output_complete = True
                label = os.path.splitext(final_path)[1][1:].upper()
                self.manager.update_task(tid, status=f"Completed ({label})", progress=100, output_path=final_path)
                self.manager.notify(f"{raw_title} downloaded ({label}).")
                return

            if fmt == "MP3":
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                audio_candidates = yt.audio_only_formats()
                audio_stream = audio_candidates[0] if audio_candidates else None
                if not audio_stream:
                    # Use the yt-dlp selector when no separate audio format is available.
                    self._complete_via_selector(tid, worker_id, url, title_safe, raw_title, "MP3",
                                                None, False, trim_start, trim_end, folder=folder,
                                                output_folder=output_folder, media_info=yt._media_info)
                    return
                source_bytes = safe_filesize(audio_stream)
                output_bytes = self._estimate_mp3_bytes(tid, source_bytes)
                self._check_disk_space(folder, source_bytes, output_bytes=output_bytes,
                                       trim_bytes=output_bytes if trim_start or trim_end else 0)
                # Keep the actual source container extension for conversion.
                src_ext = audio_stream.subtype or "m4a"
                # Record the path before downloading so cancellation can clean partial files.
                audio_tmp = os.path.join(folder, f"{title_safe}_{tid}.{src_ext}")
                tmp_path = self._download_format(tid, 'audio', url, audio_stream,
                                                  f"{title_safe}_{tid}.{src_ext}", folder=folder)
                audio_tmp = tmp_path
                out_mp3 = os.path.join(folder, f"{title_safe}.mp3")
                out_mp3 = self._dedupe_path(out_mp3, tid)
                output_paths.append(out_mp3)
                self.manager.update_task(tid, status=f"Worker{worker_id}: Converting")
                if not self.ffmpeg:
                    raise MediaOperationError("ffmpeg was not found; MP3 conversion is unavailable")
                self._ffmpeg_convert_to_mp3(tid, worker_id, tmp_path, out_mp3)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                out_mp3 = self._maybe_trim(tid, worker_id, out_mp3, trim_start, trim_end)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                out_mp3 = self._publish_output(tid, out_mp3, output_folder)
                output_paths.append(out_mp3)
                output_complete = True
                self.manager.update_task(tid, status="Completed (MP3)", progress=100, output_path=out_mp3)
                self.manager.notify(f"{raw_title} downloaded (MP3).")
                return

            quality_map = {
                "1440p(2K)": "1440p",
                "2160p(4K)": "2160p",
            }
            selected_res = quality_map.get(quality, quality)
            try:
                target_height = int(selected_res.replace('p', ''))
            except (ValueError, TypeError, AttributeError):
                raise MediaOperationError(f"Invalid resolution: {quality}")
            if target_height <= 0:
                raise MediaOperationError(f"Invalid resolution: {quality}")
            force_h264 = (codec_mode == "H.264 (Compatibility)")

            # Treat the selected quality as a ceiling; choose the best height before stream type.
            progressive_candidates = yt.progressive_formats(max_height=target_height)
            video_candidates = yt.video_only_formats(max_height=target_height)
            best_height = max((s.height for s in progressive_candidates + video_candidates), default=None)
            progressive_candidates = [s for s in progressive_candidates if s.height == best_height]
            video_candidates = [s for s in video_candidates if s.height == best_height]

            def is_h264(stream):
                # MP4 can also contain AV1; identify H.264 by its avc1 codec prefix.
                codec = (getattr(stream, 'video_codec', None) or '')
                return codec.startswith('avc1')

            if force_h264:
                # Verify MP4, H.264 video and AAC audio before publishing compatibility output.
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                progressive = next((s for s in progressive_candidates if is_h264(s)), None)
                if progressive:
                    source_bytes = safe_filesize(progressive)
                    self._check_disk_space(folder, source_bytes,
                                           trim_bytes=source_bytes if trim_start or trim_end else 0)
                    out_path = self._dedupe_path(
                        os.path.join(folder, f"{title_safe}_{tid}.mp4"), tid)
                    output_paths.append(out_path)
                    self._download_format(tid, 'progressive', url, progressive, os.path.basename(out_path), folder=folder)
                    if self.manager.is_cancelled(tid):
                        raise DownloadCancelled()
                    out_path = self._maybe_trim(tid, worker_id, out_path, trim_start, trim_end)
                    self._ensure_h264_compatible(tid, worker_id, out_path)
                    if self.manager.is_cancelled(tid):
                        raise DownloadCancelled()
                    out_path = self._publish_output(tid, out_path, output_folder)
                    output_paths.append(out_path)
                    output_complete = True
                    self.manager.update_task(tid, status="Completed (MP4/H.264)", progress=100, output_path=out_path)
                    self.manager.notify(f"{raw_title} downloaded (MP4/H.264).")
                    return

                all_video_candidates = video_candidates

                video_stream = next((s for s in all_video_candidates if is_h264(s)), None)
                source_is_h264 = video_stream is not None
                if not video_stream:
                    video_stream = all_video_candidates[0] if all_video_candidates else None

                audio_candidates = yt.audio_only_formats()
                audio_stream = audio_candidates[0] if audio_candidates else None
                if not video_stream or not audio_stream:
                    # Fall back to yt-dlp selection when no suitable pair of streams is available.
                    self._complete_via_selector(tid, worker_id, url, title_safe, raw_title, "MP4",
                                                target_height, True, trim_start, trim_end, folder=folder,
                                                output_folder=output_folder, media_info=yt._media_info)
                    return

                if not self.ffmpeg or not self.ffprobe:
                    raise MediaOperationError("ffmpeg/ffprobe were not found; video merging is unavailable")

                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()

                required = safe_filesize(video_stream) + safe_filesize(audio_stream)
                # CRF encoding has an uncertain output size, so reserve a larger margin.
                output_bytes = math.ceil(required * (4 if force_h264 and not source_is_h264 else 1.1))
                self._check_disk_space(folder, required, output_bytes=output_bytes,
                                       trim_bytes=output_bytes if trim_start or trim_end else 0)

                v_ext = video_stream.subtype or "mp4"
                a_ext = audio_stream.subtype or "m4a"
                video_tmp, audio_tmp = self._download_streams_parallel(
                    tid, url, video_stream, audio_stream,
                    f"{title_safe}_video_{tid}.{v_ext}", f"{title_safe}_audio_{tid}.{a_ext}",
                    folder=folder
                )

                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()

                final_path = os.path.join(folder, f"{title_safe}.mp4")
                final_path = self._dedupe_path(final_path, tid)
                output_paths.append(final_path)
                duration = self._ffprobe_duration(video_tmp, self.ffprobe)

                audio_info = self._ffprobe_media(audio_tmp)
                audio_tracks = [s for s in audio_info['streams'] if s.get('codec_type') == 'audio']
                if not audio_tracks:
                    raise MediaOperationError("No audio stream was found for merging")
                # Trust the downloaded media rather than the extractor's codec
                # label. AAC can be copied even when the video must be encoded.
                audio_codec_args = (["-c:a", "copy"] if audio_tracks[0].get('codec_name') == 'aac'
                                    else ["-c:a", "aac", "-b:a", "192k"])

                # Copy video identified as H.264; verify the actual output before publishing.
                # Encode other video formats directly with libx264.
                if source_is_h264:
                    video_codec_args = ["-c:v", "copy"]
                    status_label = "Merging (ffmpeg)"
                else:
                    video_codec_args = ["-c:v", "libx264", "-preset", "veryfast"]
                    status_label = "Encoding H.264 (FFmpeg)"

                cmd = [self.ffmpeg, "-y", "-i", video_tmp, "-i", audio_tmp,
                       "-map", "0:v:0", "-map", "1:a:0",
                       *video_codec_args, *audio_codec_args, final_path]

                self.manager.report_stage_progress(tid, "processing", status=f"Worker{worker_id}: {status_label}")
                returncode = self._run_ffmpeg(tid, worker_id, cmd, duration, status_label, check=False)

                if self.manager.is_cancelled(tid):
                    # Cancellation must not trigger the merge-failure re-encoding fallback.
                    raise DownloadCancelled()

                if returncode != 0:
                    if source_is_h264:
                        self.manager.update_task(tid, status=f"Worker{worker_id}: Re-encoding")
                        cmd2 = [self.ffmpeg, "-y", "-i", video_tmp, "-i", audio_tmp,
                                "-map", "0:v:0", "-map", "1:a:0",
                                "-c:v", "libx264", "-preset", "veryfast", *audio_codec_args, final_path]
                        self._run_ffmpeg(tid, worker_id, cmd2, duration, "Re-encoding")
                    else:
                        raise MediaOperationError("FFmpeg encoding failed")

                final_path = self._maybe_trim(tid, worker_id, final_path, trim_start, trim_end)
                self._ensure_h264_compatible(tid, worker_id, final_path)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                final_path = self._publish_output(tid, final_path, output_folder)
                output_paths.append(final_path)
                output_complete = True
                self.manager.update_task(tid, status="Completed (MP4/H.264)", progress=100, output_path=final_path)
                self.manager.notify(f"{raw_title} downloaded (MP4/H.264).")

            else:
                # Preserve the original codecs and choose a compatible MP4, WebM or MKV container.
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                progressive = next(iter(progressive_candidates), None)
                if progressive:
                    source_bytes = safe_filesize(progressive)
                    self._check_disk_space(folder, source_bytes,
                                           trim_bytes=source_bytes if trim_start or trim_end else 0)
                    ext = progressive.subtype or "mp4"
                    out_path = self._dedupe_path(
                        os.path.join(folder, f"{title_safe}_{tid}.{ext}"), tid)
                    output_paths.append(out_path)
                    self._download_format(tid, 'progressive', url, progressive, os.path.basename(out_path), folder=folder)
                    if self.manager.is_cancelled(tid):
                        raise DownloadCancelled()
                    out_path = self._maybe_trim(tid, worker_id, out_path, trim_start, trim_end)
                    if self.manager.is_cancelled(tid):
                        raise DownloadCancelled()
                    out_path = self._publish_output(tid, out_path, output_folder)
                    output_paths.append(out_path)
                    output_complete = True
                    self.manager.update_task(tid, status=f"Completed ({ext.upper()})", progress=100, output_path=out_path)
                    self.manager.notify(f"{raw_title} downloaded ({ext.upper()}).")
                    return

                video_stream = next(iter(video_candidates), None)
                audio_candidates = yt.audio_only_formats()
                if not video_stream or not audio_candidates:
                    # Fall back to yt-dlp selection when no suitable pair of streams is available.
                    self._complete_via_selector(tid, worker_id, url, title_safe, raw_title, "MP4",
                                                target_height, False, trim_start, trim_end, folder=folder,
                                                output_folder=output_folder, media_info=yt._media_info)
                    return

                # Choose the best audio now; select the container from the downloaded codecs later.
                audio_stream = audio_candidates[0]

                if not self.ffmpeg or not self.ffprobe:
                    raise MediaOperationError("ffmpeg/ffprobe were not found; video merging is unavailable")

                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()

                required = safe_filesize(video_stream) + safe_filesize(audio_stream)
                output_bytes = math.ceil(required * 1.1)
                self._check_disk_space(folder, required, output_bytes=output_bytes,
                                       trim_bytes=output_bytes if trim_start or trim_end else 0)

                v_ext = video_stream.subtype or "mp4"
                a_ext = audio_stream.subtype or "m4a"
                video_tmp, audio_tmp = self._download_streams_parallel(
                    tid, url, video_stream, audio_stream,
                    f"{title_safe}_video_{tid}.{v_ext}", f"{title_safe}_audio_{tid}.{a_ext}",
                    folder=folder
                )

                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()

                video_info = self._ffprobe_media(video_tmp)
                audio_info = self._ffprobe_media(audio_tmp)
                video_codecs = [s.get("codec_name") for s in video_info["streams"]
                                if s.get("codec_type") == "video"]
                audio_codecs = [s.get("codec_name") for s in audio_info["streams"]
                                if s.get("codec_type") == "audio"]
                if not video_codecs or not audio_codecs:
                    raise MediaOperationError("No video or audio stream was found for merging")
                out_ext = self._select_merge_container(video_codecs, audio_codecs)
                final_path = os.path.join(folder, f"{title_safe}.{out_ext}")
                final_path = self._dedupe_path(final_path, tid)
                output_paths.append(final_path)
                duration = self._ffprobe_duration(video_tmp, self.ffprobe)

                cmd = [self.ffmpeg, "-y", "-i", video_tmp, "-i", audio_tmp,
                       "-map", "0:v", "-map", "1:a",
                       "-c:v", "copy", "-c:a", "copy", final_path]
                status_label = "Merging (original codecs)"
                self.manager.report_stage_progress(tid, "processing", status=f"Worker{worker_id}: {status_label}")
                returncode = self._run_ffmpeg(tid, worker_id, cmd, duration, status_label, check=False)

                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()

                if returncode != 0:
                    if out_ext == "mkv":
                        raise MediaOperationError("MKV merging with the original codecs failed")
                    # If muxing fails despite compatible codec families, retry with MKV without re-encoding.
                    remove_leftovers(folder, lambda n: n == os.path.basename(final_path))
                    out_ext = "mkv"
                    final_path = self._dedupe_path(os.path.join(folder, f"{title_safe}.mkv"), tid)
                    output_paths.append(final_path)
                    cmd2 = [self.ffmpeg, "-y", "-i", video_tmp, "-i", audio_tmp,
                            "-map", "0:v", "-map", "1:a",
                            "-c:v", "copy", "-c:a", "copy", final_path]
                    self._run_ffmpeg(tid, worker_id, cmd2, duration, "Merging (MKV)")

                final_path = self._maybe_trim(tid, worker_id, final_path, trim_start, trim_end)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                final_path = self._publish_output(tid, final_path, output_folder)
                output_paths.append(final_path)
                output_complete = True
                self.manager.update_task(tid, status=f"Completed ({out_ext.upper()})", progress=100, output_path=final_path)
                self.manager.notify(f"{raw_title} downloaded ({out_ext.upper()}).")

        except DownloadCancelled:
            self.manager.update_task(tid, status="Cancelled", progress=0)
            self.manager.notify(f"{tid} cancelled.")
        except Exception as e:
            if self.manager.is_cancelled(tid):
                self.manager.update_task(tid, status="Cancelled", progress=0)
            else:
                self.manager.fail_task(tid, str(e))
        finally:
            # Clean incomplete outputs after FFmpeg exits; preserve successful and unreserved files.
            with log_context(stage='cleanup'):
                if not output_complete:
                    for path in output_paths:
                        remove_leftovers(os.path.dirname(path) or folder,
                                         part_matcher(os.path.basename(path)))
                for p in (video_tmp, audio_tmp):
                    if p:
                        remove_leftovers(os.path.dirname(p) or folder, part_matcher(os.path.basename(p)))
                if work_dir is not None:
                    self._cleanup_workspace(work_dir, output_folder)
            with self.resource_lock:
                self._subtitle_context.pop(tid, None)
                self._output_naming.pop(tid, None)
                for key, owner in list(self.reserved_output_paths.items()):
                    if owner == tid:
                        del self.reserved_output_paths[key]
