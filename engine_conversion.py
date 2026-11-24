"""Media probing, disk budgeting and compatibility conversion operations."""
import os
import re
import math
import json
import time
import shutil
from contextlib import contextmanager
from media_probe import MediaProbeCache, probe_duration
from errors import MediaOperationError
from utils import (
    DownloadCancelled,
    InsufficientDiskSpace,
    DISK_HEADROOM_BYTES,
    validate_trim_range,
    run_cmd_capture,
    log_warning,
)


class MediaConversionMixin:
    @contextmanager
    def media_probe_session(self, *, new_attempt=False):
        """Share probes only within this task attempt, including nested output work."""
        if not new_attempt and self._probe_cache.get() is not None:
            yield
            return
        cache = MediaProbeCache()
        token = self._probe_cache.set(cache)
        try:
            yield
        finally:
            self._probe_cache.reset(token)
            cache.clear()

    def _invalidate_media_probes(self, *paths):
        cache = self._probe_cache.get()
        if cache is not None:
            cache.invalidate(*paths)

    def _replace_media(self, source, destination):
        self._invalidate_media_probes(source, destination)
        try:
            os.replace(source, destination)
        finally:
            self._invalidate_media_probes(source, destination)

    def available_encoders(self):
        with self._encoder_lock:
            if self._encoders is None:
                output = run_cmd_capture([self.ffmpeg, "-hide_banner", "-encoders"], timeout=10,
                                         on_start=self._register_process, on_finish=self._unregister_process)
                self._encoders = frozenset(line.split()[1] for line in output.splitlines()
                                          if re.match(r"^\s+[VA][A-Z.]{5}\s+\S+", line))
            return self._encoders

    def _estimate_mp3_bytes(self, tid, source_bytes):
        task = self.manager.get_task(tid) or {}
        duration = task.get("duration") or 0
        bitrate = task.get("mp3_bitrate") or "192k"
        try:
            match = re.fullmatch(r"([0-9]+)([kKmM]?)", str(bitrate))
            bps = int(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2].lower()]
            estimate = float(duration) * bps / 8 * 1.1
            if math.isfinite(estimate) and estimate > 0:
                return math.ceil(estimate)
        except (TypeError, ValueError, OverflowError) as error:
            log_warning("Could not estimate MP3 size; using a source-size estimate", error,
                        [("task", tid), ("bitrate", bitrate), ("duration", duration)],
                        once_key=("mp3_size_estimate", tid))
        return math.ceil(source_bytes * 2)

    def _check_disk_space(self, folder, required_bytes, *, output_bytes=0, trim_bytes=0):
        """Reserve source, output and trim space with a 5% margin and 64 MiB headroom."""
        required = max(0, required_bytes) + max(0, output_bytes) + max(0, trim_bytes)
        needed = math.ceil(required * 1.05) + DISK_HEADROOM_BYTES
        try:
            free = shutil.disk_usage(folder).free
        except OSError as error:
            raise MediaOperationError(f"Could not read disk space: {error}") from error
        if free < needed:
            raise InsufficientDiskSpace(
                f"Insufficient disk space: required ~{needed / (1024 * 1024):.0f}MB "
                f"(including temporary files and headroom), available ~{free / (1024 * 1024):.0f}MB")

    def _check_download_disk_space(self, tid, folder, downloaded, total):
        now = time.monotonic()
        if not self.manager.should_check_disk(tid, now):
            return
        # Even an unknown total must leave the minimum free-space reserve intact.
        self._check_disk_space(folder, max(0, total - downloaded))

    def _check_ffmpeg_disk_space(self, tid, cmd, duration=0):
        output = cmd[-1]
        if output.startswith("file:"):
            output = output[5:]
        if output == "-" or not os.path.isabs(output):
            return None
        folder = os.path.dirname(output)
        sources = [cmd[index + 1] for index, arg in enumerate(cmd[:-1]) if arg == "-i"]
        # yt-dlp may prefix local input paths with 'file:'.
        sources = [path[5:] if path.startswith("file:") else path for path in sources]
        source_bytes = sum(os.path.getsize(path) for path in sources if os.path.isfile(path))
        if "libmp3lame" in cmd or ("-vn" in cmd and output.lower().endswith(".mp3")):
            output_bytes = self._estimate_mp3_bytes(tid, source_bytes)
        else:
            output_bytes = math.ceil(source_bytes * (4 if "libx264" in cmd else 1.1))
        # Sources already occupy disk space; reserve only the additional output here.
        self._check_disk_space(folder, output_bytes)
        return folder

    def _maybe_trim(self, tid, worker_id, input_path, trim_start_text, trim_end_text):
        """Apply the optional trim range using FFmpeg stream copying.

        Keep the input unchanged when no range is supplied. Stream copying avoids
        re-encoding, but keyframe boundaries can affect trim accuracy.
        """
        start_s, end_s = validate_trim_range(trim_start_text, trim_end_text)
        if start_s is None and end_s is None:
            return input_path
        if not self.ffmpeg:
            return input_path

        # The original and trimmed copy coexist until trimming succeeds.
        self._check_disk_space(os.path.dirname(input_path) or ".", os.path.getsize(input_path))
        base, ext = os.path.splitext(input_path)
        trimmed_path = self._dedupe_path(f"{base}_trim{ext}", tid)
        source_duration = self._ffprobe_duration(input_path, self.ffprobe) or self._task_duration(tid)
        cmd = [self.ffmpeg, "-y"]
        if start_s is not None:
            cmd += ["-ss", str(start_s)]
        cmd += ["-i", input_path]
        if end_s is not None:
            duration = end_s - (start_s or 0)
            cmd += ["-t", str(duration)]
        else:
            duration = max(0, source_duration - (start_s or 0))
        cmd += ["-c", "copy", trimmed_path]

        self.manager.report_stage_progress(tid, "trimming", status=f"Worker{worker_id}: Trimming")
        try:
            if source_duration > 0 and (start_s or 0) >= source_duration:
                raise ValueError("The trim start must be earlier than the source duration.")
            self._run_ffmpeg(tid, worker_id, cmd, duration=duration, status_label="Trimming",
                             progress_stage="trimming")
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            media = self._ffprobe_media(trimmed_path, fresh=True)
            trimmed_duration = probe_duration(media)
            if (not any(s.get("codec_type") in ("audio", "video") for s in media["streams"])
                    or not math.isfinite(trimmed_duration) or trimmed_duration <= 0):
                raise MediaOperationError("The trimmed file does not contain a valid media stream with positive duration.")
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            self._replace_media(trimmed_path, input_path)
            if (self.manager.get_task(tid) or {}).get('subtitle_mode', 'none') != 'none':
                self._record_subtitle_start(tid, start_s or 0)
        except (DownloadCancelled, InsufficientDiskSpace):
            raise  # Propagate cancellation and disk exhaustion instead of reporting success.
        except Exception as e:
            # Keep the full original if trimming fails and report a warning.
            log_warning("Trimming failed; preserving the full file", e,
                        [("task", tid), ("file", input_path)])
            self.manager.add_warning(tid, "Trimming could not be applied; the full file was saved")
        finally:
            try:
                if os.path.exists(trimmed_path):
                    os.remove(trimmed_path)
            except Exception as error:
                log_warning("Could not remove the temporary trim file", error, [("task", tid)], once_key=("_maybe_trim", tid))
            finally:
                self._release_output_reservation(trimmed_path, tid)
        return input_path

    @staticmethod
    def _select_merge_container(video_codecs, audio_codecs):
        from media_plan import select_merge_container
        return select_merge_container(video_codecs, audio_codecs)

    def _ffprobe_media(self, path, *, fresh=False):
        """Inspect actual media contents for codec and container compatibility decisions."""
        return self._probe_media(path, self.ffprobe, fresh=fresh)

    def _probe_media(self, path, executable, *, fresh=False):
        cache = self._probe_cache.get()
        if cache is not None:
            return cache.read(path, executable, lambda: self._read_media_probe(path, executable), fresh=fresh)
        return self._read_media_probe(path, executable)

    def _read_media_probe(self, path, executable):
        out = run_cmd_capture([
            executable, "-v", "error", "-show_entries",
            "format=format_name,duration,start_time:format_tags=major_brand:stream=index,codec_type,codec_name,sample_rate,channels,sample_fmt,bits_per_sample,bits_per_raw_sample,width,height:stream_disposition=attached_pic:stream_tags=filename,mimetype:chapter=id,start_time,end_time:chapter_tags=title",
            "-of", "json", path,
        ], timeout=15, on_start=self._register_process, on_finish=self._unregister_process)
        info = json.loads(out)
        if (not isinstance(info, dict) or not isinstance(info.get("streams"), list) or not info["streams"]
                or not all(isinstance(stream, dict) for stream in info["streams"])
                or not isinstance(info.get("format"), dict) or not info["format"]):
            raise MediaOperationError("Media codec and container information could not be verified")
        return info

    def _ffprobe_copy_start(self, path, stream_index, requested_start):
        """Locate one preceding video key packet without decoding the full video."""
        probe = self._ffprobe_media(str(path))
        try:
            origin = float(probe['format'].get('start_time') or 0)
            position = origin + requested_start
            if not math.isfinite(position):
                raise ValueError('non-finite seek position')
            raw = run_cmd_capture([
                self.ffprobe, '-v', 'error', '-read_intervals', f'{position:.9f}%+#1',
                '-select_streams', str(stream_index), '-show_entries', 'packet=pts_time,flags',
                '-of', 'json', str(path),
            ], timeout=15, on_start=self._register_process, on_finish=self._unregister_process)
            packet = json.loads(raw)['packets'][0]
            start = float(packet['pts_time']) - origin
            if ('K' not in packet.get('flags', '') or not math.isfinite(start)
                    or start < -.001 or start > requested_start + .001):
                raise ValueError('no preceding key packet')
            return max(0.0, min(start, requested_start))
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise MediaOperationError('Could not establish a synchronized stream-copy trim boundary.') from error

    @staticmethod
    def _is_h264_compatible(info):
        streams = info.get("streams") or []
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        container = info.get("format") or {}
        # MOV and MP4 share a demuxer name; use major_brand to exclude QuickTime files.
        brand = (container.get("tags", {}).get("major_brand") or "").strip().lower()
        return ("mp4" in (container.get("format_name") or "").split(",")
                and brand != "qt"
                and bool(videos) and all(s.get("codec_name") == "h264" for s in videos)
                and all(s.get("codec_name") == "aac" for s in audios))

    def _ensure_h264_compatible(self, tid, worker_id, path):
        """Verify MP4/H.264/AAC compatibility and convert streams as needed."""
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()
        self.manager.report_stage_progress(tid, "verification", status="Verifying H.264 compatibility")
        info = self._ffprobe_media(path, fresh=True)
        if self._is_h264_compatible(info):
            self.manager.report_stage_progress(tid, "verification", 1)
            return
        videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
        audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
        if not videos:
            raise MediaOperationError("No video stream was found for H.264 compatibility")
        copy_video = videos[0].get("codec_name") == "h264"
        copy_audio = not audios or audios[0].get("codec_name") == "aac"
        base = os.path.splitext(path)[0]
        converted = self._dedupe_path(base + "_compatible.mp4", tid)
        try:
            cmd = [self.ffmpeg, "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
                   "-c:v", "copy" if copy_video else "libx264"]
            if not copy_video:
                cmd += ["-preset", "veryfast"]
            cmd += ["-c:a", "copy" if copy_audio else "aac"]
            if not copy_audio:
                cmd += ["-b:a", "192k"]
            cmd += ["-movflags", "+faststart", "-f", "mp4", converted]
            label = "Applying H.264 compatibility"
            self.manager.update_task(tid, status=f"Worker{worker_id}: {label}")
            duration = probe_duration(info)
            self._run_ffmpeg(tid, worker_id, cmd, duration, label, progress_stage="verification")
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if not self._is_h264_compatible(self._ffprobe_media(converted, fresh=True)):
                raise MediaOperationError("Output MP4/H.264/AAC compatibility could not be verified")
            self._replace_media(converted, path)
            self.manager.report_stage_progress(tid, "verification", 1)
        finally:
            try:
                if os.path.exists(converted):
                    os.remove(converted)
            finally:
                self._release_output_reservation(converted, tid)

    def _ffprobe_vcodec(self, path):
        """Return the first video stream's codec name, or an empty string if probing fails."""
        try:
            info = self._ffprobe_media(path)
            return next((str(stream.get("codec_name") or "").lower() for stream in info["streams"]
                         if stream.get("codec_type") == "video"), "")
        except DownloadCancelled:
            raise
        except Exception as error:
            log_warning("Could not retrieve video codec information", error, [("file", path)],
                        once_key=("ffprobe_codec", path))
            return ""

    def _ffprobe_duration(self, path, ffprobe_cmd):
        try:
            return probe_duration(self._probe_media(path, ffprobe_cmd))
        except DownloadCancelled:
            raise
        except Exception as error:
            log_warning("Could not retrieve video duration", error, [("file", path)],
                        once_key=("ffprobe_duration", path))
            return 0

    def _ffmpeg_convert_to_mp3(self, tid, worker_id, in_path, out_path):
        task = self.manager.get_task(tid) or {}
        bitrate = task.get("mp3_bitrate") or "192k"
        cmd = [self.ffmpeg, "-y", "-i", in_path, "-vn", "-ab", bitrate, "-ar", "44100", out_path]
        duration = self._task_duration(tid) or self._ffprobe_duration(in_path, self.ffprobe)
        self._run_ffmpeg(tid, worker_id, cmd, duration=duration, status_label="Converting to MP3")

    def _task_duration(self, tid):
        task = self.manager.get_task(tid) or {}
        try:
            duration = float(task.get("duration") or 0)
            return duration if math.isfinite(duration) and duration > 0 else 0
        except (ValueError, TypeError, OverflowError):
            return 0
