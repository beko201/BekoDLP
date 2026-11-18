"""GitHub release validation and verified portable-folder Windows updates."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import secrets
import zipfile
from urllib.parse import quote, urlsplit

from app_info import APP_EXECUTABLE, UPDATE_ASSET_NAME, UPDATE_REPOSITORY
from package_layout import MANIFEST_NAME, parse_manifest, extract_package, file_sha256
from utils import log_warning

MAX_RELEASE_BYTES = 1024 * 1024
MAX_UPDATE_BYTES = 1024 * 1024 * 1024


def version_key(value):
    """Accept stable numeric release tags, treating 1.0 and v1.0.0 equally."""
    if not isinstance(value, str) or not re.fullmatch(r'v?\d{1,6}(?:\.\d{1,6}){0,3}', value):
        raise ValueError('Use a stable version tag such as v1.1 or v1.1.0.')
    parts = tuple(int(part) for part in value.removeprefix('v').split('.'))
    return parts + (0,) * (4 - len(parts))


def release_api_url(repository=UPDATE_REPOSITORY):
    if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('The GitHub update repository is invalid.')
    return f'https://api.github.com/repos/{repository}/releases/latest'


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: str


def parse_release(data, current_version, *, repository=UPDATE_REPOSITORY,
                  asset_name=UPDATE_ASSET_NAME):
    release_api_url(repository)
    if not isinstance(data, dict):
        raise ValueError('The GitHub release response is invalid.')
    if data.get('draft') or data.get('prerelease'):
        return None
    tag = data.get('tag_name')
    if version_key(tag) <= version_key(current_version):
        return None
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise ValueError('The release has no downloadable assets.')
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == asset_name]
    if not matches and asset_name == UPDATE_ASSET_NAME:
        asset_name = f'BekoDLP-{tag.removeprefix("v")}-Windows-x64.zip'
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == asset_name]
    if len(matches) != 1:
        raise ValueError(f'The release must contain exactly one {asset_name} asset.')
    asset = matches[0]
    size, digest, url = asset.get('size'), asset.get('digest'), asset.get('browser_download_url')
    if type(size) is not int or not 64 <= size <= MAX_UPDATE_BYTES or asset.get('state') != 'uploaded':
        raise ValueError('The release package size or upload state is invalid.')
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise ValueError('GitHub did not provide a SHA-256 digest for the package.')
    expected = f'https://github.com/{repository}/releases/download/{quote(tag, safe="")}/{quote(asset_name, safe="")}'
    if url != expected or urlsplit(url).scheme != 'https':
        raise ValueError('The update package URL does not belong to the configured release.')
    return Release(tag.removeprefix('v'), url, size, digest[7:].lower())


def can_self_update():
    """Old one-file bundles require a one-time manual migration."""
    bundle = getattr(sys, '_MEIPASS', None)
    target = Path(sys.executable).resolve()
    if not (sys.platform == 'win32' and getattr(sys, 'frozen', False) and bundle
            and target.name == APP_EXECUTABLE and Path(bundle).resolve() == target.parent / '_internal'):
        return False
    try:
        parse_manifest((target.parent / MANIFEST_NAME).read_bytes())
        return True
    except (OSError, ValueError, TypeError):
        return False


class StagedUpdate:
    def __init__(self, release, target):
        self.release = release
        self.target = Path(target).resolve(strict=True)
        if self.target.suffix.lower() != '.exe' or not self.target.is_file():
            raise ValueError('The update target must be an existing executable.')
        if shutil.disk_usage(self.target.parent).free < release.size + self.target.stat().st_size + 32 * 1024 * 1024:
            raise OSError('There is not enough free disk space for the update.')
        self.folder = Path(tempfile.mkdtemp(prefix='.bekodlp-update-', dir=self.target.parent.parent))
        self.path = self.folder / 'package.zip'
        self.payload = self.folder / 'payload'
        self.cancel_event = threading.Event()
        self._file = None
        self.size = 0
        self._hash = hashlib.sha256()
        self.verified = False
        try:
            self._file = self.path.open('xb', buffering=0)
        except OSError:
            self.discard()
            raise

    def write(self, chunk):
        if self._file is None or self.size + len(chunk) > self.release.size:
            raise ValueError('The update exceeds its advertised size.')
        if self._file.write(chunk) != len(chunk):
            raise OSError('The update could not be fully written to disk.')
        self._hash.update(chunk)
        self.size += len(chunk)

    def finish(self):
        if self._file is None:
            raise ValueError('The update download is already closed.')
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None
        if self.size != self.release.size or self._hash.hexdigest() != self.release.sha256:
            raise ValueError('The downloaded update failed size or SHA-256 verification.')
        try:
            self.manifest = extract_package(self.path, self.payload, expected_version=self.release.version,
                                            cancel_event=self.cancel_event)
        except zipfile.BadZipFile as error:
            raise ValueError('The update is not a valid ZIP package.') from error
        executable = self.payload / APP_EXECUTABLE
        with executable.open('rb') as stream:
            header = stream.read(64)
            if header[:2] != b'MZ':
                raise ValueError('The update is not a Windows executable.')
            offset = int.from_bytes(header[60:64], 'little')
            if offset < 64 or offset + 6 > executable.stat().st_size:
                raise ValueError('The update has an invalid executable header.')
            stream.seek(offset)
            if stream.read(6) != b'PE\x00\x00\x64\x86':
                raise ValueError('The update is not a Windows x64 executable.')
        self.verified = True

    def discard(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        # Never recurse into an installation or a caller-provided folder.
        if (self.folder.parent != self.target.parent.parent
                or not self.folder.name.startswith('.bekodlp-update-') or self.folder.is_symlink()):
            raise ValueError('Invalid update cleanup folder.')
        try:
            if self.folder.exists():
                shutil.rmtree(self.folder)
        except OSError as error:
            log_warning('Could not remove update staging folder', error,
                        event='update_cleanup_failed', component='updater')


def launch_update(stage):
    """Start a hidden helper; it waits for clean application exit before swapping."""
    if not can_self_update() or not stage.verified:
        raise ValueError('Only a verified Windows folder build can install updates.')
    if stage.target != Path(sys.executable).resolve(strict=True):
        raise ValueError('The update target is not the running application.')
    helper = stage.target.parent / 'tools' / 'apply_update.ps1'
    script = stage.folder / 'apply.ps1'
    shutil.copyfile(helper, script)
    with stage.target.open('rb') as stream:
        original_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    config = stage.folder / 'update.json'
    config.write_text(json.dumps({'pid': os.getpid(), 'target': str(stage.target),
                                 'sha256': stage.release.sha256, 'original_sha256': original_hash,
                                 'manifest_sha256': file_sha256(stage.payload / MANIFEST_NAME),
                                 'health_token': secrets.token_hex(32)}),
                      encoding='utf-8')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    return subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(script), '-Config', str(config)],
                            cwd=str(stage.folder.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                            stdin=subprocess.DEVNULL)
