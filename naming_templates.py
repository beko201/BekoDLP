"""Constrained output naming, using metadata rather than CLI output templates."""
from datetime import datetime
import os
from pathlib import Path
import re
from string import Formatter

from errors import MediaOperationError

NAMING_DEFAULTS = {'filename_template': '{title}', 'subfolder_template': ''}
FIELDS = {'title', 'channel', 'upload_date', 'playlist', 'playlist_index', 'id'}
FORMATTER = Formatter()


def safe_filename(value):
    # settings imports these defaults before utils finishes importing settings.
    from file_utils import safe_filename as sanitize
    return sanitize(value)


def _parts(template):
    try:
        parts = list(FORMATTER.parse(template))
    except ValueError:
        raise ValueError('Invalid naming template braces.') from None
    for literal, field, spec, conversion in parts:
        if field is not None and (field not in FIELDS or conversion or
                spec and not (field == 'playlist_index' and re.fullmatch(r'0?[1-6]d', spec))):
            raise ValueError('Unknown naming field or format. Use the fields shown below the templates.')
        if any(char in literal for char in '<>:"|?*'):
            raise ValueError('Naming templates contain invalid filename characters.')
    return parts


def validate_naming_options(data):
    name = data.get('filename_template', '{title}')
    folder = data.get('subfolder_template', '')
    if any(not isinstance(value, str) or len(value) > 512 or any(char in value for char in '\x00\r\n')
           for value in (name, folder)):
        raise ValueError('Invalid naming templates.')
    if not name.strip() or '/' in name or '\\' in name or name.strip() in ('.', '..'):
        raise ValueError('The filename template must be a name without folders or an extension field.')
    _parts(name)
    components = folder.replace('\\', '/').split('/') if folder else []
    if len(components) > 5 or any(not part.strip() or part.strip() in ('.', '..') for part in components):
        raise ValueError('The folder template must be a relative path without empty, dot or parent folders.')
    for component in components:
        _parts(component)
    title = data.get('playlist_title', '')
    index = data.get('playlist_index', '')
    if (not isinstance(title, str) or len(title) > 32768 or '\x00' in title or
            not isinstance(index, str) or index and (not index.isascii() or not index.isdecimal()
                or len(index) > 10 or not 1 <= int(index) <= 2147483647)):
        raise ValueError('Invalid playlist naming context.')


def uses_playlist_naming(data):
    return any('{playlist' in data.get(key, '') for key in NAMING_DEFAULTS)


def playlist_naming_context(entry, position=None, title=''):
    entry = entry if isinstance(entry, dict) else {}
    number = entry.get('playlist_index') or position
    text = str(number) if type(number) in (int, str) else ''
    if not text.isascii() or not text.isdecimal() or len(text) > 10 or not 1 <= int(text) <= 2147483647:
        text = ''
    playlist = entry.get('playlist_title') or entry.get('playlist') or title or ''
    return {'playlist_title': str(playlist).replace('\x00', '')[:32768], 'playlist_index': text}


def render_naming(data, metadata):
    validate_naming_options(data)
    date = metadata.get('upload_date') or metadata.get('release_date')
    try:
        if not re.fullmatch(r'[0-9]{8}', str(date)):
            raise ValueError('Unknown date')
        date = datetime.strptime(str(date), '%Y%m%d').strftime('%Y-%m-%d')
    except ValueError:
        date = 'Unknown date'
    index = data.get('playlist_index') or str(metadata.get('playlist_index') or '')
    number = int(index) if index.isascii() and index.isdecimal() and len(index) <= 10 else 0
    values = {
        'title': metadata.get('title') or 'Unnamed',
        'channel': metadata.get('channel') or metadata.get('uploader') or metadata.get('artist') or 'Unknown channel',
        'upload_date': date,
        'playlist': data.get('playlist_title') or metadata.get('playlist_title') or metadata.get('playlist') or 'Single videos',
        'playlist_index': number,
        'id': metadata.get('id') or 'unknown',
    }
    def component(template, limit):
        result = ''
        for literal, field, spec, _ in _parts(template):
            result += literal
            if field is not None:
                value = format(number, spec) if field == 'playlist_index' and spec else str(values[field])
                result += safe_filename(value)
        return safe_filename(result)[:limit].rstrip(' .') or 'video'
    folders = tuple(component(part, 80) for part in data.get('subfolder_template', '').replace('\\', '/').split('/') if part)
    name = component(data.get('filename_template', '{title}'), 200)
    if data.get('folder') and (folders or data.get('filename_template', '{title}') != '{title}'):
        # Reserve space for extension and collision suffixes used by publication.
        root_size = len(os.path.abspath(data['folder']).encode('utf-16-le')) // 2
        budget = min(200, 240 - root_size - sum(len(part) + 1 for part in folders) - 32)
        if budget < 12:
            raise ValueError('The output path is too long. Use a shorter folder template or download folder.')
        name = safe_filename(name[:budget])
    return folders, name


def ensure_naming_folder(root, components):
    base = Path(root).resolve()
    target = base
    for component in components:
        if component != safe_filename(component) or component in ('.', '..') or '/' in component or '\\' in component:
            raise MediaOperationError('The naming folder is invalid.')
        target = target / component
        if target.is_symlink() or target.is_junction() or not target.resolve().is_relative_to(base):
            raise MediaOperationError('The naming folder must stay inside the download folder without links.')
        target.mkdir(exist_ok=True)
        if target.is_symlink() or target.is_junction() or not target.resolve().is_relative_to(base):
            raise MediaOperationError('The naming folder must stay inside the download folder without links.')
    return str(target)
