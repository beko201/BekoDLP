"""Named reusable output settings, separate from task and connection context."""
import unicodedata
import re
import uuid

from media_options import OUTPUT_DEFAULTS, validate_output_options
from subtitle_options import SUBTITLE_DEFAULTS, validate_subtitle_options
from naming_templates import NAMING_DEFAULTS, validate_naming_options
from cover_options import COVER_DEFAULTS, validate_cover_options
from chapter_options import CHAPTER_DEFAULTS, validate_chapter_options

MAX_PROFILES = 30
PROFILE_KEYS = frozenset((*OUTPUT_DEFAULTS, *SUBTITLE_DEFAULTS, *NAMING_DEFAULTS,
                          *COVER_DEFAULTS, *CHAPTER_DEFAULTS, 'quality'))


def profile_name(name):
    if not isinstance(name, str):
        raise ValueError('Enter a profile name with 1 to 64 characters.')
    name = unicodedata.normalize('NFC', name.strip())
    if not 1 <= len(name) <= 64 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError('Enter a profile name with 1 to 64 characters.')
    return name


def profile_options(options):
    from settings import DEFAULTS, QUALITIES
    if (not isinstance(options, dict) or not set(options) <= PROFILE_KEYS or
            any(not isinstance(value, str) or len(value) > 2048 or '\x00' in value for value in options.values())):
        raise ValueError('The saved profile contains invalid settings.')
    data = {key: options.get(key, DEFAULTS[key]) for key in PROFILE_KEYS}
    if data['output_mode'] not in ('video', 'audio') or data['quality'] not in QUALITIES:
        raise ValueError('The saved profile contains invalid settings.')
    validate_output_options(data)
    validate_subtitle_options(data)
    validate_naming_options(data)
    validate_cover_options(data)
    validate_chapter_options(data)
    return data


def capture_profile(values):
    return profile_options({key: value for key, value in values.items() if key in PROFILE_KEYS})


def validate_profiles(rows):
    if not isinstance(rows, list) or len(rows) > MAX_PROFILES:
        raise ValueError('At most 30 saved profiles are supported.')
    result, ids, names = [], set(), set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'id', 'name', 'options'} or
                not isinstance(row.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', row['id'])):
            raise ValueError('The saved profile record is invalid.')
        name = profile_name(row['name'])
        if row['id'] in ids or name.casefold() in names:
            raise ValueError('Saved profile names and identifiers must be unique.')
        ids.add(row['id'])
        names.add(name.casefold())
        result.append({'id': row['id'], 'name': name, 'options': profile_options(row['options'])})
    return result


def recover_profiles(rows):
    if not isinstance(rows, list):
        return [], True
    result, invalid = [], len(rows) > MAX_PROFILES
    for row in rows[:MAX_PROFILES]:
        try:
            result = validate_profiles([*result, row])
        except ValueError:
            invalid = True
    return result, invalid


def save_profile(rows, name, values):
    result = validate_profiles(rows)
    name, options = profile_name(name), capture_profile(values)
    for row in result:
        if row['name'].casefold() == name.casefold():
            row.update(name=name, options=options)
            return result, row['id']
    if len(result) >= MAX_PROFILES:
        raise ValueError('At most 30 saved profiles are supported.')
    key = uuid.uuid4().hex
    result.append({'id': key, 'name': name, 'options': options})
    return result, key


def builtin_profiles():
    base = profile_options({})
    return [
        {'id': 'builtin:music', 'name': 'Music (preset)', 'options': dict(base, output_mode='audio',
            container='mp3', audio_codec='mp3', audio_bitrate='320k', embed_cover='on')},
        {'id': 'builtin:archive', 'name': 'Original archive (preset)', 'options': dict(base, container='mkv',
            chapter_mode='embed', embed_cover='on', subtitle_mode='both')},
        {'id': 'builtin:small', 'name': 'Smaller file (preset)', 'options': dict(base, container='webm',
            video_codec='vp9', audio_codec='opus', audio_bitrate='128k', video_quality='small', quality='720p')},
    ]
