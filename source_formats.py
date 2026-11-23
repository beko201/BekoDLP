"""Explicit source choices, independent of output codecs and containers."""
import math
import re

from errors import MediaOperationError


def validate_source_options(task):
    mode = task.get('source_selection', 'auto')
    video, audio = task.get('source_video_id', ''), task.get('source_audio_id', '')
    if mode not in ('auto', 'manual') or any(not isinstance(value, str) or
            (value and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value)) for value in (video, audio)):
        raise ValueError('Invalid source format selection.')
    if mode == 'auto':
        if video or audio:
            raise ValueError('Automatic source selection cannot contain format IDs.')
        return
    output = task.get('output_mode')
    if output not in ('video', 'audio'):
        raise ValueError('Manual source selection requires a video or audio output profile.')
    if (output == 'video' and not video) or (output == 'audio' and (video or not audio)):
        raise ValueError('Choose a source format first.')
    if output == 'video' and task.get('audio_codec') == 'none' and audio:
        raise ValueError('This output profile does not include audio.')


def downloadable(fmt):
    media = any(fmt.get(key) not in (None, '', 'none') for key in ('vcodec', 'acodec'))
    # Some generic HTML5 extractors know the container but not its codecs.
    # "none" means absent; a missing codec means unknown until FFprobe inspects it.
    if not media and not (fmt.get('vcodec') == 'none' and fmt.get('acodec') == 'none'):
        media = fmt.get('ext') in {'mp4', 'webm', 'mkv', 'mov', 'avi', 'm4v', 'flv', 'ts', 'm2ts',
            'mpg', 'mpeg', 'ogv', 'wav', 'mp3', 'm4a', 'aac', 'opus', 'ogg', 'flac', 'aiff', 'mka', '3gp'}
    return (isinstance(fmt.get('format_id'), str) and
            bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', fmt['format_id'])) and
            bool(fmt.get('url')) and not fmt.get('has_drm') and
            media)


def selected_sources(info, task):
    """Return exact streams, or fail instead of silently choosing a replacement."""
    validate_source_options(task)
    if task.get('source_selection', 'auto') != 'manual':
        return None
    raw = info._media_info.info.get('formats') or []
    def details(stream):
        return next(fmt for fmt in raw if fmt.get('format_id') == stream.format_id)
    def find(format_id):
        matches = [fmt for fmt in raw if fmt.get('format_id') == format_id]
        if len(matches) != 1 or not downloadable(matches[0]):
            raise MediaOperationError('The selected source format is unavailable. Inspect the source formats again.')
        return next(stream for stream in info.formats if stream.format_id == format_id)
    if task['output_mode'] == 'audio':
        audio = find(task['source_audio_id'])
        if details(audio).get('acodec') == 'none':
            raise MediaOperationError('The selected source does not contain audio.')
        return (audio,)
    video = find(task['source_video_id'])
    if details(video).get('vcodec') == 'none':
        raise MediaOperationError('The selected source does not contain video.')
    audio_id = task.get('source_audio_id', '')
    if audio_id:
        audio = find(audio_id)
        if details(video).get('acodec') != 'none' or details(audio).get('vcodec') != 'none':
            raise MediaOperationError('Choose a video-only source and an audio-only source for separate streams.')
        return video, audio
    if task.get('audio_codec') != 'none' and details(video).get('acodec') == 'none':
        raise MediaOperationError('Choose an audio source for this video-only format.')
    return (video,)


def format_size(fmt, duration=0):
    size = fmt.get('filesize')
    approximate = not size
    size = size or fmt.get('filesize_approx')
    if not size:
        try:
            size = float(duration or 0) * float(fmt.get('tbr') or 0) * 125
        except (TypeError, ValueError, OverflowError):
            return '—'
    try:
        size = float(size)
        if not math.isfinite(size) or size <= 0:
            return '—'
    except (TypeError, ValueError, OverflowError):
        return '—'
    return ('≈ ' if approximate else '') + f'{size / 1024 ** 2:.2f} MiB'
