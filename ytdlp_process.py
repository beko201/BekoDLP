"""yt-dlp command-line adapter. No yt-dlp Python imports or private APIs.

Only JSON and explicitly tagged progress templates cross the process boundary.
The application's FFmpeg pipeline still owns conversion and output validation.
"""
from collections import deque
from contextlib import contextmanager
from functools import lru_cache
import json
import os
from http.cookiejar import MozillaCookieJar, LoadError
from pathlib import Path
from queue import Queue, Empty, Full
import re
import subprocess
import tempfile
import threading
import time
import warnings

from errors import MediaOperationError
from utils import NO_WINDOW, DownloadCancelled, psutil, which_executable
from cookie_options import validate_cookie_options
from network_options import validate_network_options
from process_tree import spawn_owned_process

_COOKIE_PARSE_LOCK = threading.Lock()


class YtDlpProcessError(MediaOperationError):
    def __init__(self, message, returncode=None):
        super().__init__(message)
        self.returncode = returncode
        match = re.search(r'HTTP Error (401|403|410)\b', message)
        self.status = int(match[1]) if match else None


def executable_path():
    return which_executable(['yt-dlp', 'yt-dlp.exe'])


def require_executable():
    executable = executable_path()
    if not executable:
        raise YtDlpProcessError('yt-dlp executable was not found. Run python tools/install_ytdlp.py '
                               'or install yt-dlp on PATH.')
    return executable


@lru_cache(maxsize=4)
def executable_version(executable=None):
    executable = executable or executable_path()
    if not executable:
        return 'unavailable (not found)'
    try:
        result = subprocess.run([executable, '--ignore-config', '--version'], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=5, **NO_WINDOW)
        version = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r'[0-9][\w.+-]{0,100}', version):
            return version
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'unavailable (version check failed)'


def kill_process_tree(proc, descendants=()):
    """Stop descendants before the owned Popen handle; include suspended children."""
    owned = getattr(proc, '_owned_process_tree', None)
    if owned is not None:
        owned.terminate()
        proc.wait()
        if descendants:
            psutil.wait_procs(list(descendants), timeout=2)
        return
    try:
        if proc.poll() is None:
            parent = psutil.Process(proc.pid)
            parent.suspend()
            children = parent.children(recursive=True)
        else:
            children = []
    except psutil.NoSuchProcess:
        children = []
    children = list({child.pid: child for child in [*descendants, *children]}.values())
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    if children:
        psutil.wait_procs(children, timeout=2)


class YtDlpProcess:
    """Translate application options into the documented yt-dlp CLI contract."""
    def __init__(self, options=None, *, check=None, is_paused=None, on_start=None, on_finish=None,
                 on_tick=None, executable=None):
        self.options = dict(options or {})
        self.check = check or self.options.get('cancel_check') or (lambda: None)
        self.is_paused = is_paused or (lambda: False)
        self.on_start, self.on_finish, self.on_tick = on_start, on_finish, on_tick
        self.executable = executable

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _arguments(self):
        args = [self.executable or require_executable(), '--ignore-config', '--no-plugin-dirs',
                '--no-colors', '--encoding', 'utf-8', '--no-cache-dir']
        selected = validate_network_options(self.options)
        if selected['proxy_mode'] != 'system':
            args += ['--proxy', selected['proxy_url'] if selected['proxy_mode'] == 'custom' else '']
        if selected['download_rate']:
            args += ['--limit-rate', selected['download_rate']]
        if self.options.get('download_section') is not None:
            import math
            start, end = self.options['download_section']
            if (type(start) not in (int, float) or not math.isfinite(start) or start < 0 or
                    end is not None and (type(end) not in (int, float) or not math.isfinite(end) or end <= start)):
                raise ValueError('Invalid download section.')
            args += ['--download-sections', f'*{start:.9f}-' + (f'{end:.9f}' if end is not None else 'inf'),
                     '--downloader-args', 'ffmpeg_o:-copyts -start_at_zero -avoid_negative_ts disabled -movflags +faststart',
                     '--no-force-keyframes-at-cuts']
        mapping = {'format': '--format', 'outtmpl': '--output', 'ffmpeg_location': '--ffmpeg-location',
                   'merge_output_format': '--merge-output-format', 'socket_timeout': '--socket-timeout',
                   'retries': '--retries', 'fragment_retries': '--fragment-retries',
                   'extractor_retries': '--extractor-retries',
                   'concurrent_fragment_downloads': '--concurrent-fragments'}
        for key, flag in mapping.items():
            if self.options.get(key) is not None:
                args += [flag, str(self.options[key])]
        if self.options.get('noplaylist'):
            args.append('--no-playlist')
        if self.options.get('keepvideo'):
            args.append('--keep-video')
        for runtime, config in self.options.get('js_runtimes', {}).items():
            args += ['--js-runtimes', runtime + (':' + config['path'] if config.get('path') else '')]
        if self.options.get('ffmpeg_threads'):
            args += ['--postprocessor-args', f'ffmpeg:-threads {self.options["ffmpeg_threads"]}']
        # Keep cancellation responsive during retry delays too.
        args += ['--retry-sleep', '0']
        return args

    def _wait_before_start(self):
        while True:
            self.check()
            if not self.is_paused():
                return
            time.sleep(.05)

    def _run(self, args, consume):
        self._wait_before_start()
        with self._cookie_arguments() as auth:
            return self._run_process([args[0], *auth, *args[1:]], consume)

    @contextmanager
    def _cookie_arguments(self):
        try:
            selected = validate_cookie_options(self.options)
        except ValueError as error:
            raise YtDlpProcessError(str(error)) from error
        if selected['cookie_mode'] == 'browser':
            browser = selected['cookie_browser']
            if selected['cookie_profile']:
                browser += ':' + selected['cookie_profile']
            yield ['--cookies-from-browser', browser]
        elif selected['cookie_mode'] == 'file':
            # yt-dlp writes its cookie jar on exit. Each process gets its own
            # disposable copy, so parallel streams never modify the source jar.
            with tempfile.TemporaryDirectory(prefix='bekodlp-cookies-') as folder:
                copy = Path(folder) / 'cookies.txt'
                try:
                    with open(selected['cookie_file'], 'rb') as source:
                        content = source.read(16 * 1024 ** 2 + 1)
                    if len(content) > 16 * 1024 ** 2:
                        raise YtDlpProcessError('The cookie file exceeds the 16 MiB limit.')
                    copy.write_bytes(content)
                    del content
                    with _COOKIE_PARSE_LOCK, warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        MozillaCookieJar(str(copy)).load(ignore_discard=True, ignore_expires=True)
                except (OSError, ValueError, UnicodeError, LoadError) as error:
                    # CookieJar parse errors may include the full secret line.
                    # Do not pass those details into application diagnostics.
                    raise YtDlpProcessError('The cookie file could not be read as Netscape cookies.txt. '
                                           'Choose an existing, valid cookie file.') from None
                self.check()
                yield ['--cookies', str(copy)]
        else:
            yield []

    def _run_process(self, args, consume):
        self._wait_before_start()
        env = None
        if self.options.get('proxy_mode') == 'direct':
            env = {key: value for key, value in os.environ.items()
                   if key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
        proc = spawn_owned_process(args, stdout=subprocess.PIPE, env=env,
                                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                bufsize=1, **dict(NO_WINDOW, stdin=subprocess.DEVNULL))
        lines, stopped, tail = Queue(maxsize=128), threading.Event(), deque(maxlen=12)
        suspended, descendants = {}, {}
        parent = None

        def reader():
            try:
                for line in proc.stdout:
                    while not stopped.is_set():
                        try:
                            lines.put(line.rstrip('\r\n'), timeout=.05)
                            break
                        except Full:
                            continue
                    if stopped.is_set():
                        break
            finally:
                while not stopped.is_set():
                    try:
                        lines.put(None, timeout=.05)
                        break
                    except Full:
                        continue

        thread = threading.Thread(target=reader, daemon=True, name='yt-dlp-output')
        started = False
        try:
            try:
                parent = psutil.Process(proc.pid)
            except psutil.NoSuchProcess:
                pass
            if self.on_start:
                self.on_start(proc)
            thread.start()
            started = True
            last_tick, eof, parent_exited = 0, False, False
            while True:
                self.check()
                if proc.poll() is not None and not parent_exited:
                    # Parent exit is enough: an orphan may hold the pipe open
                    # silently or continue filling it, so do not wait for Empty.
                    kill_process_tree(proc, descendants.values())
                    parent_exited = True
                if parent is not None and proc.poll() is None:
                    try:
                        descendants.update((p.pid, p) for p in parent.children(recursive=True))
                    except psutil.NoSuchProcess:
                        pass
                paused = self.is_paused()
                if paused and parent is not None and proc.poll() is None:
                    try:
                        # Suspend the parent first so it cannot launch another FFmpeg child.
                        for process in [parent, *parent.children(recursive=True)]:
                            if process.pid not in suspended:
                                process.suspend()
                                suspended[process.pid] = process
                    except psutil.NoSuchProcess:
                        pass
                elif suspended:
                    for process in reversed(list(suspended.values())):
                        try:
                            process.resume()
                        except psutil.NoSuchProcess:
                            pass
                    suspended.clear()
                now = time.monotonic()
                if self.on_tick and now - last_tick >= .1:
                    self.on_tick()
                    last_tick = now
                # Do not invoke progress hooks while paused: they may wait for resume.
                # Cancellation must stay on this polling thread so a paused child can be killed.
                if paused and proc.poll() is None:
                    time.sleep(.05)
                    continue
                if eof and proc.poll() is not None:
                    break
                try:
                    line = lines.get(timeout=.05)
                except Empty:
                    continue
                if line is None:
                    eof = True
                    continue
                if not consume(line):
                    tail.append(line[-2048:])
                    if line.startswith('WARNING:') and self.options.get('logger'):
                        self.options['logger'].warning(line)
            code = proc.wait()
            self.check()
            if code:
                raise YtDlpProcessError('yt-dlp failed: ' + '\n'.join(tail), code)
        finally:
            stopped.set()
            try:
                kill_process_tree(proc, descendants.values())
            finally:
                # Closing the Windows job is a second kernel-enforced cleanup
                # path and must precede joining/closing the output reader.
                proc._owned_process_tree.close()
                if started:
                    thread.join(timeout=3)
                proc.stdout.close()
                if self.on_finish:
                    self.on_finish(proc)

    @contextmanager
    def _info_file(self, info):
        with tempfile.TemporaryDirectory(prefix='bekodlp-info-') as folder:
            path = Path(folder) / 'source.json'
            path.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
            yield str(path)

    def _metadata(self, args):
        result = []
        def consume(line):
            if not line.startswith('{'):
                return False
            try:
                info = json.loads(line)
            except ValueError as error:
                raise YtDlpProcessError('yt-dlp returned invalid metadata JSON') from error
            if not isinstance(info, dict):
                raise YtDlpProcessError('yt-dlp returned an invalid metadata document')
            result.append(info)
            return True
        self._run([*self._arguments(), '--dump-single-json', '--simulate', *args], consume)
        if len(result) != 1:
            raise YtDlpProcessError('yt-dlp did not return one metadata document')
        return result[0]

    def extract_info(self, url, download=False):
        if download:
            return self._download(['--', url])
        return self._metadata(['--', url])

    def process_ie_result(self, info, download=True):
        with self._info_file(info) as path:
            args = ['--load-info-json', path]
            return self._download(args) if download else self._metadata(args)

    def download(self, urls):
        for url in urls:
            self._download(['--', url])
        return 0

    def download_thumbnail(self, info, folder, *, max_bytes=20 * 1024 ** 2):
        """Fetch one source image through CLI cookie/proxy handling, without media."""
        folder = Path(folder).resolve()
        def files():
            return [path for path in folder.glob('cover.*') if path.suffix not in ('.part', '.ytdl', '.temp')]
        def check_size():
            for path in folder.glob('cover.*'):
                if path.is_symlink() or path.resolve().parent != folder:
                    raise YtDlpProcessError('The cover image path is invalid.')
                if path.is_file() and path.stat().st_size > max_bytes:
                    raise YtDlpProcessError('The cover image exceeds the size limit.')
        original_tick = self.on_tick
        def tick():
            if original_tick:
                original_tick()
            check_size()
        self.on_tick = tick
        try:
            with self._info_file(info) as path:
                self._run([*self._arguments(), '--skip-download', '--no-simulate', '--write-thumbnail',
                    '--no-write-playlist-metafiles', '--output', 'thumbnail:' + str(folder / 'cover.%(ext)s'),
                    '--load-info-json', path], lambda line: False)
            check_size()
            candidates = files()
            return candidates[0] if len(candidates) == 1 else None
        finally:
            self.on_tick = original_tick

    def download_subtitles(self, info, output_template, languages, *, automatic=False):
        """Fetch text tracks only through the documented CLI; never download media again."""
        results = []
        def consume(line):
            if not line.startswith('BEKO_SUBTITLES:'):
                return False
            payload = line.partition(':')[2]
            value = {} if payload in ('NA', 'null') else json.loads(payload)
            results.append(value or {})
            return True
        with self._info_file(info) as path:
            args = [*self._arguments(), '--skip-download', '--no-simulate', '--ignore-no-formats-error',
                    '--write-subs', '--write-auto-subs' if automatic else '--no-write-auto-subs',
                    '--sub-langs', ','.join(languages), '--sub-format', 'srt/vtt/ass/ttml/best',
                    '--convert-subs', 'srt', '--output', output_template,
                    '--print', 'after_video:BEKO_SUBTITLES:%(requested_subtitles)j', '--load-info-json', path]
            self._run(args, consume)
        if len(results) != 1 or not isinstance(results[0], dict):
            raise YtDlpProcessError('yt-dlp did not report the downloaded subtitles.')
        return results[0]

    def _download(self, source):
        result = []
        # JSON conversions escape filenames and metadata; ordinary console text is never parsed.
        progress = ('BEKO_PROGRESS:%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,filename})j'
                    '\t%(info.format_id)j')
        quiet = ['--no-quiet'] if self.options.get('download_section') is not None else ['--quiet']
        args = [*self._arguments(), '--no-simulate', *quiet, '--progress', '--newline',
                '--progress-delta', '0.1', '--progress-template', 'download:' + progress,
                '--print', 'before_dl:BEKO_START:%(format_id)j',
                '--print', 'post_process:BEKO_PROCESS:%(format_id)j',
                '--print', 'after_move:BEKO_RESULT:%(.{id,title,duration,filepath,_filename,ext,format_id})j',
                *source]
        def consume(line):
            if line.startswith('BEKO_PROGRESS:'):
                payload, _, format_id = line.partition(':')[2].rpartition('\t')
                data = json.loads(payload)
                data['info_dict'] = {'format_id': json.loads(format_id)}
                for key in ('downloaded_bytes', 'total_bytes', 'total_bytes_estimate'):
                    if not isinstance(data.get(key), (int, float)):
                        data[key] = 0
                for hook in self.options.get('progress_hooks', []):
                    hook(data)
                return True
            if line.startswith('BEKO_PROCESS:'):
                for hook in self.options.get('postprocessor_hooks', []):
                    hook({'status': 'started'})
                return True
            if line.startswith('BEKO_START:'):
                return True
            if line.startswith('BEKO_RESULT:'):
                result.append(json.loads(line.partition(':')[2]))
                return True
            return False
        self._run(args, consume)
        if len(result) != 1:
            raise YtDlpProcessError('yt-dlp did not report a completed output file')
        return result[0]

    def flat_extract(self, url, *, on_entries=None, batch_size=100):
        """Stream flat playlist entries without retaining yt-dlp's paging caches."""
        preview, batch, collected, standalone = [], [], [], []
        playlist = None

        def flush():
            if batch:
                current = list(batch)
                batch.clear()
                on_entries(current)

        def consume(line):
            nonlocal playlist
            if not line.startswith('{'):
                return False
            entry = json.loads(line)
            if entry.get('playlist_id') is not None or entry.get('playlist') is not None:
                if playlist is None:
                    playlist = {'_type': 'playlist', 'id': entry.get('playlist_id'),
                                'title': entry.get('playlist_title'),
                                'webpage_url': entry.get('playlist_webpage_url') or url}
                if on_entries:
                    if len(preview) < 2:
                        preview.append(entry)
                    batch.append(entry)
                    if len(batch) >= batch_size:
                        flush()
                else:
                    collected.append(entry)
            else:
                standalone.append(entry)
            return True
        try:
            self._run([*self._arguments(), '--flat-playlist', '--lazy-playlist', '--dump-json',
                       '--simulate', '--', url], consume)
        except DownloadCancelled:
            raise
        except Exception:
            if on_entries:
                flush()
            raise
        if on_entries:
            flush()
        if standalone and playlist is None:
            if len(standalone) != 1:
                raise YtDlpProcessError('yt-dlp returned ambiguous video metadata')
            return standalone[0], None
        entries = preview if on_entries else collected
        info = playlist or {'_type': 'playlist', 'webpage_url': url}
        info['entries'] = entries
        return info, entries
