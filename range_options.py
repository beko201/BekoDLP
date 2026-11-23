"""Validated, ordered time ranges stored as text in task options."""
import re

RANGE_DEFAULTS = {'trim_ranges': ''}
MAX_RANGES = 100


def parse_ranges(text):
    if not isinstance(text, str) or len(text) > 32768 or '\x00' in text:
        raise ValueError('Invalid time ranges.')
    lines = [line.strip() for line in re.split(r'[;\n]', text) if line.strip()]
    if len(lines) > MAX_RANGES:
        raise ValueError('At most 100 time ranges are supported.')
    # Import at call time: utils imports preference defaults from settings.
    from utils import validate_trim_range
    result = []
    for number, line in enumerate(lines, 1):
        match = re.fullmatch(r'([0-9:.]+)\s*[-–]\s*([0-9:.]+)', line)
        if not match:
            raise ValueError(f'Range {number}: use start - end (for example, 0:30 - 1:10).')
        start, end = validate_trim_range(*match.groups())
        result.append((start, end))
    return tuple(result)


def validate_range_options(data):
    ranges = parse_ranges(data.get('trim_ranges', ''))
    if ranges:
        if data.get('trim_start') or data.get('trim_end'):
            raise ValueError('Use either a single trim range or multiple time ranges.')
        if data.get('output_mode') == 'legacy':
            raise ValueError('Multiple time ranges require a video or audio output profile.')
        if data.get('chapter_mode', 'none') == 'split':
            raise ValueError('Use either multiple time ranges or separate chapter files. Chapter embedding can be combined with ranges.')
    return ranges
