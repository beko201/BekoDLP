"""Public download engine facade; components share this engine instance."""
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
from ytdlp_process import YtDlpProcess, YtDlpProcessError, require_executable, kill_process_tree
from media_probe import MediaProbeCache, probe_duration
from network_limits import NetworkLimits
from processing_limits import ProcessingLimits, video_encoder, budget_command
from errors import MediaOperationError
from cookie_options import cookie_selection, cookie_kwargs
from network_options import network_selection, network_kwargs
from naming_templates import NAMING_DEFAULTS, render_naming, ensure_naming_folder, playlist_naming_context
from utils import (
    DownloadCancelled,
    InsufficientDiskSpace,
    PlaylistLinkNotAllowed,
    MAX_FETCH_RETRIES,
    DISK_HEADROOM_BYTES,
    NO_WINDOW,
    psutil,
    _check_cancelled,
    _network_options,
    safe_filename,
    safe_filesize,
    normalize_url,
    validate_trim_range,
    remove_leftovers,
    part_matcher,
    which_executable,
    run_cmd_capture,
    log_error,
    log_warning,
    log_context,
    flush_warning_summaries,
    unexpected_traceback,
    env_info,
    _normalize_version,
)
from engine_conversion import MediaConversionMixin
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
from engine_processes import MediaProcessMixin
from engine_publication import MediaPublicationMixin
from engine_tasks import TaskDownloadMixin
from engine_transfers import MediaTransferMixin


class DownloadEngine(TaskDownloadMixin, MediaTransferMixin, MediaConversionMixin, MediaPublicationMixin, MediaProcessMixin):
    """Process downloads and report task state changes through the manager."""

    def __init__(self, manager, *, ffmpeg=None, ffprobe=None):
        require_executable()
        self.manager = manager
        self.ffmpeg = ffmpeg if ffmpeg is not None else which_executable(['ffmpeg', 'ffmpeg.exe'])
        self.ffprobe = ffprobe if ffprobe is not None else which_executable(['ffprobe', 'ffprobe.exe'])
        self.validate_tools()
        self.current_process_pids = {}
        self.managed_processes = set()
        self._force_stopping = False
        self.reserved_output_paths = {}
        self.resource_lock = threading.Lock()
        self._subtitle_context = {}
        self._output_naming = {}
        self._rate_divisor = ContextVar(f"download_rate_divisor_{id(self)}", default=1)
        self._encoder_lock = threading.Lock()
        self._encoders = None
        self.processing_limits = ProcessingLimits()
        self.network_limits = NetworkLimits()
        self._probe_cache = ContextVar(f"media_probes_{id(self)}", default=None)

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
