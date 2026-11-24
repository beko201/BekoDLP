"""Immutable task options, event records and the nonblocking event queue."""
import os
from dataclasses import dataclass, asdict, field
from queue import Queue
from typing import Optional
from media_options import validate_output_options
from cookie_options import validate_cookie_options
from subtitle_options import validate_subtitle_options
from network_options import validate_network_options
from naming_templates import validate_naming_options
from cover_options import validate_cover_options
from chapter_options import validate_chapter_options
from range_options import validate_range_options
from source_formats import validate_source_options
from utils import validate_trim_range


LINK_RESOLVER_WORKERS = 3


DOWNLOAD_PROGRESS_INTERVAL = 0.1


DOWNLOAD_SPEED_WINDOW = 2.0


DOWNLOAD_SPEED_SAMPLES = 32


PROGRESS_STAGES = {
    "downloading": (0, 70),
    "processing": (70, 90),
    "trimming": (90, 95),
    "verification": (95, 97),
    "publishing": (97, 99),
}


@dataclass(frozen=True)
class TaskOptions:
    folder: str
    format: str = "MP4"
    quality: str = "1080p"
    codec_mode: str = "Original (Automatic)"
    mp3_bitrate: str = "192k"
    trim_start: str = ""
    trim_end: str = ""
    trim_download_mode: str = 'full'
    output_mode: str = "legacy"  # Preserve the existing headless API; the UI submits an explicit mode.
    container: str = "auto"
    video_codec: str = "original"
    audio_codec: str = "original"
    audio_bit_depth: str = "original"
    audio_bitrate: str = "192k"
    video_quality: str = "balanced"
    cookie_mode: str = 'none'
    cookie_file: str = ''
    cookie_browser: str = 'chrome'
    cookie_profile: str = ''
    subtitle_mode: str = 'none'
    subtitle_languages: str = 'tr,en'
    subtitle_auto: str = 'off'
    subtitle_format: str = 'srt'
    proxy_mode: str = 'system'
    proxy_url: str = ''
    download_rate: str = ''
    source_selection: str = 'auto'
    source_video_id: str = ''
    source_audio_id: str = ''
    filename_template: str = '{title}'
    subfolder_template: str = ''
    playlist_title: str = ''
    playlist_index: str = ''
    embed_cover: str = 'off'
    chapter_mode: str = 'none'
    trim_ranges: str = ''

    def __post_init__(self):
        validate_trim_range(self.trim_start, self.trim_end)
        validate_output_options(asdict(self))
        if self.trim_download_mode == 'section' and (self.trim_start or self.trim_end) and self.output_mode == 'legacy':
            raise ValueError('Section downloading requires an explicit video or audio output profile.')
        validate_cookie_options(asdict(self))
        validate_subtitle_options(asdict(self))
        validate_network_options(asdict(self))
        validate_source_options(asdict(self))
        validate_naming_options(asdict(self))
        validate_cover_options(asdict(self))
        validate_chapter_options(asdict(self))
        validate_range_options(asdict(self))
        if self.chapter_mode != 'none' and self.output_mode == 'legacy':
            raise ValueError('Chapter processing requires an explicit video or audio output profile.')
        if self.cookie_file:
            object.__setattr__(self, 'cookie_file', os.path.abspath(self.cookie_file))
        object.__setattr__(self, "folder", os.path.abspath(self.folder) if self.folder else "")
        object.__setattr__(self, "mp3_bitrate", self.mp3_bitrate or "192k")


@dataclass(frozen=True)
class TaskEvent:
    kind: str
    tid: Optional[str] = None
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TaskSummary:
    total: int
    pending: int
    failed: int
    retryable: int
    clearable: int
    progress: int
    action_revision: int


class TaskEventQueue(Queue):
    """Coalesce pending state notifications while preserving other events in order."""
    COALESCED_KINDS = frozenset({'task_updated', 'progress_changed', 'queue_changed'})

    def __init__(self):
        super().__init__()  # Unbounded: workers must never wait for the GUI.
        self._pending = {}
        self._wakeup = None

    def set_wakeup(self, callback):
        """Notify an optional consumer on empty-to-nonempty transitions."""
        with self.mutex:
            self._wakeup = callback

    def put(self, event, block=True, timeout=None):
        key = (event.kind, event.tid) if event.kind in self.COALESCED_KINDS else None
        with self.not_full:
            if key is not None and key in self._pending:
                self._pending[key][1] = event
                return
            wakeup = self._wakeup if not self._qsize() else None
            slot = [key, event]
            self._put(slot)
            if key is not None:
                self._pending[key] = slot
            self.unfinished_tasks += 1
            self.not_empty.notify()
        if wakeup is not None:
            wakeup()

    def _get(self):
        # Queue.get() holds the same mutex as put(), so a consumed key can
        # immediately be enqueued again without losing a concurrent update.
        key, event = super()._get()
        if key is not None:
            del self._pending[key]
        return event
