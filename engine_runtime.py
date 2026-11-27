"""Declared per-engine resources; no component creates implicit engine fields."""
from _thread import LockType
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading

from media_probe import MediaProbeCache
from network_limits import NetworkLimits
from processing_limits import ProcessingLimits


@dataclass(slots=True)
class EngineRuntime:
    """One engine owns these locks, tool paths, caches and output reservations."""

    ffmpeg: str | None
    ffprobe: str | None
    current_process_pids: dict[int, int] = field(default_factory=dict)
    managed_processes: set = field(default_factory=set)
    _force_stopping: bool = False
    reserved_output_paths: dict[str, str] = field(default_factory=dict)
    resource_lock: LockType = field(default_factory=threading.Lock)
    _subtitle_context: dict[str, dict] = field(default_factory=dict)
    _output_naming: dict[str, tuple] = field(default_factory=dict)
    _rate_divisor: ContextVar[int] = field(default_factory=lambda: ContextVar('download_rate_divisor', default=1))
    _encoder_lock: LockType = field(default_factory=threading.Lock)
    _encoders: frozenset[str] | None = None
    processing_limits: ProcessingLimits = field(default_factory=ProcessingLimits)
    network_limits: NetworkLimits = field(default_factory=NetworkLimits)
    _probe_cache: ContextVar[MediaProbeCache | None] = field(default_factory=lambda: ContextVar('media_probes', default=None))
