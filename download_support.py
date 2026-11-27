"""Shared download budgets, cancellation checks and yt-dlp request options."""
from app_logging import log_warning
from errors import DownloadCancelled
from runtime_tools import portable_deno_path
from settings import DEFAULTS as _SETTINGS_DEFAULTS


DEFAULT_WORKERS = _SETTINGS_DEFAULTS['workers']


MAX_FETCH_RETRIES = 3


DISK_HEADROOM_BYTES = 64 * 1024 * 1024


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled()


class _YtDlpLogger:
    """Forward warnings to the application log; callers record errors."""

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        log_warning("yt-dlp", message, once_key=("yt-dlp", message),
                    event='extractor_warning', component='yt-dlp')

    def error(self, message):
        pass


def _network_options(cancel_event, *, stop_event=None):
    options = {'socket_timeout': 5, 'extractor_retries': 1,
            'cancel_check': lambda: (_check_cancelled(cancel_event), _check_cancelled(stop_event)),
            'logger': _YtDlpLogger()}
    # Resolve relative to the application, not the IDE's working directory.
    # With no portable runtime installed, yt-dlp retains its normal PATH lookup.
    deno = portable_deno_path()
    if deno:
        options['js_runtimes'] = {'deno': {'path': deno}}
    return options
