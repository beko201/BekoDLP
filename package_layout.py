"""Versioned, hash-listed Windows folder packages shared by build and updater."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from app_info import APP_NAME, APP_EXECUTABLE

MANIFEST_NAME = 'package-manifest.json'
LAYOUT = 'windows-x64-onedir'
MANAGED_ENTRIES = (APP_EXECUTABLE, '_internal', 'tools', 'licenses', 'LICENSE', MANIFEST_NAME)
MAX_FILES = 20000
MAX_UNPACKED_BYTES = 4 * 1024 ** 3
MAX_MANIFEST_BYTES = 4 * 1024 ** 2
REQUIRED_FILES = {APP_EXECUTABLE, '_internal/base_library.zip',
    '_internal/PySide6/QtCore.pyd', '_internal/PySide6/QtGui.pyd',
    '_internal/PySide6/QtWidgets.pyd', '_internal/PySide6/QtNetwork.pyd',
    '_internal/PySide6/plugins/platforms/qwindows.dll', 'tools/ffmpeg.exe',
    'tools/ffprobe.exe', 'tools/deno.exe', 'tools/apply_update.ps1',
    'licenses/THIRD_PARTY_NOTICES.md', 'LICENSE'}


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Invalid package path.')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in ('.', '..') for part in path.parts):
        raise ValueError('Package paths must stay inside the application folder.')
    for part in path.parts:
        if (part != part.rstrip(' .') or len(part) > 255 or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ValueError('The package contains an invalid Windows path.')
    if path.parts[0] not in MANAGED_ENTRIES or path.parts[0] == MANIFEST_NAME:
        raise ValueError('The package tries to manage user files or its own manifest hash.')
    if path.parts[0] in (APP_EXECUTABLE, 'LICENSE') and len(path.parts) != 1:
        raise ValueError('Invalid file/directory collision in package.')
    return path


def parse_manifest(content):
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError('The package manifest is too large.')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key in package manifest.')
            result[key] = value
        return result
    data = json.loads(content.decode('utf-8'), object_pairs_hook=unique_pairs)
    if (not isinstance(data, dict) or data.get('schema') != 1 or data.get('app') != APP_NAME
            or data.get('layout') != LAYOUT or data.get('health_protocol') != 1
            or data.get('managed_entries') != list(MANAGED_ENTRIES)):
        raise ValueError('The release is not a supported BekoDLP folder package.')
    rows = data.get('files')
    if not isinstance(data.get('version'), str) or not re.fullmatch(r'\d{1,6}(?:\.\d{1,6}){0,3}', data['version']):
        raise ValueError('Invalid package version.')
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise ValueError('The package file list is invalid.')
    keys, total = set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid package file record.')
        path = safe_relative(row.get('path'))
        key = str(path).casefold()
        if key in keys:
            raise ValueError('Duplicate or case-colliding package file.')
        keys.add(key)
        if type(row.get('size')) is not int or not 0 <= row['size'] <= MAX_UNPACKED_BYTES:
            raise ValueError('Invalid package file size.')
        if not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
            raise ValueError('Invalid package file digest.')
        total += row['size']
    for key in keys:
        if any(str(parent).casefold() in keys for parent in PurePosixPath(key).parents if str(parent) != '.'):
            raise ValueError('A package file is also used as a directory.')
    if total > MAX_UNPACKED_BYTES or not REQUIRED_FILES.issubset({row['path'] for row in rows}):
        raise ValueError('The package is incomplete or exceeds the unpacked size limit.')
    if not any(re.fullmatch(r'_internal/python3\d+\.dll', row['path']) for row in rows):
        raise ValueError('The package is missing its Python runtime.')
    return data


def create_manifest(folder, version):
    folder = Path(folder)
    rows = []
    for root in MANAGED_ENTRIES:
        if root == MANIFEST_NAME:
            continue
        source = folder / root
        if not source.exists():
            continue
        if source.is_symlink() or getattr(source.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Package roots must not contain links.')
        paths = source.rglob('*') if source.is_dir() else (source,)
        for path in paths:
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Package files must not contain links.')
            if path.is_file():
                rows.append({'path': path.relative_to(folder).as_posix(), 'size': path.stat().st_size,
                             'sha256': file_sha256(path)})
    manifest = {'schema': 1, 'app': APP_NAME, 'version': version, 'layout': LAYOUT,
                'health_protocol': 1, 'managed_entries': list(MANAGED_ENTRIES),
                'files': sorted(rows, key=lambda row: row['path'])}
    content = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    parse_manifest(content)
    (folder / MANIFEST_NAME).write_bytes(content)
    return manifest


def inspect_package(archive):
    if len(archive.infolist()) > MAX_FILES * 2:
        raise ValueError('Too many ZIP entries.')
    entries, case_names = {}, set()
    for info in archive.infolist():
        name = info.filename
        prefix = APP_NAME + '/'
        if not name.startswith(prefix):
            raise ValueError('The ZIP must contain exactly one BekoDLP application folder.')
        relative = name[len(prefix):].rstrip('/')
        if not relative:
            if not info.is_dir():
                raise ValueError('Invalid ZIP root entry.')
            continue
        if relative != MANIFEST_NAME:
            safe_relative(relative)
        if name.casefold() in case_names:
            raise ValueError('Duplicate or case-colliding ZIP entry.')
        case_names.add(name.casefold())
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or info.flag_bits & 1:
            raise ValueError('Links and encrypted ZIP entries are not supported.')
        if info.is_dir():
            continue
        if len(entries) >= MAX_FILES + 1:
            raise ValueError('Too many ZIP entries.')
        entries[relative] = info
    item = entries.get(MANIFEST_NAME)
    if item is None or item.file_size > MAX_MANIFEST_BYTES:
        raise ValueError('The ZIP is missing its package manifest.')
    content = archive.read(item)
    manifest = parse_manifest(content)
    expected = {row['path']: row for row in manifest['files']}
    if set(entries) != set(expected) | {MANIFEST_NAME}:
        raise ValueError('The ZIP file list does not match its manifest.')
    for name, row in expected.items():
        if entries[name].file_size != row['size']:
            raise ValueError('ZIP entry size does not match its manifest.')
    return manifest, entries, content


def extract_package(path, destination, *, expected_version=None, cancel_event=None):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Package extraction destination already exists.')
    with zipfile.ZipFile(path) as archive:
        manifest, entries, content = inspect_package(archive)
        if expected_version is not None:
            from updates import version_key
            if version_key(manifest.get('version')) != version_key(expected_version):
                raise ValueError('The package version does not match the GitHub release.')
        import shutil
        if shutil.disk_usage(destination.parent).free < sum(row['size'] for row in manifest['files']) + 32 * 1024 ** 2:
            raise OSError('There is not enough disk space to unpack the update.')
        destination.mkdir()
        for row in manifest['files']:
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError('Update verification cancelled.')
            output = destination.joinpath(*PurePosixPath(row['path']).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            sha, size = hashlib.sha256(), 0
            with archive.open(entries[row['path']]) as source, output.open('xb') as target:
                while chunk := source.read(256 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ValueError('Update verification cancelled.')
                    size += len(chunk)
                    if size > row['size']:
                        raise ValueError('ZIP entry exceeds its expected size.')
                    target.write(chunk)
                    sha.update(chunk)
            if size != row['size'] or sha.hexdigest() != row['sha256']:
                raise ValueError('A package file failed SHA-256 verification.')
        (destination / MANIFEST_NAME).write_bytes(content)
    return manifest
