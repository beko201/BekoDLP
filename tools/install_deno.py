"""Install the verified portable Windows x64 Deno used by the application.

Run with the project's Python. The archive and digest are from the official
denoland/deno v2.9.8 GitHub release. No system PATH changes are made.
"""
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import urllib.request
import zipfile

VERSION = '2.9.8'
URL = f'https://github.com/denoland/deno/releases/download/v{VERSION}/deno-x86_64-pc-windows-msvc.zip'
SHA256 = 'cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf'


def main():
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('This portable installer supports Windows x64. Install Deno on PATH for other platforms.')
    target = Path(__file__).resolve().parent / 'runtime'
    target.mkdir(exist_ok=True)
    archive = target / 'deno-download.zip'
    staging = target / 'deno.exe.new'
    try:
        print(f'Downloading official Deno {VERSION}...', flush=True)
        request = urllib.request.Request(URL, headers={'User-Agent': 'BekoDLP-Setup'})
        with urllib.request.urlopen(request, timeout=60) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
        with archive.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != SHA256:
            raise RuntimeError('Deno archive checksum does not match the official release digest')
        with zipfile.ZipFile(archive) as bundle, bundle.open('deno.exe') as source, staging.open('wb') as output:
            shutil.copyfileobj(source, output)
        staging.replace(target / 'deno.exe')
        print('SHA-256 verified. Installed:', target / 'deno.exe', flush=True)
        subprocess.run([str(target / 'deno.exe'), '--version'], check=True)
    finally:
        archive.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
