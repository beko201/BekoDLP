"""Legacy utility imports; implementations belong to the modules below.

Application code imports those modules directly. These explicit exports retain
existing function and exception identities for external users of utils.
Log configuration and mutable logger state are owned by app_logging.
"""
from app_paths import BASE_DIR, SETTINGS_FILE
from app_logging import (
    ERROR_LOG_FILE,
    DEBUG_LOG_FILE,
    FALLBACK_ERROR_LOG_FILE,
    FALLBACK_DEBUG_LOG_FILE,
    log_context,
    configure_logging,
    unexpected_traceback,
    get_log_status,
    log_location_text,
    log_error,
    flush_warning_summaries,
    log_warning,
    install_exception_hooks,
    _log_text,
)
from download_support import (
    DEFAULT_WORKERS,
    MAX_FETCH_RETRIES,
    DISK_HEADROOM_BYTES,
    _check_cancelled,
    _YtDlpLogger,
    _network_options,
)
from errors import (
    DownloadCancelled,
    InsufficientDiskSpace,
    PlaylistLinkNotAllowed,
    MediaOperationError,
)
from file_utils import part_matcher, remove_leftovers, safe_filesize, format_mb, safe_filename
from process_utils import _hidden_window_kwargs, NO_WINDOW, run_cmd_capture, psutil
from runtime_tools import portable_deno_path, which_executable, env_info, _normalize_version
from time_utils import parse_timecode, validate_trim_range, format_eta
from url_utils import normalize_url
