"""Build the one-file Windows x64 EXE and a ZIP with collected license notices."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_info import APP_NAME, APP_VERSION, UPDATE_ASSET_NAME
from utils import which_executable


def build():
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build on Windows x64 with the documented build environment.')
    from PySide6.QtCore import QLibraryInfo, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    work = ROOT / 'build' / 'pyinstaller'
    work.mkdir(parents=True, exist_ok=True)
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    notices = ROOT / 'build/release-notices'
    manifest = json.loads((notices / 'inventory.json').read_text(encoding='utf-8'))
    if (manifest['application'], manifest['version']) != (APP_NAME, APP_VERSION):
        raise SystemExit('Recollect release notices for this application version.')
    ffmpeg, ffprobe = which_executable(['ffmpeg']), which_executable(['ffprobe'])
    if not ffmpeg or not ffprobe:
        raise SystemExit('Install FFmpeg/FFprobe before building.')
    catalog = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)) / 'qtbase_tr.qm'
    inputs = [catalog, ROOT / 'tools/runtime/deno.exe', ROOT / 'tools/apply_update.ps1',
              notices / 'THIRD_PARTY_NOTICES.md', ROOT / 'LICENSE']
    for path in inputs:
        if not path.is_file():
            raise SystemExit(f'Missing build input: {path}')

    # Derive the Windows file icon from the app's existing SVG without Pillow.
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer = QSvgRenderer(str(ROOT / 'assets/app.svg'))
    renderer.render(painter)
    painter.end()
    icon = work / 'app.ico'
    if not image.save(str(icon), 'ICO'):
        raise SystemExit('Could not generate the Windows icon.')

    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--noupx',
            '--name', Path(UPDATE_ASSET_NAME).stem, '--icon', str(icon),
            '--specpath', str(work), '--workpath', str(work / 'work'), '--distpath', str(output),
            '--runtime-hook', str(ROOT / 'tools/pyinstaller_runtime.py'),
            '--add-binary', f'{ffmpeg}:.', '--add-binary', f'{ffprobe}:.',
            '--add-binary', f'{ROOT / "tools/runtime/deno.exe"}:tools/runtime',
            '--add-data', f'{ROOT / "assets/app.svg"}:assets',
            '--add-data', f'{ROOT / "tools/apply_update.ps1"}:tools',
            '--add-data', f'{catalog}:PySide6/translations',
            '--add-data', f'{ROOT / "LICENSE"}:.', '--add-data', f'{notices}:licenses']
    for module in ('yt_dlp', 'yt_dlp_ejs', 'curl_cffi'):
        args.extend(['--collect-all', module])
    for package in ('yt-dlp', 'curl-cffi'):
        args.extend(['--recursive-copy-metadata', package])
    for package in ('yt-dlp-ejs', 'PySide6', 'psutil', 'Unidecode'):
        args.extend(['--copy-metadata', package])
    args.append(str(ROOT / 'main.py'))
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / 'build/pyinstaller-cache'))
    print('Building', UPDATE_ASSET_NAME, flush=True)
    subprocess.run(args, cwd=ROOT, env=env, check=True)
    executable = output / UPDATE_ASSET_NAME
    archive = output / f'{APP_NAME}-{APP_VERSION}-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(executable, executable.name)
        bundle.write(ROOT / 'LICENSE', 'LICENSE')
        for path in sorted(notices.rglob('*')):
            if path.is_file():
                bundle.write(path, 'licenses/' + path.relative_to(notices).as_posix())
        bundle.writestr('README.txt', f'{APP_NAME} {APP_VERSION}\n\n'
            f'Extract the ZIP and run {UPDATE_ASSET_NAME} from a writable folder.\n'
            'Python, FFmpeg/FFprobe and Deno are bundled.\n'
            'English is the default; choose Language > Turkce for Turkish.\n'
            'Source repository: https://github.com/beko201/BekoDLP\n'
            'License notices: LICENSE and licenses/.\n')
    print('EXE:', executable, flush=True)
    print('ZIP:', archive, flush=True)
    print('License review status:', manifest['status'], flush=True)


if __name__ == '__main__':
    build()
