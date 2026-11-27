"""Headless download workers and playlist link resolution threads."""
import threading
from dataclasses import asdict
from queue import Queue, Empty
from urllib.parse import parse_qs, urlsplit
import download_engine
from cookie_options import cookie_kwargs
from network_options import network_kwargs
from naming_templates import uses_playlist_naming
from errors import DownloadCancelled, PlaylistLinkNotAllowed
from url_utils import normalize_url
from app_logging import log_error, unexpected_traceback
from runtime_tools import env_info


def _link_resolution_key(url, expand_playlist):
    normalized = normalize_url(url)
    try:
        parsed = urlsplit(url if '://' in url or url.startswith('//') else '//' + url)
        if (expand_playlist and parsed.scheme in ('', 'http', 'https') and parsed.username is None
                and parsed.port in (None, 80, 443) and parsed.hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com',
                                                    'music.youtube.com', 'youtu.be', 'www.youtu.be'}):
            playlist = (parse_qs(parsed.query).get('list') or [None])[0]
            if playlist and playlist.isascii() and all(character.isalnum() or character in '_-' for character in playlist):
                return (f'https://www.youtube.com/playlist?list={playlist}', True), None
    except (TypeError, ValueError):
        pass
    mode = False if normalized and normalized.startswith('https://www.youtube.com/watch?v=') else bool(expand_playlist)
    return (normalized, mode), normalized


class Worker(threading.Thread):
    def __init__(self, manager, queue: Queue, worker_id: int):
        super().__init__(daemon=True)
        self.manager = manager
        self.queue = queue
        self.worker_id = worker_id
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            if not self.manager._wait_for_worker_slot(self):
                break
            try:
                task = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                if self._stop_event.is_set():
                    if not self.manager._shutdown_event.is_set():
                        self.queue.put(task)
                    break
                self.manager._process_task(task, self.worker_id, worker=self)
            except Exception as e:
                self.manager._task_error(task[0], f"Worker {self.worker_id} error: {e}")
            finally:
                self.queue.task_done()


class PlaylistFetcher(threading.Thread):
    """Resolve links using immutable task options without accessing GUI objects."""
    def __init__(self, manager, raw_input_url, options, expand_playlist=True):
        super().__init__(daemon=True)
        self.manager = manager
        self.raw_input_url = raw_input_url
        self.options = options
        self.expand_playlist = expand_playlist

    def run(self):
        try:
            if self.manager._shutdown_event.is_set():
                return
            notes = []
            resolved_info = {}
            entry_context = {}
            added_count = submitted_count = 0
            def accept_batch(urls):
                nonlocal added_count, submitted_count
                if self.manager._shutdown_event.is_set():
                    raise DownloadCancelled()
                context = {'entry_context': entry_context} if uses_playlist_naming(asdict(self.options)) else {}
                added_count += len(self.manager.add_urls(urls, self.options, resolved_info=resolved_info, notify=False, **context))
                for candidate in urls:
                    entry_context.pop(candidate, None)
                submitted_count += len(urls)
            input_url = self.raw_input_url
            if self.expand_playlist and uses_playlist_naming(asdict(self.options)):
                from playlist_selection import playlist_input_url
                input_url = playlist_input_url(input_url)
            urls = download_engine._extract_urls_from_input(
                input_url, self.expand_playlist, notes,
                cancel_event=self.manager._shutdown_event, resolved_info=resolved_info,
                **cookie_kwargs(asdict(self.options)),
                **network_kwargs(asdict(self.options)),
                **({'entry_context': entry_context} if uses_playlist_naming(asdict(self.options)) else {}),
                on_batch=accept_batch if self.expand_playlist else None)
            if self.manager._shutdown_event.is_set():
                return
            if urls:
                accept_batch(urls)
            skipped = submitted_count - added_count
            message = f"{added_count} videos added to the queue!"
            if skipped:
                message += f" ({skipped} skipped: the URL is already queued)"
            self.manager.notify(message)
            if notes:
                self.manager.emit("info", title="Playlist could not be read", message="\n".join(notes))
        except DownloadCancelled:
            return
        except PlaylistLinkNotAllowed:
            self.manager.emit("playlist_blocked")
        except Exception as error:
            if self.manager._shutdown_event.is_set():
                return
            log_error("ERROR link resolution", details=[("error", error), ("link", self.raw_input_url),
                                                       ("version", env_info())], tb=unexpected_traceback(),
                      event='link_resolution_failed', component='task_manager')
            self.manager.emit("error", title="Error", message=f"Could not resolve the URL: {error}")
