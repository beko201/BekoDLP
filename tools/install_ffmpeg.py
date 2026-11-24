"""Install verified portable Gyan FFmpeg/FFprobe without changing system PATH."""
import hashlib
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import time
import uuid
from urllib.request import Request, urlopen

VERSION = '9.1'
URL = f'https://github.com/GyanD/codexffmpeg/releases/download/{VERSION}/ffmpeg-{VERSION}-full_build.7z'
SHA256 = '1a86f2c8b51b4990ae9a0b0f8bd4f345f5c79389d293ac3fda128a58ec507b16'


def replace_directory(source, destination):
    # Windows scanners may briefly hold freshly executed tool files open.
    for attempt in range(30):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(.2)


def validate_members(names):
    expected = f'ffmpeg-{VERSION}-full_build'
    for name in names:
        path = PurePosixPath(name.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts):
            raise ValueError('The FFmpeg archive contains an unsafe path.')
        if not path.parts or path.parts[0] != expected:
            raise ValueError('The FFmpeg archive has an unexpected root directory.')


def main():
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('This portable installer supports Windows x64.')
    tar = shutil.which('tar.exe')
    if not tar:
        raise SystemExit('Windows tar.exe is required to extract the verified .7z archive.')
    runtime = (Path(__file__).resolve().parent / 'runtime').resolve()
    runtime.mkdir(exist_ok=True)
    target = runtime / 'ffmpeg'
    backup = runtime / 'ffmpeg-previous'
    if backup.exists():
        raise SystemExit('A previous FFmpeg backup exists. Inspect tools/runtime/ffmpeg-previous before retrying.')
    # Inherit workspace permissions even when installation needs network approval.
    staging = runtime / ('ffmpeg-install-' + uuid.uuid4().hex)
    staging.mkdir()
    archive = staging / 'ffmpeg.7z'
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW, 'stdin': subprocess.DEVNULL}
    try:
        print(f'Downloading official Gyan FFmpeg {VERSION} full build...', flush=True)
        request = Request(URL, headers={'User-Agent': 'BekoDLP-Setup'})
        with urlopen(request, timeout=60) as response, archive.open('wb') as stream:
            shutil.copyfileobj(response, stream)
        with archive.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != SHA256:
                raise ValueError('The FFmpeg archive does not match its official SHA-256 digest.')
        listing = subprocess.run([tar, '-tf', str(archive)], capture_output=True, text=True,
                                 check=True, timeout=60, **kwargs)
        validate_members(listing.stdout.splitlines())
        subprocess.run([tar, '-xf', str(archive), '-C', str(staging)], check=True, timeout=180, **kwargs)
        package = staging / f'ffmpeg-{VERSION}-full_build'
        for name in ('ffmpeg', 'ffprobe'):
            result = subprocess.run([str(package / 'bin' / f'{name}.exe'), '-version'],
                                    capture_output=True, text=True, check=True, timeout=15, **kwargs)
            if not result.stdout.startswith(f'{name} version {VERSION}-'):
                raise ValueError(f'The downloaded {name} reports an unexpected version.')
            if '--enable-nonfree' in result.stdout:
                raise ValueError('A nonfree FFmpeg build must not be distributed.')
        if target.exists():
            replace_directory(target, backup)
        try:
            replace_directory(package, target)
        except OSError:
            if backup.exists():
                replace_directory(backup, target)
            raise
        print('SHA-256 verified. Installed:', target, flush=True)
        if backup.exists():
            if not backup.resolve().is_relative_to(runtime):
                raise ValueError('The backup path is outside the runtime folder.')
            shutil.rmtree(backup)
    finally:
        if not staging.resolve().is_relative_to(runtime):
            raise ValueError('The staging path is outside the runtime folder.')
        shutil.rmtree(staging)


if __name__ == '__main__':
    main()
