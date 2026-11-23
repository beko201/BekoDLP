"""Install the checksum-verified official Windows x64 yt-dlp executable."""
import hashlib
from pathlib import Path
import platform
import subprocess
import urllib.request

VERSION = '2026.10.26'
URL = f'https://github.com/yt-dlp/yt-dlp/releases/download/{VERSION}/yt-dlp.exe'
SHA256 = 'f5389d3698ecde4ef06f4e6b1ce0294fe9ac1396228c8990b8d3416d6d177a7d'


def main():
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('This installer supports Windows x64. Install yt-dlp on PATH for other platforms.')
    folder = Path(__file__).resolve().parent / 'runtime'
    folder.mkdir(exist_ok=True)
    staging = folder / 'yt-dlp.exe.new'
    try:
        request = urllib.request.Request(URL, headers={'User-Agent': 'BekoDLP-Setup'})
        with urllib.request.urlopen(request, timeout=30) as response, staging.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        with staging.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != SHA256:
                raise RuntimeError('yt-dlp checksum does not match the official release digest')
        staging.replace(folder / 'yt-dlp.exe')
        subprocess.run([str(folder / 'yt-dlp.exe'), '--version'], check=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        print('SHA-256 verified. Installed:', folder / 'yt-dlp.exe', flush=True)
    finally:
        staging.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
