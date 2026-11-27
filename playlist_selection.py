"""Bounded playlist previews; collecting entries never creates download tasks."""
from dataclasses import dataclass
import math
import re
from urllib.parse import parse_qs, urlsplit

import download_engine
from cookie_options import cookie_kwargs
from network_options import network_kwargs
from naming_templates import playlist_naming_context
from errors import DownloadCancelled
from download_support import _check_cancelled
from url_utils import normalize_url
from app_logging import log_warning

MAX_PREVIEW_ENTRIES = 20000


@dataclass(frozen=True)
class PlaylistEntry:
    number: int
    title: str
    url: str
    duration: str
    status: str
    playlist_title: str = ''

    @property
    def available(self):
        return bool(self.url) and self.status == 'Available'


class PreviewCancellation:
    def __init__(self, cancel, shutdown):
        self.cancel, self.shutdown = cancel, shutdown

    def is_set(self):
        return self.cancel.is_set() or self.shutdown.is_set()


def playlist_input_url(url):
    """A watch URL carrying a playlist should preview the original playlist order."""
    parsed = urlsplit(url)
    if parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
                           'youtu.be', 'www.youtu.be'} and parsed.username is None and parsed.port in (None, 80, 443):
        playlist = (parse_qs(parsed.query).get('list') or [''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]+', playlist):
            return 'https://www.youtube.com/playlist?list=' + playlist
    return url


def preview_entry(entry, position, playlist_url):
    entry = entry if isinstance(entry, dict) else {}
    number = entry.get('playlist_index')
    if type(number) is not int or not 1 <= number <= 2147483647:
        number = position
    title = str(entry.get('title') or entry.get('id') or 'Untitled video')
    marker = title.casefold().strip('[] ')
    status = ('Private video' if marker == 'private video' else 'Deleted video' if marker == 'deleted video'
              else 'Unavailable' if entry.get('availability') in ('private', 'unavailable') or
              entry.get('_type') in ('playlist', 'multi_video') else 'Available')
    candidate = entry.get('webpage_url') or entry.get('url') or ''
    if entry.get('url') and entry.get('webpage_url') == entry.get('playlist_webpage_url'):
        candidate = entry['url']
    candidate = candidate if isinstance(candidate, str) else ''
    if not candidate.startswith(('http://', 'https://')):
        identifier = candidate or entry.get('id') or ''
        youtube = ('youtube' in str(entry.get('ie_key') or '').lower() or
                   urlsplit(playlist_url).hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com',
                                                       'music.youtube.com', 'youtu.be', 'www.youtu.be'})
        candidate = ('https://www.youtube.com/watch?v=' + identifier
                     if youtube and isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]+', identifier) else '')
    try:
        parsed = urlsplit(candidate)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or len(candidate) > 32768
                or any(char in candidate for char in '\x00\r\n')):
            candidate = ''
    except ValueError:
        candidate = ''
    if not candidate and status == 'Available':
        status = 'Unavailable'
    duration = '—'
    try:
        seconds = float(entry.get('duration'))
        if math.isfinite(seconds) and seconds >= 0:
            seconds = int(seconds)
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            duration = f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'
    except (TypeError, ValueError, OverflowError):
        pass
    return PlaylistEntry(number, title, normalize_url(candidate), duration, status,
                         playlist_naming_context(entry)['playlist_title'])


def load_playlist_preview(url, options, cancel_event, on_batch, *, limit=MAX_PREVIEW_ENTRIES, resolved_info=None):
    """Emit entry batches for display. Return a title and any partial-load warning."""
    count = 0
    title = ''
    class PreviewLimit(RuntimeError):
        pass

    def consume(entries):
        nonlocal count
        batch = []
        for entry in entries:
            _check_cancelled(cancel_event)
            if count >= limit:
                if batch:
                    on_batch(batch)
                raise PreviewLimit()
            count += 1
            item = preview_entry(entry, count, url)
            batch.append(item)
            if (resolved_info is not None and item.available and isinstance(entry, dict)
                    and entry.get('id') and entry.get('formats')):
                size = resolved_info.prepare(entry)
                if size is not None:
                    resolved_info.put(item.url, entry, size)
        if batch:
            on_batch(batch)
    try:
        info, entries = download_engine._flat_extract(playlist_input_url(url), noplaylist=False,
            cancel_event=cancel_event, on_entries=consume, **cookie_kwargs(options), **network_kwargs(options))
        _check_cancelled(cancel_event)
        if not count:
            if entries:
                consume(entries)
            elif info and info.get('_type', 'video') == 'video' and info.get('id'):
                consume([info])
        title = str((info or {}).get('title') or '')
        return title, ''
    except DownloadCancelled:
        raise
    except PreviewLimit:
        return title, 'The playlist preview reached its entry limit. Only the displayed entries can be selected.'
    except Exception as error:
        log_warning('Could not load playlist preview', error)
        if not count:
            raise
        return title, 'The playlist could not be fully read. Only the displayed entries can be selected.\n' + str(error)
