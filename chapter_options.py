"""Chapter choices and bounded source-time ranges shared by embed and split modes."""
from dataclasses import dataclass
import math

CHAPTER_DEFAULTS = {'chapter_mode': 'none'}
EMBED_CHAPTER_EXTENSIONS = {'.mp3', '.m4a', '.mp4', '.mkv', '.mka'}
MAX_CHAPTERS = 1000


def validate_chapter_options(data):
    value = data.get('chapter_mode', 'none')
    if value not in ('none', 'embed', 'split'):
        raise ValueError('Invalid chapter setting.')
    return value


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    start: float
    end: float


@dataclass(frozen=True)
class ChapterPart:
    path: str
    start: float


@dataclass(frozen=True)
class ChapterBatch:
    parts: tuple[ChapterPart, ...]
    directory_name: str
    group_label: str = 'chapters'


def chapter_ranges(info, start, stop):
    raw = info.get('chapters') or []
    if not isinstance(raw, list) or not math.isfinite(start) or not math.isfinite(stop) or stop <= start:
        return []
    entries = []
    for number, item in enumerate(raw[:MAX_CHAPTERS], 1):
        if not isinstance(item, dict):
            continue
        try:
            begin = float(item.get('start_time'))
            if not math.isfinite(begin) or begin < 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        entries.append((begin, number, item))
    entries.sort(key=lambda item: (item[0], item[1]))
    result = []
    previous = None
    for index, (begin, number, item) in enumerate(entries):
        if begin == previous:
            continue
        previous = begin
        next_start = next((other[0] for other in entries[index + 1:] if other[0] > begin), stop)
        try:
            end = float(item.get('end_time') if item.get('end_time') is not None else next_start)
            if not math.isfinite(end) or end <= begin:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        end = min(end, next_start, stop)
        clipped_start = max(begin, start)
        if end - clipped_start >= .001:
            title = str(item.get('title') or f'Chapter {number}').replace('\x00', '')[:4096]
            result.append(Chapter(number, title, clipped_start, end))
    return result
