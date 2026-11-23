"""Download, format selection and media processing independent of the GUI toolkit."""
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
    DownloadCancelled, InsufficientDiskSpace, PlaylistLinkNotAllowed,
    MAX_FETCH_RETRIES, DISK_HEADROOM_BYTES, NO_WINDOW, psutil,
    _check_cancelled, _network_options, safe_filename, safe_filesize, normalize_url,
    validate_trim_range, remove_leftovers, part_matcher, which_executable,
    run_cmd_capture, log_error, log_warning, log_context, flush_warning_summaries,
    unexpected_traceback, env_info, _normalize_version,
)


PLAYLIST_BATCH_SIZE = 100


def _flat_extract(url: str, noplaylist: bool = False, cancel_event=None, *, on_entries=None, cookie_options=None, network_options=None):
    options = {'noplaylist': noplaylist, **_network_options(cancel_event)}
    options.update(cookie_selection(cookie_options))
    options.update(network_selection(network_options))
    with YtDlpProcess(options) as ydl:
        return ydl.flat_extract(url, on_entries=on_entries, batch_size=PLAYLIST_BATCH_SIZE)


def _entries_to_urls(entries):
    video_urls = []
    for e in entries or []:
        if not e:
            continue
        candidate = e.get('webpage_url') or e.get('url') or e.get('id')
        # CLI JSON enriches embedded HTML5 entries with their common page URL.
        # Queue each media item instead of enqueueing that collection repeatedly.
        if e.get('url') and e.get('webpage_url') == e.get('playlist_webpage_url'):
            candidate = e['url']
        if not candidate:
            continue
        if not candidate.startswith('http'):
            # Some extractors, including YouTube, return only a video ID.
            ie_key = (e.get('ie_key') or '').lower()
            if 'youtube' in ie_key or not ie_key:
                candidate = f"https://www.youtube.com/watch?v={candidate}"
            else:
                continue
        video_urls.append(normalize_url(candidate))
    return video_urls


def _extract_urls_from_input(url: str, expand_playlist: bool = True, notes=None, cancel_event=None,
                             resolved_info=None, on_batch=None, cookie_options=None, network_options=None, entry_context=None):
    """Resolve video URLs from any platform supported by yt-dlp.

    With expand_playlist=True, expand playlists, albums and threads. For a
    YouTube watch URL with a playlist, put the selected video first. If expansion
    fails, fall back to the available video and append an explanation to notes.

    With expand_playlist=False, accept only a single video. A YouTube watch URL
    with a playlist resolves only its explicit video; collection-only URLs raise
    PlaylistLinkNotAllowed.

    This function performs network requests and must run outside the GUI thread.
    With on_batch, collections are delivered incrementally and return an empty
    list; a standalone video can still be returned for the caller to enqueue.
    """
    url = url.strip()
    _check_cancelled(cancel_event)
    normalized = normalize_url(url)
    single_video_url = (normalized if re.fullmatch(
        r'https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]+', normalized) else None)
    list_id = None
    youtube_playlist = False
    try:
        parsed = urlsplit(url if '://' in url or url.startswith('//') else '//' + url)
        if (parsed.scheme in ('', 'http', 'https') and parsed.username is None
                and parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}):
            list_id = (parse_qs(parsed.query).get('list') or [None])[0]
            if list_id and not re.fullmatch(r'[A-Za-z0-9_-]+', list_id):
                list_id = None
            youtube_playlist = parsed.path.rstrip('/') == '/playlist'
    except ValueError:
        pass  # The extractor will report malformed URLs.
    if not expand_playlist and youtube_playlist:
        raise PlaylistLinkNotAllowed()

    buffered, seen = [], set()
    streamed = 0
    entry_position = 0

    def capture_context(entries, title=''):
        nonlocal entry_position
        if entry_context is None:
            return
        for entry in entries or []:
            entry_position += 1
            for candidate in _entries_to_urls([entry]):
                entry_context.setdefault(candidate, playlist_naming_context(entry, entry_position, title))

    def consume_entries(entries):
        nonlocal streamed, buffered
        capture_context(entries)
        urls = _entries_to_urls(entries)
        if urls and not seen and single_video_url:
            urls.insert(0, single_video_url)
        for candidate in urls:
            _check_cancelled(cancel_event)
            if candidate in seen:
                continue
            seen.add(candidate)
            streamed += 1
            buffered.append(candidate)
            if len(buffered) >= PLAYLIST_BATCH_SIZE:
                on_batch(buffered)
                buffered = []

    def finish_stream(error=None):
        if buffered:
            _check_cancelled(cancel_event)
            on_batch(buffered)
        if error is not None and notes is not None:
            notes.append("The playlist could not be fully read; available videos were queued.")
        return []

    stream_options = {'on_entries': consume_entries} if expand_playlist and on_batch is not None else {}

    error = None
    info, entries = None, None
    try:
        extract_url = single_video_url if not expand_playlist and single_video_url else url
        info, entries = _flat_extract(extract_url, noplaylist=not expand_playlist,
                                     cancel_event=cancel_event, **stream_options, **cookie_kwargs(cookie_options), **network_kwargs(network_options))
    except DownloadCancelled:
        raise
    except Exception as e:
        error = e
        context = "Link preflight resolution failed"
        if streamed:
            context = "Playlist resolution stopped; available entries will be kept"
        elif expand_playlist or single_video_url:
            context += "; a fallback will be attempted"
        log_warning(context, e,
                    [("link", url)], once_key=("flat_extract", url))

    if (resolved_info is not None and info and info.get('_type', 'video') == 'video'
            and info.get('id') and (info.get('formats') or info.get('url'))):
        resolved_info[normalize_url(info.get('webpage_url') or url)] = info
    if stream_options and not streamed and entries is not None:
        consume_entries(entries)
    if streamed:
        return finish_stream(error)
    if (stream_options and info is not None and not single_video_url
            and (info.get('_type') in ('playlist', 'multi_video') or info.get('entries') is not None)):
        return []
    video_urls = _entries_to_urls(entries)
    if not stream_options:
        capture_context(entries, (info or {}).get('title', ''))

    if not expand_playlist:
        if error is not None:
            if single_video_url:
                return [single_video_url]
            # Without metadata, an arbitrary URL might be an album/playlist.
            # Do not hand an unverified collection to a single-video worker.
            raise error
        collection = info is not None and (
            info.get('_type') in ('playlist', 'multi_video')
            or info.get('entries') is not None or entries is not None)
        if collection:
            if single_video_url:
                return [single_video_url]
            raise PlaylistLinkNotAllowed()
        if info is None:
            raise MediaOperationError("The URL could not be verified as a single video.")
        return [normalize_url(info.get('webpage_url') or url)]

    if len(video_urls) <= 1 and list_id and list_id.startswith(('PL', 'OLAK', 'UU', 'FL', 'LL')):
        # Retry with a playlist-only URL if the watch URL did not expand the list.
        try:
            alternative_info, entries2 = _flat_extract(f"https://www.youtube.com/playlist?list={list_id}",
                                        cancel_event=cancel_event, **stream_options, **cookie_kwargs(cookie_options), **network_kwargs(network_options))
            if stream_options and not streamed and entries2 is not None:
                consume_entries(entries2)
            alt = _entries_to_urls(entries2)
            if len(alt) > len(video_urls):
                video_urls, error = alt, None
                if entry_context is not None and not stream_options:
                    entry_position = 0
                    entry_context.clear()
                    capture_context(entries2, (alternative_info or {}).get('title', ''))
        except DownloadCancelled:
            raise
        except Exception as e2:
            error = error or e2
            log_warning("Fallback playlist resolution failed", e2, [("link", url)],
                        once_key=("playlist_fallback", url))
        if streamed:
            return finish_stream(error)

    if len(video_urls) > 1:
        # Put the explicitly selected YouTube video first.
        if single_video_url:
            if single_video_url in video_urls:
                video_urls.remove(single_video_url)
            video_urls.insert(0, single_video_url)
        return video_urls

    # Fall back to the available video if the playlist could not be expanded.
    if list_id:
        reason = ""
        if error is not None:
            reason = " ".join(str(error).replace("ERROR:", "").split())[:160]
            log_error("WARNING playlist could not be expanded", details=[("error", error), ("link", url),
                                                           ("version", env_info())],
                      level='WARNING', event='playlist_expansion_failed', component='download_engine')
        if notes is not None:
            notes.append("The playlist entries could not be retrieved; only this video was added."
                         + (f"\n\nReason: {reason}" if reason else
                            "\n\nThe URL may contain a single video, or the playlist may be empty or inaccessible."))
    if video_urls:
        return video_urls
    if info is not None and not entries:
        return [single_video_url or normalize_url(info.get('webpage_url') or url)]
    return [normalize_url(url)]


def _video_codec_rank(vcodec: str) -> int:
    """Rank video codecs for automatic selection: AV1, then VP9, H.264 and others."""
    if not vcodec:
        return 0
    if vcodec.startswith('av01'):
        return 3
    if vcodec.startswith('vp09') or vcodec.startswith('vp9'):
        return 2
    if vcodec.startswith('avc1'):
        return 1
    return 0


class _MediaInfoCache:
    """Cache metadata for one task attempt and share refreshes between parallel streams."""
    def __init__(self, url, info, cookie_options=None, network_options=None):
        self.url, self.info = url, info
        self.cookie_options = cookie_selection(cookie_options)
        self.network_options = network_selection(network_options)
        self.generation = 0
        self.lock = threading.RLock()

    def snapshot(self, format_id=None):
        with self.lock:
            info = self.info
            if info.get('_type', 'video') != 'video' or not info.get('id'):
                return None, self.generation
            formats = info.get('formats') or []
            if format_id is not None:
                formats = [fmt for fmt in formats if fmt.get('format_id') == format_id]
                if not formats:
                    return None, self.generation
            if not any(fmt.get('url') for fmt in formats) and not (format_id is None and info.get('url')):
                return None, self.generation
            # yt-dlp mutates metadata and records output paths while processing.
            # Never share those mutations between threads or carry old selections.
            result = {key: value for key, value in info.items() if key not in {
                'formats', 'requested_formats', 'requested_downloads', 'filepath', '_filename',
                '__files_to_merge', '__files_to_move', '__postprocessors', 'additional_urls',
            }}
            if formats:
                result['formats'] = formats
            return deepcopy(result), self.generation

    def refresh(self, generation, cancel_event, stop_event):
        with self.lock:
            _check_cancelled(cancel_event)
            _check_cancelled(stop_event)
            if self.generation == generation:
                refreshed = fetch_ytinfo_with_retry(
                    self.url, max_retries=1, cancel_event=cancel_event, stop_event=stop_event,
                    **cookie_kwargs(self.cookie_options), **network_kwargs(self.network_options))
                self.info = refreshed._media_info.info
                self.generation += 1


def _is_expired_media_error(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (YtDlpProcessError, urllib.error.HTTPError)):
            if getattr(current, 'status', getattr(current, 'code', None)) in (401, 403, 410):
                return True
        pending.extend((getattr(current, 'cause', None), getattr(current, '__cause__', None),
                        getattr(current, '__context__', None)))
        exc_info = getattr(current, 'exc_info', None)
        if exc_info:
            pending.append(exc_info[1])
    return False


class YTFormat:
    """Expose yt-dlp format metadata through the stream attributes used by the engine."""
    def __init__(self, fmt: dict, duration=0, media_info=None):
        self._media_info = media_info
        self.format_id = fmt.get('format_id')
        self.protocol = fmt.get('protocol') or ''
        self.ext = fmt.get('ext') or 'mp4'
        self.subtype = self.ext
        self.height = fmt.get('height')
        vcodec = fmt.get('vcodec') or 'none'
        acodec = fmt.get('acodec') or 'none'
        self.video_codec = vcodec if vcodec != 'none' else None
        self.audio_codec = acodec if acodec != 'none' else None
        self.codec_rank = _video_codec_rank(self.video_codec)
        self.abr = fmt.get('abr') or 0
        self._filesize = fmt.get('filesize') or fmt.get('filesize_approx') or 0
        # Estimate missing sizes from duration and bitrate in kbit/s.
        if not self._filesize:
            bitrate = fmt.get('tbr') or ((fmt.get('vbr') or 0) + (fmt.get('abr') or 0))
            try:
                estimate = float(duration or 0) * float(bitrate or 0) * 1000 / 8
                self._filesize = math.ceil(estimate) if math.isfinite(estimate) and estimate > 0 else 0
            except (ValueError, TypeError, OverflowError) as error:
                log_warning("Could not estimate the format size", error, [("format", self.format_id)],
                            once_key=("format_size_estimate", self.format_id))
                self._filesize = 0
        self.is_progressive = (vcodec != 'none' and acodec != 'none')
        self.only_video = (vcodec != 'none' and acodec == 'none')
        self.only_audio = (vcodec == 'none' and acodec != 'none')

    @property
    def filesize(self):
        return self._filesize


class YTInfo:
    """Expose yt-dlp video metadata and queries for progressive, video and audio formats."""
    def __init__(self, url: str, info: dict, cookie_options=None, network_options=None):
        self._media_info = _MediaInfoCache(url, info, cookie_options, network_options)
        self.url = url
        self.title = info.get('title') or 'Unnamed'
        self.thumbnail_url = info.get('thumbnail')
        self.duration = info.get('duration') or 0
        self.formats = [YTFormat(f, self.duration, self._media_info) for f in (info.get('formats') or [])]

    def progressive_formats(self, height=None, *, max_height=None):
        res = [f for f in self.formats if f.is_progressive]
        if height:
            res = [f for f in res if f.height == height]
        if max_height is not None:
            res = [f for f in res if f.height is not None and 0 < f.height <= max_height]
        # Prefer higher resolution, then AV1, VP9 and H.264 at the same height.
        res.sort(key=lambda f: (f.height or 0, f.codec_rank), reverse=True)
        return res

    def video_only_formats(self, height=None, *, max_height=None):
        res = [f for f in self.formats if f.only_video]
        if height:
            res = [f for f in res if f.height == height]
        if max_height is not None:
            res = [f for f in res if f.height is not None and 0 < f.height <= max_height]
        # Prefer higher resolution, then AV1, VP9 and H.264 at the same height.
        res.sort(key=lambda f: (f.height or 0, f.codec_rank), reverse=True)
        return res

    def audio_only_formats(self):
        res = [f for f in self.formats if f.only_audio]
        res.sort(key=lambda f: (f.abr or 0), reverse=True)
        return res


def fetch_ytinfo_with_retry(url, max_retries=MAX_FETCH_RETRIES, cancel_event=None, *, stop_event=None, cookie_options=None, network_options=None):
    """Fetch video metadata with brief backoff retries for transient failures."""
    last_exc = None
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'noplaylist': True,
    }
    ydl_opts.update(_network_options(cancel_event, stop_event=stop_event))
    ydl_opts.update(cookie_selection(cookie_options))
    ydl_opts.update(network_selection(network_options))
    for attempt in range(1, max_retries + 1):
        _check_cancelled(cancel_event)
        _check_cancelled(stop_event)
        try:
            with YtDlpProcess(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            _check_cancelled(cancel_event)
            _check_cancelled(stop_event)
            return YTInfo(url, info, cookie_options, network_options)
        except DownloadCancelled:
            raise
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                log_warning("Could not retrieve video metadata; retrying", e,
                            [("link", url), ("metadata attempt", f"{attempt}/{max_retries}")])
                if cancel_event is not None:
                    if cancel_event.wait(1.5 * attempt):
                        raise DownloadCancelled()
                else:
                    time.sleep(1.5 * attempt)
    _check_cancelled(cancel_event)
    raise last_exc




class DownloadEngine:
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

    def configure_processing(self, video_workers, ffmpeg_threads):
        self.processing_limits.configure(video_workers, ffmpeg_threads)

    def configure_network(self, fragment_downloads, network_connections):
        self.network_limits.configure(fragment_downloads, network_connections)

    def ytdlp_process(self, options, tid, *, stop_event=None, folder=None):
        paused_status = None
        def check():
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

        def tick():
            nonlocal paused_status
            task = self.manager.get_task(tid) or {}
            if self.manager.paused and paused_status is None:
                paused_status = task.get('status', 'Downloading')
                self.manager.update_task(tid, status='Paused')
            elif not self.manager.paused and paused_status is not None:
                if task.get('status') == 'Paused':
                    self.manager.update_task(tid, status=paused_status)
                paused_status = None
            if folder:
                self._check_disk_space(folder, 0)

        options = dict(options)
        task = self.manager.get_task(tid)
        options.update(cookie_selection(task))
        options.update(network_selection(task))
        if options['download_rate']:
            options['download_rate'] = str(max(1, int(options['download_rate']) // self._rate_divisor.get()))
            options['concurrent_fragment_downloads'] = 1
        options['ffmpeg_threads'] = self.processing_limits.thread_budget()
        if self.ffmpeg:
            options.setdefault('ffmpeg_location', self.ffmpeg)
        return YtDlpProcess(options, check=check, is_paused=lambda: self.manager.paused,
                            on_start=self._register_process, on_finish=self._unregister_process,
                            on_tick=tick)

    def media_download_session(self, tid, *, streams=1, fragmented=True, stop_event=None):
        if (self.manager.get_task(tid) or {}).get('download_rate'):
            fragmented = False
        return self.network_limits.session(
            lambda: self._wait_for_download_resume(tid, stop_event=stop_event),
            lambda: self.manager.update_task(tid, status="Waiting for download connection slot"),
            streams=streams, fragmented=fragmented)

    def available_encoders(self):
        with self._encoder_lock:
            if self._encoders is None:
                output = run_cmd_capture([self.ffmpeg, "-hide_banner", "-encoders"], timeout=10,
                                         on_start=self._register_process, on_finish=self._unregister_process)
                self._encoders = frozenset(line.split()[1] for line in output.splitlines()
                                          if re.match(r"^\s+[VA][A-Z.]{5}\s+\S+", line))
            return self._encoders

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
                  fetch_ytinfo_with_retry(url, cancel_event=self.manager.task_control(tid)[0], **cookie_kwargs(task), **network_kwargs(task)))
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

    def _wait_for_download_resume(self, tid, *, stop_event=None):
        """Wait while paused before network work, continuing to check cancellation signals."""
        notified = False
        while True:
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if not self.manager.paused:
                return
            if not notified:
                self.manager.update_task(tid, status="Paused")
                notified = True
            time.sleep(0.1)

    def _download_from_info(self, ydl, source_url, media_info, cancel_event, *,
                            format_id=None, stop_event=None, tid=None):
        _check_cancelled(cancel_event)
        _check_cancelled(stop_event)
        if tid is not None:
            self._wait_for_download_resume(tid, stop_event=stop_event)
        info, generation = media_info.snapshot(format_id) if media_info else (None, None)
        if info is None:
            if tid is not None and self.manager.get_task(tid).get('source_selection', 'auto') == 'manual':
                raise MediaOperationError('The selected source format is unavailable. Inspect the source formats again.')
            if format_id is None:
                return ydl.extract_info(source_url, download=True)
            return ydl.download([source_url])
        try:
            return ydl.process_ie_result(info, download=True)
        except Exception as error:
            _check_cancelled(cancel_event)
            _check_cancelled(stop_event)
            if not _is_expired_media_error(error):
                raise
            # Refresh once, shared by both streams. Preserve the original format
            # selection; an expired URL must never lift the resolution limit.
            if tid is not None:
                self._wait_for_download_resume(tid, stop_event=stop_event)
            media_info.refresh(generation, cancel_event, stop_event)
            refreshed, _ = media_info.snapshot(format_id)
            if refreshed is None:
                raise MediaOperationError("The selected format was not found in refreshed video metadata") from error
            _check_cancelled(cancel_event)
            _check_cancelled(stop_event)
            if tid is not None:
                self._wait_for_download_resume(tid, stop_event=stop_event)
            return ydl.process_ie_result(refreshed, download=True)

    def _download_format(self, tid, stream_key, source_url, yt_format, filename, *, folder, stop_event=None):
        """Download the selected format to the requested path.

        Use progress hooks to report download progress and check pause and
        cancellation signals.
        """
        self.manager.set_log_stage(tid, 'download')
        target_path = os.path.join(folder, filename)
        self.manager.register_download_streams(tid, {stream_key: safe_filesize(yt_format)})

        def check_cancelled():
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

        def hook(d):
            check_cancelled()
            if d.get('status') in ('downloading', 'finished'):
                finished = d.get('status') == 'finished'
                downloaded = d.get('downloaded_bytes') or 0
                total = downloaded if finished else d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                self._check_download_disk_space(tid, folder, downloaded, total)
                self.manager.report_progress(tid, stream_key, downloaded, total, force=finished)

        ydl_opts = {
            'quiet': True,
            'format': yt_format.format_id,
            'outtmpl': target_path,
            'progress_hooks': [hook],
            'noplaylist': True,
            'retries': 5,
            'fragment_retries': 5,
            'noprogress': True,
        }
        if self.manager.get_task(tid).get('source_selection', 'auto') == 'manual':
            # "all" keeps formats with unknown codecs too; the exact-ID filter
            # and the one-format metadata snapshot still select a single source.
            ydl_opts['format'] = f'all[format_id="{yt_format.format_id}"]'
        failed = None
        try:
            cancel_event, worker_id = self.manager.task_control(tid)
            if worker_id is None:
                raise RuntimeError("No worker was assigned to the download")
            _check_cancelled(cancel_event)
            self._wait_for_download_resume(tid, stop_event=stop_event)
            ydl_opts.update(_network_options(cancel_event, stop_event=stop_event))
            protocol = getattr(yt_format, 'protocol', '')
            fragmented = not protocol or any(kind in protocol for kind in ('m3u8', 'dash_segments', 'ism'))
            with self.media_download_session(tid, fragmented=fragmented, stop_event=stop_event) as network:
                ydl_opts.update(network)
                with self.ytdlp_process(ydl_opts, tid, stop_event=stop_event, folder=folder) as ydl:
                    self._download_from_info(ydl, source_url, getattr(yt_format, '_media_info', None),
                                             cancel_event, format_id=yt_format.format_id, stop_event=stop_event, tid=tid)
            check_cancelled()
        except BaseException as e:
            # Traceback frames can retain open yt-dlp files and block deletion on Windows.
            # Release those references here, then clean up after leaving the exception handler.
            try:
                traceback.clear_frames(e.__traceback__)
            except Exception as error:
                log_warning("Could not clear download traceback frames", error, [("task", tid)], once_key=("_download_format", tid))
            failed = e
        if failed is not None:
            # Parallel downloads are cleaned together after both threads exit.
            # Do not delay signalling the peer while retrying locked files here.
            if stop_event is None:
                remove_leftovers(folder, part_matcher(filename))
            raise failed
        size = os.path.getsize(target_path)
        self.manager.report_progress(tid, stream_key, size, size, force=True)
        return target_path

    def _download_streams_parallel(self, tid, source_url, video_stream, audio_stream, video_filename, audio_filename,
                                   *, folder):
        """Download video and audio concurrently, stopping the peer if either stream fails."""
        results = {}
        errors = []
        state_lock = threading.Lock()
        stop_event = threading.Event()
        self.manager.register_download_streams(tid, {
            "video": safe_filesize(video_stream), "audio": safe_filesize(audio_stream),
        })
        # Precompute both paths so cleanup includes downloads that fail before returning.
        expected_paths = {
            "video": os.path.join(folder, video_filename),
            "audio": os.path.join(folder, audio_filename),
        }

        def _dl(key, stream, filename):
            token = self._rate_divisor.set(2)
            try:
                path = self._download_format(tid, key, source_url, stream, filename,
                                             folder=folder, stop_event=stop_event)
                with state_lock:
                    results[key] = path
            except Exception as e:
                with state_lock:
                    # Store the original failure before the peer can report
                    # DownloadCancelled in response to this signal.
                    errors.append(e)
                    stop_event.set()

            finally:
                self._rate_divisor.reset(token)

        t_video = threading.Thread(target=copy_context().run,
                                   args=(_dl, "video", video_stream, video_filename), daemon=True)
        t_audio = threading.Thread(target=copy_context().run,
                                   args=(_dl, "audio", audio_stream, audio_filename), daemon=True)
        started = []
        try:
            for thread in (t_video, t_audio):
                thread.start()
                started.append(thread)
        except BaseException as error:
            with state_lock:
                # Record the launch failure before the running peer can
                # respond with DownloadCancelled and hide the original cause.
                errors.append(error)
                stop_event.set()
        finally:
            # Never clean up a workspace while a successfully started stream
            # still owns files in it. An unstarted thread cannot be joined.
            for thread in started:
                thread.join()

        if errors:
            # Include partial files from failed streams, even if no path was returned.
            for p in expected_paths.values():
                remove_leftovers(folder, part_matcher(os.path.basename(p)))
            raise errors[0]

        return results["video"], results["audio"]

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

    def _complete_via_selector(self, tid, worker_id, url, title_safe, raw_title, fmt,
                               target_height, force_h264, trim_start, trim_end, *, folder,
                               output_folder=None, media_info=None):
        validate_trim_range(trim_start, trim_end)
        output_folder = folder if output_folder is None else output_folder
        work_dir = tempfile.mkdtemp(prefix=f".yd-{tid}-", dir=folder)
        try:
            return self._complete_via_selector_in_workspace(
                tid, worker_id, url, title_safe, raw_title, fmt, target_height, force_h264,
                trim_start, trim_end, folder=work_dir, output_folder=output_folder, media_info=media_info)
        finally:
            self._cleanup_workspace(work_dir, folder)

    def _complete_via_selector_in_workspace(self, tid, worker_id, url, title_safe, raw_title, fmt,
                                            target_height, force_h264, trim_start, trim_end, *, folder,
                                            output_folder, media_info=None):
        """Use yt-dlp selection when native format classification finds no suitable streams.

        Apply the requested MP3 conversion or H.264 compatibility checks and
        optional trimming. Clean incomplete outputs on failure or cancellation.
        """
        self.manager.set_log_stage(tid, 'download')
        validate_trim_range(trim_start, trim_end)
        if not self.ffmpeg or not self.ffprobe:
            raise MediaOperationError("ffmpeg/ffprobe were not found; processing is unavailable")

        self._check_disk_space(folder, 0)
        audio_only = (fmt == "MP3")
        prefix = f"{title_safe}_{tid}"
        outtmpl = os.path.join(folder, prefix + ".%(ext)s")

        if audio_only:
            fmt_sel = "ba/b"
        else:
            h = f"[height<={target_height}]" if target_height else ""
            if force_h264:
                fmt_sel = f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/bv*{h}+ba/b{h}"
            else:
                fmt_sel = f"bv*{h}+ba/b{h}"

        def hook(d):
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if d.get('status') in ('downloading', 'finished'):
                finished = d.get('status') == 'finished'
                key = (d.get('info_dict') or {}).get('format_id') or d.get('filename') or 'sel'
                downloaded = d.get('downloaded_bytes') or 0
                total = downloaded if finished else d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                self._check_download_disk_space(tid, folder, downloaded, total)
                self.manager.report_progress(tid, key, downloaded, total, force=finished)

        def pp_hook(d):
            if d.get('status') == 'started':
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                self.manager.report_stage_progress(
                    tid, "processing", status=f"Worker{worker_id}: Processing (merging)")

        ydl_opts = {
            'quiet': True,
            'format': fmt_sel,
            'outtmpl': outtmpl,
            'progress_hooks': [hook],
            'postprocessor_hooks': [pp_hook],
            'noplaylist': True,
            'retries': 5,
            'fragment_retries': 5,
            'noprogress': True,
            'merge_output_format': 'mp4/mkv',
        }
        if self.ffmpeg and os.path.isabs(self.ffmpeg):
            ydl_opts['ffmpeg_location'] = self.ffmpeg
        cancel_event = self.manager.task_control(tid)[0]
        _check_cancelled(cancel_event)
        ydl_opts.update(_network_options(cancel_event))

        cleanup = []  # Remove these generated outputs on failure or cancellation.

        def purge():
            remove_leftovers(folder, lambda n: n.startswith(prefix + "."))
            for pth in cleanup:
                if pth:
                    remove_leftovers(os.path.dirname(pth) or folder,
                                     lambda n, b=os.path.basename(pth): n == b)

        failed = None
        try:
            self._wait_for_download_resume(tid)
            with self.media_download_session(tid, streams=1 if audio_only else 2) as network:
                ydl_opts.update(network)
                with self.ytdlp_process(ydl_opts, tid, folder=folder) as ydl:
                    info = self._download_from_info(ydl, url, media_info, cancel_event, tid=tid)
            if (info or {}).get("duration"):
                self.manager.update_metadata(tid, duration=info["duration"])

            src = None
            rd = (info or {}).get('requested_downloads') or []
            if rd:
                src = rd[0].get('filepath') or rd[0].get('_filename')
            if not src or not os.path.exists(src):
                # Find the final output by prefix, excluding intermediate format files.
                for f in os.listdir(folder):
                    if f.startswith(prefix + ".") and "." not in f[len(prefix) + 1:] \
                            and not f.endswith((".part", ".ytdl", ".temp")):
                        src = os.path.join(folder, f)
                        break
            if not src or not os.path.exists(src):
                raise MediaOperationError("The downloaded file was not found")
            if os.path.dirname(os.path.realpath(src)) != os.path.realpath(folder):
                raise RuntimeError("The downloaded file is outside the task workspace")
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

            if audio_only:
                out = self._dedupe_path(os.path.join(folder, f"{title_safe}.mp3"), tid)
                cleanup.append(out)
                self.manager.update_task(tid, status=f"Worker{worker_id}: Converting")
                self._ffmpeg_convert_to_mp3(tid, worker_id, src, out)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                try:
                    os.remove(src)
                except Exception as error:
                    log_warning("Could not remove the temporary source", error, [("task", tid), ("file", src)], once_key=("source_cleanup", tid))
                out = self._maybe_trim(tid, worker_id, out, trim_start, trim_end)
                label = "MP3"
            else:
                ext = os.path.splitext(src)[1].lstrip('.').lower() or 'mp4'
                if force_h264:
                    ext = 'mp4'
                out = self._dedupe_path(os.path.join(folder, f"{title_safe}.{ext}"), tid)
                cleanup.append(out)
                self._replace_media(src, out)
                label = "MP4/H.264" if force_h264 else ext.upper()
                out = self._maybe_trim(tid, worker_id, out, trim_start, trim_end)
                if force_h264:
                    self._ensure_h264_compatible(tid, worker_id, out)
            out = self._publish_output(tid, out, output_folder)
        except BaseException as e:
            try:
                traceback.clear_frames(e.__traceback__)
            except Exception as error:
                log_warning("Could not clear download traceback frames", error, [("task", tid)], once_key=("selector_frames", tid))
            failed = e
        if failed is not None:
            purge()  # Clean up outside the exception handler after releasing traceback-held files.
            raise failed

        self.manager.update_task(tid, status=f"Completed ({label})", progress=100, output_path=out)
        self.manager.notify(f"{raw_title} downloaded ({label}).")

    def _cleanup_workspace(self, work_dir, parent_folder):
        """Remove only our direct child workspace, retrying temporary filesystem locks."""
        target = os.path.realpath(os.path.abspath(work_dir))
        parent = os.path.realpath(os.path.abspath(parent_folder))
        if (os.path.normcase(os.path.dirname(target)) != os.path.normcase(parent)
                or not os.path.basename(target).startswith(".yd-")):
            log_error("Unsafe workspace cleanup was blocked", details=[("folder", work_dir)],
                      event='unsafe_cleanup_blocked', component='download_engine')
            return
        last_error = None
        for attempt in range(8):
            try:
                shutil.rmtree(target)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                last_error = error
                if attempt < 7:
                    gc.collect()
                    time.sleep(0.3)
        log_error("WARNING: could not remove the workspace", details=[("folder", target), ("error", last_error)],
                  level='WARNING', event='workspace_cleanup_failed', component='download_engine')

    def _release_output_reservation(self, path, tid):
        key = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        with self.resource_lock:
            if self.reserved_output_paths.get(key) == tid:
                del self.reserved_output_paths[key]

    def _record_subtitle_start(self, tid, start):
        with self.resource_lock:
            self._subtitle_context.setdefault(tid, {})['start'] = max(0, start or 0)

    def _publish_output(self, tid, source, output_folder, *, apply_naming=True):
        task = self.manager.get_task(tid) or {}
        with self.resource_lock:
            naming = self._output_naming.get(tid)
            state = dict(self._subtitle_context.get(tid, {}))
        def finalize_media():
            if task.get('chapter_mode', 'none') == 'embed':
                from chapter_processing import ChapterProcessor
                ChapterProcessor(self).embed(tid, source, state.get('metadata'), state.get('start', 0))
            if task.get('embed_cover', 'off') == 'on':
                from cover_processing import CoverProcessor
                CoverProcessor(self).prepare(tid, source, state.get('metadata'))
        filename_options = {}
        bundle_options = {}
        if naming and apply_naming:
            folders, stem = naming
            output_folder = ensure_naming_folder(output_folder, folders)
            filename_options['filename'] = stem + os.path.splitext(source)[1]
            bundle_options['stem'] = stem
        if task.get('subtitle_mode', 'none') == 'none':
            finalize_media()
            return self._publish_media_file(tid, source, output_folder, **filename_options)
        from subtitle_processing import SubtitleProcessor, publish_bundle
        with self.resource_lock:
            state = dict(self._subtitle_context.get(tid, {}))
        with SubtitleProcessor(self).prepare(tid, source, state.get('metadata'), state.get('start', 0)) as sidecars:
            finalize_media()
            if sidecars:
                return publish_bundle(self, tid, source, output_folder, sidecars, **bundle_options)
            return self._publish_media_file(tid, source, output_folder, **filename_options)

    def _publish_media_file(self, tid, source, output_folder, *, filename=None):
        """Publish a completed output without overwriting any existing destination."""
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()
        self.manager.report_stage_progress(tid, "publishing", status="Kaydediliyor")
        source_size = os.path.getsize(source)
        desired = os.path.join(output_folder, filename or os.path.basename(source))
        while True:
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            destination = self._dedupe_path(desired, tid)
            created = False
            try:
                try:
                    # Create atomically without overwriting files created after the reservation.
                    os.link(source, destination)
                    created = True
                except FileExistsError:
                    raise
                except OSError:
                    # Use exclusive creation when hard links are unavailable to preserve existing files.
                    self._check_disk_space(output_folder, os.path.getsize(source))
                    with open(destination, "xb") as output:
                        created = True
                        with open(source, "rb") as input_file:
                            copied = 0
                            while True:
                                if self.manager.is_cancelled(tid):
                                    raise DownloadCancelled()
                                block = input_file.read(1024 * 1024)
                                if not block:
                                    break
                                output.write(block)
                                copied += len(block)
                                self.manager.report_stage_progress(
                                    tid, "publishing", copied / source_size if source_size else 0)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                self.manager.report_stage_progress(tid, "publishing", 1)
                return destination
            except FileExistsError:
                self._release_output_reservation(destination, tid)
                if created:
                    remove_leftovers(output_folder, lambda n: n == os.path.basename(destination))
                    raise
                # Another process created this destination; reserve a different name and retry.
            except BaseException:
                if created:
                    remove_leftovers(output_folder, lambda n: n == os.path.basename(destination))
                self._release_output_reservation(destination, tid)
                raise

    def _dedupe_path(self, path, tid):
        """Reserve a unique output path among parallel tasks and existing files."""
        base, ext = os.path.splitext(path)
        candidate = path
        suffix = 0

        with self.resource_lock:
            while True:
                key = os.path.normcase(
                    os.path.realpath(os.path.abspath(candidate))
                )
                if (
                    not os.path.lexists(candidate)
                    and key not in self.reserved_output_paths
                ):
                    self.reserved_output_paths[key] = tid
                    return candidate

                suffix += 1
                extra = f"_{tid}" if suffix == 1 else f"_{tid}_{suffix}"
                candidate = f"{base}{extra}{ext}"

    def _run_ffmpeg(self, tid, worker_id, cmd, duration=0, status_label="Processing", *, check=True,
                    progress_stage="processing", stop_event=None):
        def check_cancelled():
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()

        def run(budget):
            self._invalidate_media_probes(cmd[-1])
            try:
                return self._run_ffmpeg_process(
                    tid, worker_id, budget_command(cmd, budget), duration, status_label,
                    check=check, progress_stage=progress_stage, stop_event=stop_event)
            finally:
                self._invalidate_media_probes(cmd[-1])

        if video_encoder(cmd):
            with self.processing_limits.video_slot(
                    check_cancelled, lambda: self.manager.paused,
                    lambda: self.manager.update_task(tid, status="Waiting for video conversion slot")) as budget:
                return run(budget)
        return run(self.processing_limits.thread_budget())

    def _run_ffmpeg_process(self, tid, worker_id, cmd, duration=0, status_label="Processing", *, check=True,
                            progress_stage="processing", stop_event=None):
        """Register and control each FFmpeg process, always closing it on exit."""
        while self.manager.paused and not self.manager.is_cancelled(tid):
            _check_cancelled(stop_event)
            time.sleep(0.1)
        _check_cancelled(stop_event)
        if self.manager.is_cancelled(tid):
            raise DownloadCancelled()
        self.manager.report_stage_progress(tid, progress_stage, status=status_label)
        disk_folder = self._check_ffmpeg_disk_space(tid, cmd, duration)
        proc = subprocess.Popen(
            [cmd[0], "-nostdin", *cmd[1:]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **NO_WINDOW)
        try:
            self._register_process(proc)
            with self.resource_lock:
                self.current_process_pids[worker_id] = proc.pid
            tail = self._pump_ffmpeg_progress(proc, tid, duration, status_label,
                                              disk_folder=disk_folder, progress_stage=progress_stage,
                                              stop_event=stop_event)
            returncode = proc.wait()
            _check_cancelled(stop_event)
            if self.manager.is_cancelled(tid):
                raise DownloadCancelled()
            if returncode != 0:
                log_error("ERROR FFmpeg process", details=[("task", tid), ("worker", worker_id),
                          ("operation", status_label), ("exit code", returncode), ("command", repr(cmd)),
                          ("ffmpeg", "\n".join(tail))], event='ffmpeg_process_failed', component='download_engine')
                if check:
                    raise MediaOperationError(f"FFmpeg process failed ({returncode}): " + " ".join(tail[-5:]))
            if returncode == 0:
                self.manager.report_stage_progress(tid, progress_stage, 1)
            return returncode
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()  # Killing also terminates a suspended process.
                proc.wait()
            finally:
                try:
                    proc.stdout.close()
                finally:
                    with self.resource_lock:
                        self.managed_processes.discard(proc)
                        if self.current_process_pids.get(worker_id) == proc.pid:
                            self.current_process_pids.pop(worker_id, None)

    def _register_process(self, proc):
        with self.resource_lock:
            self.managed_processes.add(proc)
            stopping = self._force_stopping
        if stopping:
            # Covers a process created just as the shutdown snapshot was taken.
            try:
                if proc.poll() is None:
                    proc.kill()
            finally:
                raise DownloadCancelled()

    def _unregister_process(self, proc):
        with self.resource_lock:
            self.managed_processes.discard(proc)

    def terminate_managed_processes(self):
        """Kill only owned process handles; never look up a possibly reused PID."""
        with self.resource_lock:
            self._force_stopping = True
            processes = list(self.managed_processes)
        for proc in processes:
            try:
                if proc.poll() is None or getattr(proc, '_owned_process_tree', None) is not None:
                    kill_process_tree(proc)
            except OSError as error:
                log_warning("Could not terminate the managed subprocess", error, [("pid", proc.pid)])

    def _pump_ffmpeg_progress(self, proc, tid, duration, status_label, *, disk_folder=None,
                              progress_stage="processing", stop_event=None):
        """Check cancellation and pause regularly, even when FFmpeg produces no output."""
        output = Queue()
        tail = []
        read_errors = []
        time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

        def read_output():
            try:
                for line in proc.stdout:
                    output.put(line)
            except Exception as error:
                read_errors.append(error)
            finally:
                output.put(None)

        reader = threading.Thread(target=copy_context().run, args=(read_output,), daemon=True)
        reader.start()
        suspended = False
        output_closed = False
        last_update = 0
        last_disk_check = 0
        try:
            while True:
                _check_cancelled(stop_event)
                if self.manager.is_cancelled(tid):
                    raise DownloadCancelled()
                now = time.monotonic()
                if disk_folder and now - last_disk_check >= 1:
                    self._check_disk_space(disk_folder, 0)
                    last_disk_check = now
                if proc.poll() is None and self.manager.paused != suspended:
                    try:
                        process = psutil.Process(proc.pid)
                        if self.manager.paused:
                            process.suspend()
                            suspended = True
                            self.manager.update_task(tid, status="Paused")
                        else:
                            process.resume()
                            suspended = False
                            self.manager.update_task(tid, status=status_label)
                    except psutil.NoSuchProcess:
                        pass
                if output_closed:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                try:
                    line = output.get(timeout=0.1)
                except Empty:
                    continue
                if line is None:
                    if read_errors:
                        raise RuntimeError(f"Could not read FFmpeg output: {read_errors[0]}") from read_errors[0]
                    output_closed = True
                    continue
                tail.append(line.strip())
                del tail[:-40]
                m = time_re.search(line)
                now = time.monotonic()
                if m and duration > 0 and not self.manager.paused and now - last_update >= 0.2:
                    h, mm, ss = m.groups()
                    seconds = int(h) * 3600 + int(mm) * 60 + float(ss)
                    fraction = max(0, min(1, seconds / duration))
                    self.manager.report_stage_progress(
                        tid, progress_stage, fraction, status=f"{status_label}...")
                    last_update = now
            return tail
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            reader.join(timeout=1)

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

