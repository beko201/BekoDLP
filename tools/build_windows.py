"""Build a portable Windows folder and a manifest-verified release ZIP."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_info import APP_NAME, APP_VERSION, APP_EXECUTABLE
from package_layout import MANIFEST_NAME, MANAGED_ENTRIES, create_manifest, parse_manifest
from utils import which_executable
from tools.prepare_release_notices import consolidate


def build():
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build on Windows x64 with the documented build environment.')
    from PySide6.QtCore import QLibraryInfo, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    work = ROOT / 'build' / 'pyinstaller-folder'
    work.mkdir(parents=True, exist_ok=True)
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    if not output.resolve().is_relative_to(ROOT) or not work.resolve().is_relative_to(ROOT):
        raise SystemExit('Build directories must remain inside the workspace.')
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

    staging = ROOT / 'build/package-staging'
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--contents-directory', '_internal', '--windowed', '--noupx',
            '--name', APP_NAME, '--icon', str(icon),
            '--specpath', str(work), '--workpath', str(work / 'work'), '--distpath', str(staging),
            '--runtime-hook', str(ROOT / 'tools/pyinstaller_runtime.py'),
            '--additional-hooks-dir', str(ROOT / 'tools/build_hooks'),
            '--exclude-module', 'yt_dlp.__pyinstaller',
            '--add-data', f'{ROOT / "assets/app.svg"}:assets',
            '--add-data', f'{catalog}:PySide6/translations']
    for module in ('yt_dlp', 'yt_dlp_ejs', 'curl_cffi'):
        args.extend(['--collect-all', module])
    for package in ('yt-dlp', 'curl-cffi'):
        args.extend(['--recursive-copy-metadata', package])
    for package in ('yt-dlp-ejs', 'PySide6', 'psutil', 'Unidecode'):
        args.extend(['--copy-metadata', package])
    args.append(str(ROOT / 'main.py'))
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / 'build/pyinstaller-cache'))
    print('Building portable folder:', APP_NAME, flush=True)
    subprocess.run(args, cwd=ROOT, env=env, check=True)
    collected = staging / APP_NAME
    tools = collected / 'tools'
    tools.mkdir(exist_ok=True)
    for source, name in ((ffmpeg, 'ffmpeg.exe'), (ffprobe, 'ffprobe.exe'),
                         (ROOT / 'tools/runtime/deno.exe', 'deno.exe'),
                         (ROOT / 'tools/apply_update.ps1', 'apply_update.ps1')):
        shutil.copy2(source, tools / name)
    consolidate(notices, collected / 'licenses')
    shutil.copy2(ROOT / 'LICENSE', collected / 'LICENSE')
    package = create_manifest(collected, APP_VERSION)
    destination = output / APP_NAME
    if destination.exists():
        # A rebuild must not delete portable preferences, logs or downloads.
        if not (destination / MANIFEST_NAME).is_file():
            raise SystemExit('Existing output has no manifest; move it aside before rebuilding.')
        parse_manifest((destination / MANIFEST_NAME).read_bytes())
        if destination.is_symlink() or destination.is_junction() or not destination.resolve().is_relative_to(output.resolve()):
            raise SystemExit('Refusing linked build output.')
        for name in MANAGED_ENTRIES:
            path = destination / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(destination.resolve()):
                raise SystemExit('Managed output escaped the application folder.')
        import psutil
        for process in psutil.process_iter(['exe']):
            if process.info['exe'] and Path(process.info['exe']).resolve() == destination / APP_EXECUTABLE:
                raise SystemExit('Close the packaged application before rebuilding.')
        for name in MANAGED_ENTRIES:
            path = destination / name
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        for name in MANAGED_ENTRIES:
            shutil.move(str(collected / name), str(destination / name))
    else:
        shutil.move(str(collected), str(destination))
    executable = destination / APP_EXECUTABLE
    legacy = output / 'BekoDLP-Windows-x64.exe'
    if legacy.exists():
        legacy_folder = ROOT / 'build/legacy-onefile'
        legacy_folder.mkdir(exist_ok=True)
        shutil.move(str(legacy), str(legacy_folder / legacy.name))
    archive = output / f'{APP_NAME}-{APP_VERSION}-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for name in [row['path'] for row in package['files']] + [MANIFEST_NAME]:
            bundle.write(destination / name, APP_NAME + '/' + name)
    print('EXE:', executable, flush=True)
    print('ZIP:', archive, flush=True)
    print('License review status:', manifest['status'], flush=True)


if __name__ == '__main__':
    build()
