"""Bounded, redacted diagnostic reports without GUI or network dependencies."""
import os
import re
import platform
import subprocess
import threading
from datetime import datetime
from importlib import metadata
from pathlib import Path
import tempfile

import app_logging
import app_paths
import process_utils
import runtime_tools
from app_info import APP_NAME, APP_VERSION


LOG_TAIL_BYTES = 64 * 1024
TOOL_VERSION_TIMEOUT = 2
_RUNTIME_LOCK = threading.Lock()
_RUNTIME_INFO = None
_SESSION_LOGGED = False


def _tool_version(name, executable=None):
    executable = executable if isinstance(executable, str) and executable else runtime_tools.which_executable([name])
    if not executable:
        return 'unavailable (not found)'
    try:
        result = subprocess.run([executable, '--version' if name == 'deno' else '-version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace',
                                timeout=TOOL_VERSION_TIMEOUT, **process_utils.NO_WINDOW)
    except subprocess.TimeoutExpired:
        return 'unavailable (version check timed out)'
    except OSError as error:
        return f'unavailable ({type(error).__name__})'
    if result.returncode != 0:
        return f'unavailable (exit code {result.returncode})'
    prefix = 'deno ' if name == 'deno' else f'{name} version '
    for line in result.stdout.splitlines():
        if line.lower().startswith(prefix):
            version = line[len(prefix):].strip()[:256]
            return version.split()[0] if name == 'deno' and version else version
    return 'unavailable (unexpected version output)'


def _package_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return 'unavailable (not installed)'


def invalidate_runtime_info():
    """A manual tool update invalidates the process's version snapshot."""
    global _RUNTIME_INFO
    with _RUNTIME_LOCK:
        _RUNTIME_INFO = None


def get_runtime_info(ffmpeg=None, ffprobe=None):
    """Collect one bounded version snapshot per process and return a copy."""
    global _RUNTIME_INFO
    with _RUNTIME_LOCK:
        if _RUNTIME_INFO is None:
            from ytdlp_process import executable_version
            yt_version = executable_version()
            _RUNTIME_INFO = {'Application': f'{APP_NAME} {APP_VERSION}',
                             'Python': platform.python_version(), 'Platform': platform.system(),
                             'yt-dlp': yt_version, 'PySide6': _package_version('PySide6'),
                             'FFmpeg': _tool_version('ffmpeg', ffmpeg),
                             'ffprobe': _tool_version('ffprobe', ffprobe),
                             'Deno': _tool_version('deno', runtime_tools.portable_deno_path()),
                             'yt-dlp backend': 'external executable (bundled EJS)'}
        return dict(_RUNTIME_INFO)


def initialize_runtime_diagnostics(ffmpeg=None, ffprobe=None):
    """Write the cached version snapshot once; retry if no log sink worked."""
    global _SESSION_LOGGED
    info = get_runtime_info(ffmpeg, ffprobe)
    with _RUNTIME_LOCK:
        if not _SESSION_LOGGED:
            _SESSION_LOGGED = app_logging.log_error('Application session started', level='INFO',
                                             event='session_started', component='application',
                                             details=list(info.items()))
    return info


def mask_personal_paths(text, output_folders=()):
    """Hide known output folders and home roots, including foreign tracebacks."""
    text = str(text)
    homes = {str(Path.home()), os.environ.get('USERPROFILE', ''), os.environ.get('HOME', '')}
    roots = {home: '<USER_HOME>' for home in homes}
    roots.update({folder: '<OUTPUT_FOLDER>' for folder in output_folders if folder})
    for root in sorted(roots, key=len, reverse=True):
        if len(root.rstrip('/\\')) < 2 or not re.match(r'^(?:[A-Za-z]:[\\/]|[\\/])', root):
            continue
        parts = re.split(r'[\\/]+', root.rstrip('/\\'))
        pattern = r'[\\/]+'.join(re.escape(part) for part in parts)
        text = re.sub(pattern + r'(?=$|[\\/\s\'"<>:,;)])', roots[root], text, flags=re.I)
    text = re.sub(r'(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+|'
                  r'/(?:home|Users)/)[^\\/\r\n\'"<>:]+', '<USER_HOME>', text, flags=re.I)
    return re.sub(r'(?<![A-Za-z0-9])/root(?=$|[/\s\'"<>:,;)])', '<USER_HOME>', text)


def _report_text(value, output_folders=()):
    return app_logging._log_text(mask_personal_paths(value, output_folders))


def build_diagnostic_report(details='', tasks=()):
    output_folders = []
    for task in tasks[:100]:
        if isinstance(task.get('folder'), str):
            output_folders.append(task['folder'])
        if isinstance(task.get('output_path'), str):
            output_folders.append(re.sub(r'[\\/][^\\/]+$', '', task['output_path']))
    status = app_logging.get_log_status()
    sections = ['Video Downloader diagnostic report',
                f'Generated: {datetime.now().astimezone().isoformat(timespec="seconds")}',
                f'Session: {app_logging._SESSION_ID}',
                *[f'{key}: {value}' for key, value in get_runtime_info().items()],
                app_logging.log_location_text(status)]
    if details:
        sections.extend(['\nError details', _report_text(details, output_folders)])
    if tasks:
        sections.append('\nTasks (up to 100)')
        fields = ('id', 'title', 'status', 'url', 'attempt', '_log_stage',
                  'output_mode', 'container', 'video_codec', 'audio_codec')
        for task in tasks[:100]:
            sections.append(_report_text('\n'.join(f'{key}: {task[key]}' for key in fields if key in task),
                                         output_folders))
            sections.append('')
    sections.append('\nRecent log (up to 64 KiB)')
    path = status['path']
    if path:
        try:
            # Rotation and writes use the same lock, so this snapshot is consistent.
            with app_logging._LOG_LOCK, open(path, 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - LOG_TAIL_BYTES))
                tail = stream.read(LOG_TAIL_BYTES).decode('utf-8', errors='replace')
            sections.append(_report_text(tail, output_folders) if tail else '(Log is empty.)')
        except OSError as error:
            sections.append(_report_text(f'Log unavailable: {error}', output_folders))
    else:
        sections.append('(No log could be saved.)')
    return mask_personal_paths('\n'.join(sections), output_folders) + '\n'


def save_diagnostic_report(path, report):
    """Replace the chosen report atomically; leave an existing file intact on failure."""
    destination = Path(path)
    status = app_logging.get_log_status()
    for source in (status['primary_path'], status['fallback_path'],
                   app_logging.DEBUG_LOG_FILE, app_logging.FALLBACK_DEBUG_LOG_FILE, app_paths.SETTINGS_FILE):
        source = Path(source).resolve()
        target = destination.resolve()
        if target == source or (target.parent == source.parent and
                                target.name.startswith(source.name + '.')):
            raise ValueError('Choose a report filename outside the application logs and settings files.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=destination.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(report)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
