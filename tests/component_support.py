"""Run the historical regression suite against the composed components.

The convenience attributes below belong only to the test harness. Production
GUI, manager and engine communicate through their explicit APIs and events.
"""
from queue import Queue
import threading
from unittest.mock import Mock

import download_engine
import task_manager
import utils


_MANAGER_FIELDS = {
    'tasks', 'tasks_lock', 'task_queue', 'task_counter', 'workers', 'retiring_workers',
    'worker_counter', 'worker_count', 'background_threads', 'paused', 'active_urls',
    'cancel_events', 'tid_to_workerid', 'stream_progress', 'download_speed_history',
    '_shutdown_event',
}
_ENGINE_FIELDS = {'ffmpeg', 'ffprobe', 'current_process_pids', 'reserved_output_paths'}
_ALIASES = {'_start_thread': 'start_thread'}


class _HarnessType(type):
    def __getattr__(cls, name):
        for component in (task_manager.TaskManager, download_engine.DownloadEngine):
            if hasattr(component, name):
                return getattr(component, name)
        raise AttributeError(name)


class TestApplication(metaclass=_HarnessType):
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        manager = task_manager.TaskManager()
        object.__setattr__(instance, 'manager', manager)
        object.__setattr__(instance, 'engine', manager.engine)
        return instance

    def __getattr__(self, name):
        if name in _MANAGER_FIELDS:
            return getattr(self.manager, name)
        if name in _ENGINE_FIELDS:
            return getattr(self.engine, name)
        if name in _ALIASES:
            return getattr(self.manager, _ALIASES[name])
        for component in (self.manager, self.engine):
            if hasattr(component, name):
                return getattr(component, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in {'manager', 'engine'}:
            object.__setattr__(self, name, value)
        elif name == '_closing':
            self.manager._closing = value
        elif name in _MANAGER_FIELDS:
            setattr(self.manager, name, value)
            if name == 'tasks_lock':
                self.manager._worker_slots = threading.Condition(value)
        elif name in _ENGINE_FIELDS:
            setattr(self.engine, name, value)
        elif name in _ALIASES:
            setattr(self.manager, _ALIASES[name], value)
        else:
            targets = [c for c in (self.manager, self.engine) if hasattr(c, name)]
            if targets:
                for component in targets:
                    setattr(component, name, value)
            else:
                object.__setattr__(self, name, value)


class _ComponentNamespace:
    def __init__(self):
        owners = {}
        for module in (utils, download_engine, task_manager):
            for name in vars(module):
                owners.setdefault(name, module)
        for name in ('psutil', 'run_cmd_capture', 'fetch_ytinfo_with_retry', 'ManagedYoutubeDL',
                     '_extract_urls_from_input'):
            owners[name] = download_engine
        object.__setattr__(self, '_owners', owners)
        object.__setattr__(self, 'ComponentHarness', TestApplication)

    def __getattr__(self, name):
        module = self._owners.get(name)
        if module is None:
            raise AttributeError(name)
        return getattr(module, name)

    def __setattr__(self, name, value):
        if name in self._owners:
            setattr(self._owners[name], name, value)
        else:
            object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name in self.__dict__:
            object.__delattr__(self, name)
        else:
            delattr(self._owners[name], name)


downloader = _ComponentNamespace()
