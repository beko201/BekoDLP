"""Declared per-manager task state and the single lock protecting transitions."""
from _thread import LockType
from collections import deque
from dataclasses import dataclass, field
import math
from queue import Queue
import threading
import time
from typing import Any, Callable

from download_support import DEFAULT_WORKERS
from resolved_info_cache import ResolvedInfoCache
from task_storage import CancellationEvent, TaskRecord
from task_types import TaskEventQueue


@dataclass(slots=True)
class TaskRuntime:
    """Private to one manager; queue, progress and lifecycle share this owner."""

    tasks_lock: LockType = field(default_factory=threading.Lock)
    _worker_slots: threading.Condition = field(init=False)
    tasks: dict[str, TaskRecord | dict] = field(default_factory=dict)
    _summary_rows: dict[str, tuple] = field(default_factory=dict)
    _summary_counts: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    _progress_sum: float = 0.0
    _progress_correction: float = 0.0
    _action_revision: int = 0
    _queue_revision: int = 0
    _queue_rows: dict[str, tuple] = field(default_factory=dict)
    _restart_after_shutdown: set[str] = field(default_factory=set)
    _restored_queue_paused: bool = False
    _resolved_info: ResolvedInfoCache = field(default_factory=ResolvedInfoCache)
    task_counter: int = 0
    task_queue: Queue = field(default_factory=Queue)
    events: TaskEventQueue = field(default_factory=TaskEventQueue)
    _compact_task_events: bool = False
    workers: list[threading.Thread] = field(default_factory=list)
    retiring_workers: list[threading.Thread] = field(default_factory=list)
    worker_counter: int = 0
    worker_count: int = DEFAULT_WORKERS
    background_threads: set[threading.Thread] = field(default_factory=set)
    link_queue: Queue = field(default_factory=Queue)
    link_workers: list[threading.Thread] = field(default_factory=list)
    _pending_link_keys: set = field(default_factory=set)
    paused: bool = False
    active_urls: set[str] = field(default_factory=set)
    _active_url_owners: dict[str, str] = field(default_factory=dict)
    cancel_events: dict[str, CancellationEvent | threading.Event] = field(default_factory=dict)
    tid_to_workerid: dict[str, int] = field(default_factory=dict)
    stream_progress: dict[str, dict] = field(default_factory=dict)
    download_speed_history: dict[str, deque] = field(default_factory=dict)
    _download_progress_locks: dict[str, LockType] = field(default_factory=dict)
    _closing: bool = False
    _shutdown_event: threading.Event = field(default_factory=threading.Event)
    shutdown_grace_seconds: float = 5.0
    shutdown_force_seconds: float = 2.0
    _shutdown_deadline: float | None = None
    _shutdown_clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    _shutdown_phase: str | None = None
    _shutdown_force_thread: threading.Thread | None = None
    _shutdown_abandoned: bool = False
    engine: Any = None

    def __post_init__(self):
        self._worker_slots = threading.Condition(self.tasks_lock)
        self.shutdown_grace_seconds = float(self.shutdown_grace_seconds)
        self.shutdown_force_seconds = float(self.shutdown_force_seconds)
        if any(not math.isfinite(value) or value < 0 for value in (
                self.shutdown_grace_seconds, self.shutdown_force_seconds)):
            raise ValueError("Shutdown wait durations must be finite and non-negative")
