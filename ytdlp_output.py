"""Bounded byte-to-line decoding and JSON budgets at the yt-dlp boundary."""
import json
import re

from errors import MediaOperationError

OUTPUT_CHUNK_BYTES = 64 * 1024
OUTPUT_QUEUE_CHUNKS = 16
MAX_OUTPUT_LINE_BYTES = 8 * 1024 ** 2
MAX_JSON_DOCUMENT_BYTES = 8 * 1024 ** 2
MAX_PLAYLIST_JSON_BYTES = 256 * 1024 ** 2
MAX_COLLECTED_PLAYLIST_JSON_BYTES = 32 * 1024 ** 2
MAX_PLAYLIST_DOCUMENTS = 100_000
MAX_JSON_DEPTH = 128


class OutputLimitError(MediaOperationError):
    pass


class BoundedLines:
    """Accept bounded chunks without ever reading an unbounded stdout line."""
    def __init__(self):
        self.pending = bytearray()
        self.skip_lf = False

    def _append(self, part):
        if len(self.pending) + len(part) > MAX_OUTPUT_LINE_BYTES:
            raise OutputLimitError('yt-dlp output exceeded the per-line size limit.')
        self.pending.extend(part)

    def feed(self, chunk):
        if self.skip_lf and chunk.startswith(b'\n'):
            chunk = chunk[1:]
        self.skip_lf = chunk.endswith(b'\r')
        start = 0
        for match in re.finditer(rb'\r\n|\r|\n', chunk):
            self._append(chunk[start:match.start()])
            line = self.pending.decode('utf-8', errors='replace')
            self.pending.clear()
            yield line
            start = match.end()
        self._append(chunk[start:])

    def finish(self):
        if self.pending:
            line = self.pending.decode('utf-8', errors='replace')
            self.pending.clear()
            yield line


class JsonBudget:
    """Count wire bytes and documents before JSON decoding or retention."""
    def __init__(self, *, max_documents=1, max_bytes=MAX_JSON_DOCUMENT_BYTES):
        self.max_documents = max_documents
        self.max_bytes = max_bytes
        self.documents = 0
        self.bytes = 0

    def read(self, payload, *, object_only=True):
        size = len(payload.encode('utf-8'))
        if size > MAX_JSON_DOCUMENT_BYTES or self.bytes + size > self.max_bytes:
            raise OutputLimitError('yt-dlp metadata exceeded the size limit.')
        if self.documents >= self.max_documents:
            raise OutputLimitError('yt-dlp metadata exceeded the document count limit.')
        self.documents += 1
        self.bytes += size
        # Check nesting before the JSON decoder allocates containers. Ignore
        # brackets inside quoted strings, including escaped quotes/backslashes.
        depth, in_string, escaped = 0, False, False
        for char in payload:
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in '{[':
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise OutputLimitError('yt-dlp metadata exceeded the JSON nesting limit.')
            elif char in '}]':
                depth -= 1
        try:
            value = json.loads(payload)
        except (ValueError, RecursionError):
            raise OutputLimitError('yt-dlp returned invalid or excessively nested metadata JSON.') from None
        if object_only and not isinstance(value, dict):
            raise OutputLimitError('yt-dlp returned an invalid metadata document.')
        return value
