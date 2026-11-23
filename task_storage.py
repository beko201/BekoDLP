"""Compact internal task state; public task snapshots remain ordinary dictionaries."""
from collections.abc import MutableMapping
import threading


_MISSING = object()
_DELETED = object()


class TaskRecord(MutableMapping):
    """Share read-only option dictionaries while keeping changes private per task."""
    __slots__ = ('_options', '_state')

    def __init__(self, options, **state):
        self._options = options
        self._state = state

    def __getitem__(self, key):
        value = self._state.get(key, _MISSING)
        if value is _DELETED:
            raise KeyError(key)
        return self._options[key] if value is _MISSING else value

    def get(self, key, default=None):
        value = self._state.get(key, _MISSING)
        if value is _DELETED:
            return default
        return self._options.get(key, default) if value is _MISSING else value

    def __setitem__(self, key, value):
        self._state[key] = value

    def __delitem__(self, key):
        self[key]  # Preserve the normal KeyError for missing keys.
        if key in self._options:
            self._state[key] = _DELETED
        else:
            del self._state[key]

    def __iter__(self):
        for key in self._options:
            if self._state.get(key) is not _DELETED:
                yield key
        for key, value in self._state.items():
            if key not in self._options and value is not _DELETED:
                yield key

    def __len__(self):
        return sum(1 for _ in self)

    def snapshot(self):
        result = self._options.copy()
        for key, value in self._state.items():
            if value is _DELETED:
                result.pop(key, None)
            else:
                result[key] = value
        return result

    def frozen_copy(self):
        result = TaskRecord(self._options, **self._state)
        if 'warnings' in self._state:
            result._state['warnings'] = list(self._state['warnings'])
        return result


class CancellationEvent:
    """Event-compatible cancellation without a Condition for every queued task.

    Polling needs only a flag. Blocking waiters allocate a real Event once;
    the shared lock serializes that transition with set/clear.
    """
    __slots__ = ('_flag', '_event', '__dict__')
    _allocation_lock = threading.Lock()

    def __init__(self):
        self._flag = False
        self._event = None

    def is_set(self):
        return self._flag

    def set(self):
        with self._allocation_lock:
            self._flag = True
            if self._event is not None:
                self._event.set()

    def clear(self):
        with self._allocation_lock:
            self._flag = False
            if self._event is not None:
                self._event.clear()

    def wait(self, timeout=None):
        with self._allocation_lock:
            if self._event is None:
                self._event = threading.Event()
                if self._flag:
                    self._event.set()
            event = self._event
        return event.wait(timeout)
