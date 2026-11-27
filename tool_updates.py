"""Opt-in Windows x64 tool updates with verified staging and an atomic pointer."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import threading
from urllib.parse import quote, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import uuid
import zipfile

import app_paths
from errors import DownloadCancelled
from process_utils import NO_WINDOW
from runtime_tools import which_executable, portable_deno_path

SPECS = {'ytdlp': ('yt-dlp/yt-dlp', 'yt-dlp.exe', ('yt-dlp.exe',)),
         'deno': ('denoland/deno', 'deno-x86_64-pc-windows-msvc.zip', ('deno.exe',)),
         'ffmpeg': ('GyanD/codexffmpeg', None, ('ffmpeg.exe', 'ffprobe.exe'))}
MAX_DOWNLOAD = 512 * 1024 ** 2
MAX_UNPACKED = 1024 ** 3
TRUSTED_HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
_COMMIT_LOCK = threading.Lock()


def supported():
    return os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64')


def check_cancel(cancel):
    if cancel.is_set():
        raise DownloadCancelled()


def trusted_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in TRUSTED_HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('The tool download URL is not trusted.')
    return url


class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_url(url, *, cancel, limit, output=None, progress=None, expected=None,
             accept='application/octet-stream'):
    check_cancel(cancel)
    trusted_url(url)
    request = Request(url, headers={'User-Agent': 'BekoDLP-ToolUpdater', 'Accept': accept})
    count, chunks, digest = 0, [], hashlib.sha256()
    with build_opener(SecureRedirect()).open(request, timeout=20) as response:
        trusted_url(response.geturl())
        length = response.headers.get('Content-Length')
        if length and (int(length) > limit or expected is not None and int(length) != expected):
            raise ValueError('The tool download size is invalid.')
        while True:
            check_cancel(cancel)
            block = response.read(256 * 1024)
            if not block:
                break
            count += len(block)
            if count > limit or expected is not None and count > expected:
                raise ValueError('The tool download exceeds the size limit.')
            digest.update(block)
            if output is None:
                chunks.append(block)
            else:
                output.write(block)
            if progress:
                progress(count, expected or 0)
    check_cancel(cancel)
    if expected is not None and count != expected:
        raise ValueError('The tool download is incomplete.')
    return digest.hexdigest() if output is not None else b''.join(chunks)


@dataclass(frozen=True)
class ToolRelease:
    component: str
    version: str
    url: str
    size: int
    sha256: str
    asset: str


def parse_release(component, data):
    repository, asset, _ = SPECS[component]
    if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
        raise ValueError('No stable tool release is available.')
    tag = data.get('tag_name')
    if not isinstance(tag, str) or not re.fullmatch(r'v?\d{1,6}(?:\.\d{1,6}){1,3}', tag):
        raise ValueError('The tool release version is invalid.')
    version = tag.removeprefix('v')
    asset = asset or f'ffmpeg-{version}-full_build.zip'
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise ValueError('The tool release has no assets.')
    matches = [item for item in assets if isinstance(item, dict) and item.get('name') == asset]
    if len(matches) != 1:
        raise ValueError('The required Windows x64 tool package is unavailable.')
    item = matches[0]
    digest, size = item.get('digest'), item.get('size')
    url = f'https://github.com/{repository}/releases/download/{quote(tag, safe="")}/{asset}'
    if (item.get('browser_download_url') != url or item.get('state') != 'uploaded'
            or type(size) is not int or not 64 <= size <= MAX_DOWNLOAD
            or not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest)):
        raise ValueError('The tool package URL, size or SHA-256 digest is invalid.')
    return ToolRelease(component, version, url, size, digest[7:].lower(), asset)


def latest_release(component, cancel):
    repository = SPECS[component][0]
    content = read_url(f'https://api.github.com/repos/{repository}/releases/latest', cancel=cancel,
                       limit=2 * 1024 ** 2, accept='application/vnd.github+json')
    return parse_release(component, json.loads(content.decode('utf-8')))


def run_version(path, component):
    flags = ['--ignore-config', '--version'] if component == 'ytdlp' else ['--version'] if component == 'deno' else ['-version']
    result = subprocess.run([str(path), *flags], capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=15, check=True, **NO_WINDOW)
    text = result.stdout[:65536]
    prefix = '' if component == 'ytdlp' else 'deno ' if component == 'deno' else path.stem + ' version '
    match = re.match(re.escape(prefix) + r'(\d+(?:\.\d+){1,3})(?=[\s+\-]|$)', text)
    if not match or '--enable-nonfree' in text:
        raise ValueError('The downloaded tool reports an unexpected version or configuration.')
    return match[1]


def installed_version(component):
    name = 'yt-dlp' if component == 'ytdlp' else component
    path = (portable_deno_path() or shutil.which('deno')) if component == 'deno' else which_executable([name])
    if not path:
        return 'Not installed'
    try:
        return run_version(Path(path), component)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'Unknown'


def is_newer(latest, current):
    def key(value):
        return tuple(int(part) for part in value.split('.')) + (0,) * (4 - len(value.split('.')))
    return current in ('Not installed', 'Unknown') or key(latest) > key(current)


def extract_payload(archive, destination, release, cancel):
    expected = SPECS[release.component][2]
    with zipfile.ZipFile(archive) as bundle:
        members, total, seen = {}, 0, set()
        if len(bundle.infolist()) > 10000:
            raise ValueError('The tool archive has too many files.')
        for item in bundle.infolist():
            path = PurePosixPath(item.filename.replace('\\', '/'))
            if (path.is_absolute() or '..' in path.parts or any(':' in p for p in path.parts)
                    or stat.S_ISLNK(item.external_attr >> 16) or item.filename.casefold() in seen):
                raise ValueError('The tool archive contains an unsafe or duplicate path.')
            seen.add(item.filename.casefold())
            total += item.file_size
            if total > MAX_UNPACKED or item.flag_bits & 1:
                raise ValueError('The tool archive exceeds the extraction limit or is encrypted.')
            if release.component == 'ffmpeg':
                allowed = path.as_posix() in [f'ffmpeg-{release.version}-full_build/bin/{name}' for name in expected]
            else:
                allowed = item.filename in expected
            if allowed:
                if path.name in members:
                    raise ValueError('The tool archive contains duplicate executables.')
                members[path.name] = item
        if set(members) != set(expected):
            raise ValueError('The tool archive is missing required executables.')
        for name, item in members.items():
            with bundle.open(item) as source, (destination / name).open('xb') as output:
                count = 0
                while block := source.read(256 * 1024):
                    check_cancel(cancel)
                    count += len(block)
                    if count > 512 * 1024 ** 2:
                        raise ValueError('The extracted tool exceeds the size limit.')
                    output.write(block)


def validate_payload(folder, release, cancel):
    for name in SPECS[release.component][2]:
        check_cancel(cancel)
        path = folder / name
        with path.open('rb') as binary:
            header = binary.read(64)
            if len(header) != 64 or header[:2] != b'MZ':
                raise ValueError('The downloaded tool is not a Windows executable.')
            offset = int.from_bytes(header[60:64], 'little')
            if not 64 <= offset <= min(path.stat().st_size - 6, 1024 * 1024):
                raise ValueError('The downloaded tool has an invalid executable header.')
            binary.seek(offset)
            if binary.read(6) != b'PE\x00\x00\x64\x86':
                raise ValueError('The downloaded tool is not a Windows x64 executable.')
        if run_version(path, release.component) != release.version:
            raise ValueError('The downloaded tool version does not match the release.')
    check_cancel(cancel)


def _safe_root():
    root = app_paths.tool_updates_root()
    if root.is_symlink() or root.is_junction():
        raise ValueError('The tool update folder must not be a link.')
    root.mkdir(exist_ok=True)
    return root


class StagedToolUpdate:
    def __init__(self, release):
        self.release = release
        self.cancel = threading.Event()
        self.folder = _safe_root() / uuid.uuid4().hex
        self.folder.mkdir()
        self.ready = False
        self.activated = False
        self.hashes = {}

    def prepare(self, progress=None):
        release = self.release
        if shutil.disk_usage(self.folder).free < release.size + MAX_UNPACKED + 32 * 1024 ** 2:
            raise OSError('There is not enough free disk space for the tool update.')
        archive = self.folder / 'download.bin'
        try:
            with archive.open('xb') as output:
                digest = read_url(release.url, cancel=self.cancel, limit=MAX_DOWNLOAD, expected=release.size,
                                  output=output, progress=progress)
            if digest != release.sha256:
                raise ValueError('The tool package SHA-256 verification failed.')
            if release.component == 'ytdlp':
                archive.replace(self.folder / 'yt-dlp.exe')
            else:
                extract_payload(archive, self.folder, release, self.cancel)
            validate_payload(self.folder, release, self.cancel)
            for name in SPECS[release.component][2]:
                with (self.folder / name).open('rb') as binary:
                    self.hashes[name] = hashlib.file_digest(binary, 'sha256').hexdigest()
            (self.folder / 'provenance.json').write_text(json.dumps({**release.__dict__,
                'files': self.hashes}, indent=2), encoding='utf-8')
            self.ready = True
        finally:
            archive.unlink(missing_ok=True)

    def activate(self):
        check_cancel(self.cancel)
        if not self.ready or self.activated:
            raise ValueError('The tool update is not ready to install.')
        if set(self.hashes) != set(SPECS[self.release.component][2]):
            raise ValueError('The verified tool file list is incomplete.')
        for name, expected in self.hashes.items():
            check_cancel(self.cancel)
            with (self.folder / name).open('rb') as binary:
                if hashlib.file_digest(binary, 'sha256').hexdigest() != expected:
                    raise ValueError('The staged tool changed after verification.')
        root = _safe_root()
        with _COMMIT_LOCK:
            pointer = root / 'active.json'
            if pointer.exists() and pointer.stat().st_size > 16384:
                raise ValueError('The installed tool selection is invalid.')
            data = json.loads(pointer.read_text(encoding='utf-8')) if pointer.exists() else {}
            if not isinstance(data, dict):
                raise ValueError('The installed tool selection is invalid.')
            data[self.release.component] = self.folder.name
            temporary = root / ('.active-' + uuid.uuid4().hex)
            try:
                with temporary.open('x', encoding='utf-8') as output:
                    json.dump(data, output)
                    output.flush()
                    os.fsync(output.fileno())
                check_cancel(self.cancel)
                temporary.replace(pointer)
                self.activated = True
            finally:
                temporary.unlink(missing_ok=True)

    def discard(self):
        if not self.activated and self.folder.exists():
            root = app_paths.tool_updates_root().resolve()
            if (self.folder.is_symlink() or self.folder.is_junction()
                    or self.folder.resolve().parent != root or not re.fullmatch(r'[a-f0-9]{32}', self.folder.name)):
                raise ValueError('The tool staging folder is outside the update folder.')
            shutil.rmtree(self.folder)
