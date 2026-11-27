"""Translate historical output inputs at API and persistence boundaries."""
from media_options import OUTPUT_DEFAULTS

LEGACY_OUTPUT_KEYS = frozenset({'format', 'codec_mode', 'mp3_bitrate'})
LEGACY_CODECS = ('Original (Automatic)', 'H.264 (Compatibility)')
LEGACY_BITRATES = ('128k', '192k', '256k', '320k')
HISTORICAL_CODEC_LABELS = {'Orijinal (Otomatik)': LEGACY_CODECS[0],
                           'H.264 (Uyumluluk)': LEGACY_CODECS[1]}


def normalize_output(data, *, preferences=False):
    """Return canonical output fields; an explicit modern mode takes precedence.

    Preferences additionally migrate removed UI choices. Invalid live profiles
    remain invalid and are rejected by the normal output validator.
    """
    result = dict(data)
    mode = result.get('output_mode')
    if mode in (None, '', 'legacy'):
        fmt = result.get('format')
        fmt = 'MP4' if fmt is None else fmt
        label = result.get('codec_mode')
        label = LEGACY_CODECS[0] if label is None else label
        label = HISTORICAL_CODEC_LABELS.get(label, label) if isinstance(label, str) else label
        if not preferences and fmt not in ('MP4', 'MP3'):
            raise ValueError('Unknown historical output format.')
        if not preferences and label not in LEGACY_CODECS:
            raise ValueError('Unknown historical codec mode.')
        for key, value in OUTPUT_DEFAULTS.items():
            result.setdefault(key, value)
        result['output_mode'] = 'video'
        # Historical tasks download the full source unless explicitly requested.
        result['trim_download_mode'] = data.get('trim_download_mode', OUTPUT_DEFAULTS['trim_download_mode'] if preferences else 'full')
        if fmt == 'MP3':
            result.update(output_mode='audio', container='mp3', audio_codec='mp3')
        elif label == LEGACY_CODECS[1]:
            result.update(output_mode='video', container='mp4', video_codec='h264', audio_codec='aac')
        bitrate = data.get('mp3_bitrate') or data.get('audio_bitrate') or '192k'
        result['audio_bitrate'] = (bitrate if not preferences or bitrate in LEGACY_BITRATES else '192k')
    else:
        for key, value in OUTPUT_DEFAULTS.items():
            result.setdefault(key, value)
    if preferences:
        if result.get('output_mode') == 'automatic':
            result['output_mode'] = 'video'
        if result.get('container') == 'aac':
            result['container'] = 'm4a'
        elif result.get('container') == 'ogg':
            audio = result.get('audio_codec')
            result['container'] = {'opus': 'opus', 'flac': 'flac'}.get(audio, 'auto') if isinstance(audio, str) else 'auto'
        if result.get('audio_codec') in ('pcm_s16le', 'pcm_s24le'):
            result['audio_bit_depth'] = data.get('audio_bit_depth', '16' if result['audio_codec'] == 'pcm_s16le' else '24')
            result['audio_codec'] = 'lpcm'
    return {key: value for key, value in result.items() if key not in LEGACY_OUTPUT_KEYS}


def output_aliases(data):
    """Read-only compatibility labels derived from a canonical profile."""
    return dict(format='MP3' if data['output_mode'] == 'audio' else 'MP4',
                codec_mode=LEGACY_CODECS[1] if data.get('video_codec') == 'h264' else LEGACY_CODECS[0],
                mp3_bitrate=data.get('audio_bitrate', '192k'))


def canonical_document(data):
    """Write one output vocabulary while retaining unrelated document fields."""
    return {key: value for key, value in data.items() if key not in LEGACY_OUTPUT_KEYS}


def legacy_preference_issues(data):
    if data.get('output_mode') not in (None, '', 'legacy'):
        return []
    issues = []
    choices = {'format': ('MP4', 'MP3'), 'codec_mode': LEGACY_CODECS, 'mp3_bitrate': LEGACY_BITRATES}
    for key, allowed in choices.items():
        if key not in data:
            continue
        value = data[key]
        if key == 'codec_mode' and isinstance(value, str):
            value = HISTORICAL_CODEC_LABELS.get(value, value)
        if not isinstance(value, str) or value not in allowed:
            issues.append(key)
    return issues


def normalize_changes(changes, current):
    """Translate legacy-only preference edits before merging canonical values."""
    result = dict(changes)
    issues = legacy_preference_issues(changes)
    if issues:
        raise ValueError('Invalid preferences: ' + ', '.join(issues))
    if 'output_mode' not in changes and any(key in changes for key in ('format', 'codec_mode')):
        historical = {**output_aliases(current), **changes}
        historical.pop('output_mode', None)
        converted = normalize_output(historical, preferences=True)
        result.update({key: converted[key] for key in OUTPUT_DEFAULTS})
    elif 'output_mode' not in changes and 'mp3_bitrate' in changes and 'audio_bitrate' not in changes:
        result['audio_bitrate'] = changes['mp3_bitrate']
    return result
