"""Structured application logs, redaction, rotation and warning aggregation."""
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from app_paths import BASE_DIR
from errors import DownloadCancelled, MediaOperationError
from runtime_tools import env_info
from settings import DEFAULTS as _SETTINGS_DEFAULTS


ERROR_LOG_FILE = os.path.join(BASE_DIR, "errors.log")
DEBUG_LOG_FILE = os.path.join(BASE_DIR, "debug.log")
FALLBACK_ERROR_LOG_FILE = os.path.join(tempfile.gettempdir(), "youtube_downloader_errors.log")
FALLBACK_DEBUG_LOG_FILE = os.path.join(tempfile.gettempdir(), "youtube_downloader_debug.log")
_LOG_LOCK = threading.Lock()
_LOG_STATE = None
_DEBUG_LOG_STATE = None
_WARNING_LOCK = threading.Lock()
_WARNING_STATES = OrderedDict()
_WARNING_REPEAT_SECONDS = _SETTINGS_DEFAULTS['log_repeat_seconds']
_WARNING_CACHE_LIMIT = 1024
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_SESSION_ID = uuid.uuid4().hex[:12]
_LOG_CONTEXT = ContextVar('download_log_context', default=None)
_LOG_DEBUG = os.environ.get('YD_DEBUG_LOG', '1').strip().lower() in ('1', 'true', 'yes')
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


@contextmanager
def log_context(provider=None, **fields):
    """Scope log fields to this execution; providers return a fresh task snapshot.

    Nested scopes inherit the parent. Use copy_context() for new threads.
    """
    parent = _LOG_CONTEXT.get()

    def current():
        return {**(parent() if parent else {}), **(provider() if provider else {}), **fields}

    token = _LOG_CONTEXT.set(current)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def configure_logging(max_bytes, backup_count, *, repeat_seconds=None):
    if type(max_bytes) is not int or not 65536 <= max_bytes <= 100 * 1024 * 1024:
        raise ValueError("Invalid log size")
    if type(backup_count) is not int or not 1 <= backup_count <= 10:
        raise ValueError("Invalid log backup count")
    if repeat_seconds is not None and (type(repeat_seconds) is not int or not 1 <= repeat_seconds <= 3600):
        raise ValueError('Invalid warning repeat interval')
    global _LOG_MAX_BYTES, _LOG_BACKUP_COUNT, _WARNING_REPEAT_SECONDS
    with _LOG_LOCK:
        _LOG_MAX_BYTES, _LOG_BACKUP_COUNT = max_bytes, backup_count
    if repeat_seconds is not None:
        with _WARNING_LOCK:
            _WARNING_REPEAT_SECONDS = repeat_seconds


def _log_text(value):
    text = _ANSI_RE.sub('', str(value))
    # Redact signed URL parameters, credentials and HTTP headers from shareable logs.
    text = re.sub(r'''((?:https?|socks4a?|socks5h?)://)[^\s/'"<>]*@''', r'\1[REDACTED]@', text, flags=re.I)
    text = re.sub(r'([?&](?:token|access_token|refresh_token|key|api_key|signature|sig|lsig|password|auth|authorization|cookie|policy|credential|x-amz-signature|x-amz-credential)=)[^\s&#\'"<>]+',
                  r'\1[REDACTED]', text, flags=re.I)
    text = re.sub(r'(?im)\b(authorization|proxy-authorization|cookie|set-cookie)(\s*[:=]\s*)[^\r\n]+',
                  r'\1\2[REDACTED]', text)
    text = ''.join(c for c in text if c in '\n\t\r' or ord(c) >= 32)
    limit = max(4096, min(65536, _LOG_MAX_BYTES // 4))
    return text if len(text) <= limit else text[:limit // 2] + '\n[... truncated ...]\n' + text[-limit // 2:]


def _write_log(path, block):
    handler = RotatingFileHandler(path, maxBytes=_LOG_MAX_BYTES,
                                  backupCount=_LOG_BACKUP_COUNT, encoding='utf-8', delay=True)
    try:
        # Let write errors reach the caller so it can try a fallback; emit() would swallow them.
        record = logging.LogRecord('downloader', logging.ERROR, '', 0, block.rstrip('\n'), (), None)
        if handler.shouldRollover(record):
            handler.doRollover()
        with open(path, 'a', encoding='utf-8', errors='backslashreplace') as stream:
            stream.write(block)
    finally:
        handler.close()


def unexpected_traceback(error=None):
    """Keep technical details unless the failure is explicitly anticipated."""
    exc = error if error is not None else sys.exc_info()[1]
    if exc is None:
        return None
    expected = (MediaOperationError,)
    if not isinstance(exc, BaseException) or isinstance(exc, expected):
        return None
    return ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def get_log_status():
    """Return the actual destination of the latest successful write."""
    with _LOG_LOCK:
        paths = (ERROR_LOG_FILE, FALLBACK_ERROR_LOG_FILE)
        state = _LOG_STATE if _LOG_STATE and _LOG_STATE['paths'] == paths else None
        return {'path': state['path'] if state else ERROR_LOG_FILE,
                'write_failed': state['write_failed'] if state else False,
                'primary_path': ERROR_LOG_FILE, 'fallback_path': FALLBACK_ERROR_LOG_FILE}


def log_location_text(status=None):
    status = get_log_status() if status is None else status
    if status['write_failed']:
        return (f"Latest diagnostics could not be saved.\nPrimary log: {status['primary_path']}\n"
                f"Fallback log: {status['fallback_path']}")
    label = 'Fallback log file' if status['path'] == status['fallback_path'] else 'Log file'
    return f"{label}: {status['path']}"


def log_error(message: str, details=None, tb=None, *, level='ERROR', event=None, component=None,
              task=None, attempt=None, stage=None):
    """Write an explicitly leveled record with optional event/component context.

    Levels are DEBUG, INFO, WARNING, ERROR and CRITICAL (case-insensitive).
    Message text never determines severity. Existing calls default to ERROR.
    """
    global _LOG_STATE, _DEBUG_LOG_STATE
    level = level.upper() if isinstance(level, str) else level
    if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError('Invalid log level')
    if level == 'DEBUG' and not _LOG_DEBUG:
        return True
    provider = _LOG_CONTEXT.get()
    fields = provider() if provider else {}
    for label, value in (('task', task), ('attempt', attempt), ('stage', stage)):
        if value is not None:
            fields[label] = value
    context = ''
    for label, value in (('event', event), ('component', component),
                         *((key, fields.get(key)) for key in ('task', 'attempt', 'stage'))):
        if value is not None:
            value = _log_text(value).strip().replace('\n', ' ').replace('\r', ' ')
            context += f' [{label}={value.replace("[", "(").replace("]", ")")}]'
    out = [f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] "
           f"[{level}] [session={_SESSION_ID} pid={os.getpid()} thread={_log_text(threading.current_thread().name)}] "
           f"{context.lstrip() + ' ' if context else ''}"
           f"{_log_text(message).strip().replace(chr(10), ' ').replace(chr(13), ' ')}"]
    for label, value in (details or []):
        if value is None or str(value).strip() == "":
            continue
        lines = _log_text(value).strip().splitlines()
        if not lines:
            continue
        out.append(f"    {_log_text(label):<8}: {lines[0]}")
        out += [f"    {'':<8}  {line}" for line in lines[1:]]
    if tb:
        out.append("    traceback:")
        out += [f"      {line}" for line in _log_text(tb).rstrip().splitlines()]
    block = "\n".join(out) + "\n\n"
    debug_record = level in ('DEBUG', 'INFO')
    primary_path = DEBUG_LOG_FILE if debug_record else ERROR_LOG_FILE
    fallback_path = FALLBACK_DEBUG_LOG_FILE if debug_record else FALLBACK_ERROR_LOG_FILE
    with _LOG_LOCK:
        paths = (primary_path, fallback_path)
        state = _DEBUG_LOG_STATE if debug_record else _LOG_STATE
        if state is None or state['paths'] != paths:
            state = {'paths': paths, 'path': None, 'write_failed': False}
            if debug_record:
                _DEBUG_LOG_STATE = state
            else:
                _LOG_STATE = state
        try:
            _write_log(primary_path, block)
            state.update(path=primary_path, write_failed=False)
            return True
        except (OSError, ValueError) as error:
            fallback = (_log_text(f"Could not write the primary log ({primary_path}): {error}\n"
                        f"Fallback log: {fallback_path}\n") + block)
            stream = sys.stderr or sys.__stderr__
            if stream is not None:
                try:
                    stream.write(fallback)
                    stream.flush()
                except (OSError, ValueError):
                    # Avoid recursive logging when reporting a logging failure.
                    stream = None
            try:
                _write_log(fallback_path, fallback)
                state.update(path=fallback_path, write_failed=False)
                return True
            except (OSError, ValueError):
                state['write_failed'] = True
                return False  # Logging failure must not interrupt the original operation.


@dataclass
class _WarningState:
    last_logged: float
    message: str
    details: list
    fields: dict
    suppressed: int = 0


def _write_warning_summary(state, now):
    """Caller holds _WARNING_LOCK; cached data contains no exception objects."""
    token = _LOG_CONTEXT.set(None)
    try:
        return log_error(f'{state.message} (repeated {state.suppressed} times)',
                         details=[*state.details, ('repeated', state.suppressed),
                                  ('repeat window', f'{max(0, now - state.last_logged):.1f}s')],
                         level='WARNING', **state.fields)
    finally:
        _LOG_CONTEXT.reset(token)


def flush_warning_summaries(*, task=None, attempt=None):
    """Flush pending repeats at task completion or application shutdown.

    With no filters, flush all warnings. Failed writes remain available to retry.
    """
    with _WARNING_LOCK:
        now = time.monotonic()
        for key, state in list(_WARNING_STATES.items()):
            if task is not None and key[0] != task:
                continue
            if attempt is not None and key[1] != attempt:
                continue
            if not state.suppressed or _write_warning_summary(state, now):
                del _WARNING_STATES[key]


def log_warning(context, error, details=None, *, once_key=None, event=None, component=None,
                task=None, attempt=None, stage=None):
    """Log warnings; keyed repeats produce summaries at the configured interval."""
    provider = _LOG_CONTEXT.get()
    fields = provider() if provider else {}
    for label, value in (('task', task), ('attempt', attempt), ('stage', stage)):
        if value is not None:
            fields[label] = value
    fields.update(event=event, component=component)
    message = f'WARNING {context}'
    record_details = [('error', error), ('exit code', getattr(error, 'returncode', None)),
                      ('program', getattr(error, 'output', None)), *(details or [])]
    if once_key is None:
        return log_error(message, details=record_details, tb=unexpected_traceback(error),
                         level='WARNING', **fields)

    # Cache only sanitized strings, not exceptions, tracebacks or task providers.
    cached_details = [(_log_text(label), _log_text(value)) for label, value in record_details
                      if value is not None]
    cached_fields = {key: _log_text(value) if value is not None else None
                     for key, value in fields.items()}
    key = (fields.get('task'), fields.get('attempt'), once_key)
    with _WARNING_LOCK:
        now = time.monotonic()
        state = _WARNING_STATES.get(key)
        if state is not None:
            _WARNING_STATES.move_to_end(key)
            state.message = _log_text(message)
            state.details = cached_details
            state.fields = cached_fields
            state.suppressed += 1
            status = get_log_status()
            log_exists = bool(status['path']) and os.path.isfile(status['path'])
            if now - state.last_logged < _WARNING_REPEAT_SECONDS and log_exists:
                return
            if _write_warning_summary(state, now):
                state.last_logged = now
                state.suppressed = 0
            return

        if len(_WARNING_STATES) >= _WARNING_CACHE_LIMIT:
            _, oldest = _WARNING_STATES.popitem(last=False)
            if oldest.suppressed:
                _write_warning_summary(oldest, now)
        if log_error(message, details=record_details, tb=unexpected_traceback(error),
                     level='WARNING', **fields):
            _WARNING_STATES[key] = _WarningState(now, _log_text(message), cached_details, cached_fields)


def install_exception_hooks():
    """Persist uncaught main-thread and unmanaged background-thread exceptions."""
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def report(error_type, error, tb, thread_name):
        if issubclass(error_type, (KeyboardInterrupt, SystemExit, DownloadCancelled)):
            return
        log_error("ERROR uncaught exception", details=[("error type", error_type.__name__),
                  ("error", error), ("thread", thread_name), ("version", env_info())],
                  tb=''.join(traceback.format_exception(error_type, error, tb)),
                  event='uncaught_exception', component='runtime')

    def sys_hook(error_type, error, tb):
        report(error_type, error, tb, threading.current_thread().name)
        previous_sys(error_type, error, tb)

    def thread_hook(args):
        report(args.exc_type, args.exc_value, args.exc_traceback,
               args.thread.name if args.thread else '?')
        previous_thread(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
