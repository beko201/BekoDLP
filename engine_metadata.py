"""Link resolution, format descriptions and shared media metadata caches."""
import re
import math
import time
import threading
import urllib.request
from urllib.parse import parse_qs, urlsplit
from copy import deepcopy
from ytdlp_process import YtDlpProcess, YtDlpProcessError
from errors import MediaOperationError
from cookie_options import cookie_selection, cookie_kwargs
from network_options import network_selection, network_kwargs
from naming_templates import playlist_naming_context
from errors import DownloadCancelled, PlaylistLinkNotAllowed
from download_support import MAX_FETCH_RETRIES, _check_cancelled, _network_options
from url_utils import normalize_url
from app_logging import log_error, log_warning
from runtime_tools import env_info


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
