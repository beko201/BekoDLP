"""Serializable subtitle choices shared by the UI and task snapshots."""
import re

SUBTITLE_DEFAULTS = dict(subtitle_mode='none', subtitle_languages='tr,en', subtitle_auto='off', subtitle_format='srt')
SUBTITLE_MODES = ('none', 'files', 'embed', 'both')


def subtitle_selection(data=None):
    data = data or {}
    return {key: data.get(key, default) for key, default in SUBTITLE_DEFAULTS.items()}


def language_patterns(text):
    if not isinstance(text, str) or len(text) > 1024:
        raise ValueError('Invalid subtitle languages.')
    patterns = [part.strip() for part in text.split(',') if part.strip()]
    if not patterns or len(patterns) > 64 or any(
            part != 'all' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}(?:\.\*)?', part)
            for part in patterns):
        raise ValueError('Enter subtitle language codes such as tr,en, en.* or all.')
    return patterns


def validate_subtitle_options(data):
    selected = subtitle_selection(data)
    if (selected['subtitle_mode'] not in SUBTITLE_MODES or selected['subtitle_auto'] not in ('off', 'on')
            or selected['subtitle_format'] not in ('srt', 'vtt')):
        raise ValueError('Invalid subtitle settings.')
    if not isinstance(selected['subtitle_languages'], str):
        raise ValueError('Invalid subtitle languages.')
    if selected['subtitle_mode'] != 'none':
        language_patterns(selected['subtitle_languages'])
        audio = data.get('output_mode') == 'audio' or data.get('output_mode', 'legacy') == 'legacy' and data.get('format') == 'MP3'
        if audio and selected['subtitle_mode'] in ('embed', 'both'):
            raise ValueError('Audio downloads support separate subtitle files only.')
    return selected


def select_languages(info, selected):
    manual = info.get('subtitles') or {}
    automatic = info.get('automatic_captions') or {} if selected['subtitle_auto'] == 'on' else {}
    available = {lang for table in (manual, automatic) for lang, formats in table.items()
                 if formats and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', lang)
                 and lang not in ('live_chat', 'danmaku')}
    result = []
    for pattern in language_patterns(selected['subtitle_languages']):
        for lang in sorted(available):
            matches = pattern == 'all' or (lang.startswith(pattern[:-2]) if pattern.endswith('.*') else lang == pattern)
            if matches and lang not in result:
                result.append(lang)
    return result
