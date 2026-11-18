"""Bounded per-attempt media-probe reuse with file identity checks."""
from collections import OrderedDict
from copy import deepcopy
import math
import os
import threading

from errors import MediaOperationError


def probe_duration(probe):
    """Return a positive, finite format duration, or zero when unavailable."""
    try:
        value = float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if math.isfinite(value) and value > 0 else 0


def _path_key(path):
    value = os.fspath(path)
    if value.startswith("file:"):
        value = value[5:]
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def _signature(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class MediaProbeCache:
    MAX_ENTRIES = 64

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def read(self, path, executable, loader, *, fresh=False):
        filename = _path_key(path)
        key = (filename, os.fspath(executable))
        with self.lock:
            before = _signature(filename)
            entry = self.entries.get(key)
            if not fresh and before is not None and entry is not None and entry[0] == before:
                self.entries.move_to_end(key)
                return deepcopy(entry[1])
            self.entries.pop(key, None)
            result = loader()
            after = _signature(filename)
            if before is not None and after != before:
                raise MediaOperationError("The media file changed while FFprobe was inspecting it.")
            if before is not None and after is not None:
                self.entries[key] = (after, deepcopy(result))
                while len(self.entries) > self.MAX_ENTRIES:
                    self.entries.popitem(last=False)
            return result

    def invalidate(self, *paths):
        names = {_path_key(path) for path in paths}
        with self.lock:
            for key in list(self.entries):
                if key[0] in names:
                    del self.entries[key]

    def clear(self):
        with self.lock:
            self.entries.clear()
