"""Bounded pending extractor metadata; callers serialize access with tasks_lock."""
from collections import OrderedDict
import sys
import time


MAX_RESOLVED_ENTRIES = 32
MAX_RESOLVED_BYTES = 16 * 1024 * 1024
RESOLVED_TTL_SECONDS = 300


def metadata_size(value, limit, *, max_nodes=100000):
    """Estimate retained Python payload size without serializing or copying it.

    Unknown objects are rejected: their referenced memory cannot be accounted
    for safely. Traversal, including cycles and repeated references, is bounded.
    This is an admission budget, not a hard bound on process RSS.
    """
    stack = [iter((value,))]
    seen = set()
    total = 0
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > max_nodes:
            return None
        total += sys.getsizeof(item)
        if total > limit:
            return None
        if isinstance(item, dict):
            stack.extend((iter(item.keys()), iter(item.values())))
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.append(iter(item))
        elif type(item) not in (str, bytes, int, float, bool, type(None)):
            return None
    return total


class ResolvedInfoCache:
    def __init__(self, *, max_entries=MAX_RESOLVED_ENTRIES, max_bytes=MAX_RESOLVED_BYTES,
                 ttl=RESOLVED_TTL_SECONDS, clock=None):
        if max_entries <= 0 or max_bytes <= 0 or ttl <= 0:
            raise ValueError('Cache limits must be positive')
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.clock = clock or time.monotonic
        self._entries = OrderedDict()
        self.size_bytes = 0

    def __len__(self):
        return len(self._entries)

    def prepare(self, info):
        # TaskManager does this outside tasks_lock to avoid holding it while
        # walking a potentially large extractor payload.
        return metadata_size(info, self.max_bytes)

    def prune(self):
        now = self.clock()
        while self._entries:
            tid, (_, _, expires) = next(iter(self._entries.items()))
            if expires > now:
                break
            self.pop(tid)

    def put(self, tid, info, size):
        self.prune()
        self.pop(tid)
        if size is None or size > self.max_bytes:
            return
        while self._entries and (len(self._entries) >= self.max_entries
                                 or self.size_bytes + size > self.max_bytes):
            self.pop(next(iter(self._entries)))
        self._entries[tid] = (info, size, self.clock() + self.ttl)
        self.size_bytes += size

    def pop(self, tid, default=None):
        entry = self._entries.pop(tid, None)
        if entry is None:
            return default
        info, size, expires = entry
        self.size_bytes -= size
        return info if expires > self.clock() else default

    def clear(self):
        self._entries.clear()
        self.size_bytes = 0
