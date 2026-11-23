"""Versioned queue snapshots, with bounded reads, atomic writes and recovery."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import urlsplit

from settings import QUALITIES, CODECS, _unique_object, _reject_constant, _finite_float

SCHEMA_VERSION = 1
MAX_QUEUE_BYTES = 16 * 1024 * 1024
MAX_TASKS = 20000


class UnsupportedQueueVersion(ValueError):
    pass


def task_state(task, restarting=(), tid=None):
    status = task['status']
    if status.startswith('Completed'):
        return 'completed'
    if task.get('id', tid) in restarting:
        return 'pending'
    if status.startswith('Error'):
        return 'failed'
    if status.startswith('Cancel'):
        return 'cancelled'
    return 'pending'


def validate_document(document):
    # Import at the boundary, avoiding an import cycle with the task manager.
    from task_manager import TaskOptions
    if not isinstance(document, dict):
        raise ValueError('The saved queue must be a JSON object.')
    if type(document.get('schema_version')) is not int or document['schema_version'] != SCHEMA_VERSION:
        raise UnsupportedQueueVersion('Unsupported saved queue version.')
    rows = document.get('tasks')
    if not isinstance(rows, list) or len(rows) > MAX_TASKS:
        raise ValueError('The saved queue has an invalid task list.')
    result, ids, pending_urls = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid saved task.')
        tid = row.get('id')
        if not isinstance(tid, str) or not re.fullmatch(r'T[1-9][0-9]{0,11}', tid) or tid in ids:
            raise ValueError('Invalid or duplicate saved task ID.')
        ids.add(tid)
        url = row.get('url')
        if not isinstance(url, str) or len(url) > 32768 or '\x00' in url:
            raise ValueError('Invalid saved URL.')
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Saved task URLs must use HTTP or HTTPS.')
        state = row.get('state')
        if state not in ('pending', 'completed', 'failed', 'cancelled'):
            raise ValueError('Unknown saved task state.')
        if state == 'pending':
            if url in pending_urls:
                raise ValueError('The saved queue contains duplicate pending URLs.')
            pending_urls.add(url)
        options = row.get('options')
        if (not isinstance(options, dict) or 'folder' not in options
                or any(not isinstance(value, str) or '\x00' in value or len(value) > 32768
                       for value in options.values())):
            raise ValueError('Invalid saved task options.')
        try:
            options = asdict(TaskOptions(**options))
        except TypeError as error:
            raise ValueError('Unknown saved task options.') from error
        if (options['quality'] not in QUALITIES or options['format'] not in ('MP4', 'MP3')
                or options['codec_mode'] not in CODECS):
            raise ValueError('Invalid saved download profile.')
        title = row.get('title', '(resolving title...)')
        status = row.get('status', '')
        output = row.get('output_path')
        if any(not isinstance(value, str) or len(value) > 65536 or '\x00' in value
               for value in (title, status)):
            raise ValueError('Invalid saved title or status.')
        if output is not None and (not isinstance(output, str) or len(output) > 32768 or '\x00' in output):
            raise ValueError('Invalid saved output path.')
        warnings = row.get('warnings', [])
        if (not isinstance(warnings, list) or len(warnings) > 100
                or any(not isinstance(w, str) or len(w) > 65536 for w in warnings)):
            raise ValueError('Invalid saved task warnings.')
        attempt = row.get('attempt', 0)
        if type(attempt) is not int or not 0 <= attempt <= 10 ** 12:
            raise ValueError('Invalid saved attempt count.')
        result.append(dict(id=tid, url=url, options=options, state=state, title=title,
                           status=status, output_path=output, warnings=list(warnings), attempt=attempt))
    return dict(schema_version=SCHEMA_VERSION, tasks=result)


class QueueStore:
    def __init__(self, path):
        self.path = Path(path)
        self.backup = Path(str(path) + '.bak')
        self.writable = True
        self._recovered = False

    def _read(self, path):
        with path.open('rb') as stream:
            content = stream.read(MAX_QUEUE_BYTES + 1)
        if len(content) > MAX_QUEUE_BYTES:
            raise ValueError('The saved queue exceeds the 16 MiB limit.')
        try:
            return validate_document(json.loads(content.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                                               parse_constant=_reject_constant, parse_float=_finite_float))
        except RecursionError as error:
            raise ValueError('The saved queue has excessive nesting.') from error

    def load(self):
        empty = dict(schema_version=SCHEMA_VERSION, tasks=[])
        try:
            return self._read(self.path), []
        except FileNotFoundError:
            if not self.backup.exists():
                return empty, []
            try:
                return self._read(self.backup), ['The saved queue was restored from its backup.']
            except (OSError, ValueError):
                self.writable = False
                raise
        except (OSError, UnsupportedQueueVersion):
            self.writable = False
            raise
        except ValueError as error:
            # Keep the original bytes for recovery before allowing a new save.
            preserved = Path(str(self.path) + f'.corrupt-{time.time_ns()}')
            try:
                shutil.copy2(self.path, preserved)
                recovered = self._read(self.backup) if self.backup.exists() else empty
            except (OSError, ValueError):
                self.writable = False
                raise
            self._recovered = True
            return recovered, [f'The saved queue was damaged. Original preserved at {preserved}. {error}']

    def _atomic_write(self, path, document):
        content = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
        if len(content) > MAX_QUEUE_BYTES:
            raise ValueError('The saved queue exceeds the 16 MiB limit.')
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, document):
        if not self.writable:
            raise PermissionError('Queue writes are disabled to preserve the original file.')
        document = validate_document(document)
        try:
            previous = self._read(self.path)
        except FileNotFoundError:
            previous = None
        except (OSError, UnsupportedQueueVersion):
            self.writable = False
            raise
        except ValueError:
            if not self._recovered:
                self.writable = False
                raise
            previous = None
        if previous == document:
            return
        if previous is not None:
            self._atomic_write(self.backup, previous)
        self._atomic_write(self.path, document)
        self._recovered = False
