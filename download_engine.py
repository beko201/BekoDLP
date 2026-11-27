"""Download engine facade composed from explicit, independently constructed services."""
import os
import re
import math
import json
import subprocess
import time
import sys
import threading
import gc
import traceback
import urllib.request
from urllib.parse import parse_qs, urlsplit
import shutil
import tempfile
from copy import deepcopy
from contextvars import copy_context, ContextVar
from contextlib import contextmanager
from queue import Queue, Empty
from component_state import RuntimeField
from engine_runtime import EngineRuntime
from ytdlp_process import YtDlpProcess, YtDlpProcessError, require_executable, kill_process_tree
from media_probe import MediaProbeCache, probe_duration
from network_limits import NetworkLimits
from processing_limits import ProcessingLimits, video_encoder, budget_command
from errors import MediaOperationError
from cookie_options import cookie_selection, cookie_kwargs
from network_options import network_selection, network_kwargs
from naming_templates import NAMING_DEFAULTS, render_naming, ensure_naming_folder, playlist_naming_context
from errors import DownloadCancelled, InsufficientDiskSpace, PlaylistLinkNotAllowed
from download_support import (
    MAX_FETCH_RETRIES,
    DISK_HEADROOM_BYTES,
    _check_cancelled,
    _network_options,
)
from process_utils import NO_WINDOW, psutil, run_cmd_capture
from file_utils import safe_filename, safe_filesize, remove_leftovers, part_matcher
from url_utils import normalize_url
from time_utils import validate_trim_range
from runtime_tools import which_executable, env_info, _normalize_version
from app_logging import (
    log_error,
    log_warning,
    log_context,
    flush_warning_summaries,
    unexpected_traceback,
)
from engine_conversion import MediaConversion
from engine_metadata import (
    PLAYLIST_BATCH_SIZE,
    YTFormat,
    YTInfo,
    _MediaInfoCache,
    _entries_to_urls,
    _extract_urls_from_input,
    _flat_extract,
    _is_expired_media_error,
    _video_codec_rank,
    fetch_ytinfo_with_retry,
)
from engine_processes import MediaProcesses
from engine_publication import MediaPublication
from engine_tasks import DownloadTasks
from engine_transfers import MediaTransfers


class DownloadEngine:
    """Process downloads and report task state changes through the manager."""

    runtime: EngineRuntime

    # Existing callers retain these names; the runtime is their single owner.
    ffmpeg = RuntimeField()
    ffprobe = RuntimeField()
    current_process_pids = RuntimeField()
    managed_processes = RuntimeField()
    _force_stopping = RuntimeField()
    reserved_output_paths = RuntimeField()
    resource_lock = RuntimeField()
    _subtitle_context = RuntimeField()
    _output_naming = RuntimeField()
    _rate_divisor = RuntimeField()
    _encoder_lock = RuntimeField()
    _encoders = RuntimeField()
    processing_limits = RuntimeField()
    network_limits = RuntimeField()
    _probe_cache = RuntimeField()

    def __init__(self, manager, *, ffmpeg=None, ffprobe=None):
        require_executable()
        self.manager = manager
        self.runtime = EngineRuntime(
            ffmpeg=ffmpeg if ffmpeg is not None else which_executable(['ffmpeg', 'ffmpeg.exe']),
            ffprobe=ffprobe if ffprobe is not None else which_executable(['ffprobe', 'ffprobe.exe']))
        self.validate_tools()
        self.task_runner = DownloadTasks(self.runtime, manager, self)
        self.transfers = MediaTransfers(self.runtime, manager, self)
        self.conversion = MediaConversion(self.runtime, manager, self)
        self.publication = MediaPublication(self.runtime, manager, self)
        self.processes = MediaProcesses(self.runtime, manager, self)

    def configure_processing(self, video_workers, ffmpeg_threads):
        self.processing_limits.configure(video_workers, ffmpeg_threads)

    def configure_network(self, fragment_downloads, network_connections):
        self.network_limits.configure(fragment_downloads, network_connections)

    def validate_tools(self):
        missing = [name for name in ('ffmpeg', 'ffprobe') if not getattr(self, name)]
        if missing:
            raise MediaOperationError(
                f"FFmpeg is required. Missing tools: {', '.join(missing)}. "
                "Add the ffmpeg and ffprobe executables from the FFmpeg distribution "
                "to PATH.")

    def process_task(self, task_tuple, worker_id: int):
        tid = task_tuple[0]
        attempt = self.manager.begin_task_attempt(tid)
        if attempt is None:
            return
        with log_context(lambda: self.manager.task_log_fields(tid), task=tid, attempt=attempt), \
                self.media_probe_session(new_attempt=True):
            log_error('Task attempt started', level='DEBUG', event='task_attempt_started',
                      component='download_engine', details=[('worker', worker_id)])
            try:
                self._process_task_attempt(task_tuple, worker_id)
            finally:
                flush_warning_summaries(task=tid, attempt=attempt)

    def _process_task_attempt(self, task_tuple, worker_id: int):
        return self.task_runner._process_task_attempt(task_tuple, worker_id)

    def ytdlp_process(self, options, tid, *, stop_event=None, folder=None):
        return self.transfers.ytdlp_process(options, tid, stop_event=stop_event, folder=folder)

    def media_download_session(self, tid, *, streams=1, fragmented=True, stop_event=None):
        return self.transfers.media_download_session(tid, streams=streams, fragmented=fragmented, stop_event=stop_event)

    def _wait_for_download_resume(self, tid, *, stop_event=None):
        return self.transfers._wait_for_download_resume(tid, stop_event=stop_event)

    def _download_from_info(self, ydl, source_url, media_info, cancel_event, *, format_id=None, stop_event=None, tid=None):
        return self.transfers._download_from_info(ydl, source_url, media_info, cancel_event, format_id=format_id, stop_event=stop_event, tid=tid)

    def _download_format(self, tid, stream_key, source_url, yt_format, filename, *, folder, stop_event=None):
        return self.transfers._download_format(tid, stream_key, source_url, yt_format, filename, folder=folder, stop_event=stop_event)

    def _download_streams_parallel(self, tid, source_url, video_stream, audio_stream, video_filename, audio_filename, *, folder):
        return self.transfers._download_streams_parallel(tid, source_url, video_stream, audio_stream, video_filename, audio_filename, folder=folder)

    def _complete_via_selector(self, tid, worker_id, url, title_safe, raw_title, fmt, target_height, force_h264, trim_start, trim_end, *, folder, output_folder=None, media_info=None):
        return self.transfers._complete_via_selector(tid, worker_id, url, title_safe, raw_title, fmt, target_height, force_h264, trim_start, trim_end, folder=folder, output_folder=output_folder, media_info=media_info)

    def _complete_via_selector_in_workspace(self, tid, worker_id, url, title_safe, raw_title, fmt, target_height, force_h264, trim_start, trim_end, *, folder, output_folder, media_info=None):
        return self.transfers._complete_via_selector_in_workspace(tid, worker_id, url, title_safe, raw_title, fmt, target_height, force_h264, trim_start, trim_end, folder=folder, output_folder=output_folder, media_info=media_info)

    def media_probe_session(self, *, new_attempt=False):
        return self.conversion.media_probe_session(new_attempt=new_attempt)

    def _invalidate_media_probes(self, *paths):
        return self.conversion._invalidate_media_probes(*paths)

    def _replace_media(self, source, destination):
        return self.conversion._replace_media(source, destination)

    def available_encoders(self):
        return self.conversion.available_encoders()

    def _estimate_mp3_bytes(self, tid, source_bytes):
        return self.conversion._estimate_mp3_bytes(tid, source_bytes)

    def _check_disk_space(self, folder, required_bytes, *, output_bytes=0, trim_bytes=0):
        return self.conversion._check_disk_space(folder, required_bytes, output_bytes=output_bytes, trim_bytes=trim_bytes)

    def _check_download_disk_space(self, tid, folder, downloaded, total):
        return self.conversion._check_download_disk_space(tid, folder, downloaded, total)

    def _check_ffmpeg_disk_space(self, tid, cmd, duration=0):
        return self.conversion._check_ffmpeg_disk_space(tid, cmd, duration)

    def _maybe_trim(self, tid, worker_id, input_path, trim_start_text, trim_end_text):
        return self.conversion._maybe_trim(tid, worker_id, input_path, trim_start_text, trim_end_text)

    @staticmethod
    def _select_merge_container(video_codecs, audio_codecs):
        return MediaConversion._select_merge_container(video_codecs, audio_codecs)

    def _ffprobe_media(self, path, *, fresh=False):
        return self.conversion._ffprobe_media(path, fresh=fresh)

    def _probe_media(self, path, executable, *, fresh=False):
        return self.conversion._probe_media(path, executable, fresh=fresh)

    def _read_media_probe(self, path, executable):
        return self.conversion._read_media_probe(path, executable)

    def _ffprobe_copy_start(self, path, stream_index, requested_start):
        return self.conversion._ffprobe_copy_start(path, stream_index, requested_start)

    @staticmethod
    def _is_h264_compatible(info):
        return MediaConversion._is_h264_compatible(info)

    def _ensure_h264_compatible(self, tid, worker_id, path):
        return self.conversion._ensure_h264_compatible(tid, worker_id, path)

    def _ffprobe_vcodec(self, path):
        return self.conversion._ffprobe_vcodec(path)

    def _ffprobe_duration(self, path, ffprobe_cmd):
        return self.conversion._ffprobe_duration(path, ffprobe_cmd)

    def _ffmpeg_convert_to_mp3(self, tid, worker_id, in_path, out_path):
        return self.conversion._ffmpeg_convert_to_mp3(tid, worker_id, in_path, out_path)

    def _task_duration(self, tid):
        return self.conversion._task_duration(tid)

    def _cleanup_workspace(self, work_dir, parent_folder):
        return self.publication._cleanup_workspace(work_dir, parent_folder)

    def _release_output_reservation(self, path, tid):
        return self.publication._release_output_reservation(path, tid)

    def _record_subtitle_start(self, tid, start):
        return self.publication._record_subtitle_start(tid, start)

    def _publish_output(self, tid, source, output_folder, *, apply_naming=True):
        return self.publication._publish_output(tid, source, output_folder, apply_naming=apply_naming)

    def _publish_media_file(self, tid, source, output_folder, *, filename=None):
        return self.publication._publish_media_file(tid, source, output_folder, filename=filename)

    def _dedupe_path(self, path, tid):
        return self.publication._dedupe_path(path, tid)

    def _run_ffmpeg(self, tid, worker_id, cmd, duration=0, status_label='Processing', *, check=True, progress_stage='processing', stop_event=None):
        return self.processes._run_ffmpeg(tid, worker_id, cmd, duration, status_label, check=check, progress_stage=progress_stage, stop_event=stop_event)

    def _run_ffmpeg_process(self, tid, worker_id, cmd, duration=0, status_label='Processing', *, check=True, progress_stage='processing', stop_event=None):
        return self.processes._run_ffmpeg_process(tid, worker_id, cmd, duration, status_label, check=check, progress_stage=progress_stage, stop_event=stop_event)

    def _register_process(self, proc):
        return self.processes._register_process(proc)

    def _unregister_process(self, proc):
        return self.processes._unregister_process(proc)

    def terminate_managed_processes(self):
        return self.processes.terminate_managed_processes()

    def _pump_ffmpeg_progress(self, proc, tid, duration, status_label, *, disk_folder=None, progress_stage='processing', stop_event=None):
        return self.processes._pump_ffmpeg_progress(proc, tid, duration, status_label, disk_folder=disk_folder, progress_stage=progress_stage, stop_event=stop_event)
