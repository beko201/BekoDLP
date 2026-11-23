"""Optional embedded cover artwork without changing the selected media format."""
COVER_DEFAULTS = {'embed_cover': 'off'}
SUPPORTED_COVER_EXTENSIONS = {'.mp3', '.m4a', '.flac', '.mp4', '.mkv', '.mka'}


def validate_cover_options(data):
    value = data.get('embed_cover', 'off')
    if value not in ('off', 'on'):
        raise ValueError('Invalid cover artwork setting.')
    return value
