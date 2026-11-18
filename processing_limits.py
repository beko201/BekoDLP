"""Live video-encoding admission and FFmpeg codec thread budgets."""
from collections import deque
from contextlib import contextmanager
import os
import threading

from settings import DEFAULTS, MAX_VIDEO_WORKERS, MAX_FFMPEG_THREADS


class ProcessingLimits:
    def __init__(self):
        self.condition = threading.Condition()
        self.video_workers = DEFAULTS["video_workers"]
        self.ffmpeg_threads = DEFAULTS["ffmpeg_threads"]
        self.active = 0
        self.waiters = deque()

    def configure(self, video_workers, ffmpeg_threads):
        if type(video_workers) is not int or not 1 <= video_workers <= MAX_VIDEO_WORKERS:
            raise ValueError(f"Video conversion count must be between 1 and {MAX_VIDEO_WORKERS}")
        if type(ffmpeg_threads) is not int or not 0 <= ffmpeg_threads <= MAX_FFMPEG_THREADS:
            raise ValueError(f"FFmpeg threads must be between 0 and {MAX_FFMPEG_THREADS}")
        with self.condition:
            self.video_workers = video_workers
            self.ffmpeg_threads = ffmpeg_threads
            self.condition.notify_all()

    def thread_budget(self):
        with self.condition:
            if self.ffmpeg_threads:
                return self.ffmpeg_threads
            cores = getattr(os, "process_cpu_count", os.cpu_count)() or 1
            return max(1, min(8, (cores - 1) // self.video_workers))

    @contextmanager
    def video_slot(self, check_cancelled, is_paused, on_wait):
        """Admit in FIFO order; reductions let active conversions finish normally."""
        ticket = object()
        acquired = False
        notified = False
        with self.condition:
            self.waiters.append(ticket)
        try:
            while True:
                check_cancelled()
                paused = is_paused()
                with self.condition:
                    if not paused and self.waiters[0] is ticket and self.active < self.video_workers:
                        self.waiters.popleft()
                        self.active += 1
                        acquired = True
                        self.condition.notify_all()
                        break
                if not notified:
                    on_wait()
                    notified = True
                with self.condition:
                    self.condition.wait(timeout=.1)
            check_cancelled()
            yield self.thread_budget()
        finally:
            with self.condition:
                if acquired:
                    self.active -= 1
                else:
                    self.waiters.remove(ticket)
                self.condition.notify_all()


def video_encoder(command):
    if "-vn" in command:
        return None
    for index, option in enumerate(command[:-1]):
        if option in ("-c:v", "-codec:v", "-vcodec", "-c", "-codec") or option.startswith(("-c:v:", "-codec:v:")):
            codec = command[index + 1]
            if codec != "copy":
                return codec
    return None


def budget_command(command, budget):
    """Bound codec/filter pools, including encoders with their own pool settings."""
    result = [command[0], "-filter_threads", str(budget), "-filter_complex_threads", str(budget)]
    for index, option in enumerate(command[1:], 1):
        if option == "-i":
            result += ["-threads", str(budget)]
        result.append(option)
    output = result.pop()
    result += ["-threads:v", str(budget), "-threads:a", str(budget)]
    codec = video_encoder(command)
    parameter = {"libx265": ("-x265-params", {"pools": str(budget), "frame-threads": "1"}),
                 "libsvtav1": ("-svtav1-params", {"lp": str(budget)})}.get(codec)
    if parameter:
        option, limits = parameter
        # Retain unrelated encoder parameters while overriding pool sizes.
        existing = {}
        if option in result:
            index = result.index(option)
            existing = dict(item.split("=", 1) for item in result[index + 1].split(":") if "=" in item)
            del result[index:index + 2]
        result += [option, ":".join(f"{key}={value}" for key, value in {**existing, **limits}.items())]
    return result + [output]
