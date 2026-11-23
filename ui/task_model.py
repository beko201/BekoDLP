"""Read-only task snapshots with numeric sort values for the Qt table."""
from dataclasses import dataclass
from collections.abc import Mapping
import math
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ui.task_controller import task_status
from media_options import AUDIO_CODECS, CONTAINERS, VIDEO_CODECS
from utils import format_eta
from ui.i18n import duration_text, register, tr


SORT_ROLE = Qt.ItemDataRole.UserRole + 1
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 2

TABLE_TASK_FIELDS = (
    'id', 'url', 'title', 'format', 'quality', 'status', 'progress', 'warnings',
    'output_path', 'output_mode', 'container', 'video_codec', 'audio_codec',
    'audio_bit_depth', '_progress_stage', 'speed_bps', 'eta_seconds',
)
_FIELD_POSITIONS = {key: index for index, key in enumerate(TABLE_TASK_FIELDS)}
_MISSING = object()


class _TaskSnapshot(Mapping):
    """Fixed field order avoids a separate hash table for every displayed row."""
    __slots__ = ('_values',)

    def __init__(self, task):
        self._values = tuple(tuple(task.get('warnings', ())) if key == 'warnings'
                             else task.get(key, _MISSING) for key in TABLE_TASK_FIELDS)

    def __getitem__(self, key):
        value = self._values[_FIELD_POSITIONS[key]]
        if value is _MISSING:
            raise KeyError(key)
        return value

    def get(self, key, default=None):
        position = _FIELD_POSITIONS.get(key)
        value = self._values[position] if position is not None else _MISSING
        return default if value is _MISSING else value

    def __iter__(self):
        return (key for key, value in zip(TABLE_TASK_FIELDS, self._values) if value is not _MISSING)

    def __len__(self):
        return sum(value is not _MISSING for value in self._values)


@dataclass(slots=True)
class TaskRow:
    snapshot: Mapping
    display: tuple
    sort: tuple
    progress: float


def _make_row(tid, task):
    # The manager owns full options, diagnostics and internal processing state.
    snapshot = _TaskSnapshot(task)
    title = task.get("title", "")
    if title == "(resolving title...)":
        title = tr("Resolving title…")
    raw_status = task.get("status", "")
    downloading = raw_status.startswith("Downloading") and task.get("_progress_stage", "downloading") == "downloading"
    speed_bps, eta_seconds = task.get("speed_bps"), task.get("eta_seconds")
    speed = (speed_bps * 8 / 1e6 if downloading and isinstance(speed_bps, (int, float))
             and math.isfinite(speed_bps) and speed_bps >= 0 else -1)
    eta_sort = (eta_seconds if downloading and isinstance(eta_seconds, (int, float))
                and math.isfinite(eta_seconds) and eta_seconds >= 0 else math.inf)
    eta = duration_text(format_eta(eta_sort)) if downloading else ""
    progress = float(task.get("progress", 0))
    progress = max(0, min(100, progress)) if math.isfinite(progress) else 0
    quality = task.get("quality", "") if task.get("format") != "MP3" else "—"
    resolution = re.match(r"\d+", quality)
    number = re.search(r"\d+$", tid)
    mode_label = {"video": "Video", "audio": "Audio"}.get(task.get("output_mode"))
    display = (tid, title, tr(mode_label or {"MP4": "Video", "MP3": "Audio"}.get(task.get("format"), task.get("format", ""))),
               quality, tr(task_status(task)), f"{progress:.0f}%",
               f"{speed:.2f} Mbps" if speed >= 0 else "—",
               eta if eta else "—")
    sort = (int(number[0]) if number else 0, title.casefold(), display[2],
            int(resolution[0]) if resolution else -1, display[4], progress, speed, eta_sort)
    return TaskRow(snapshot, display, sort, progress)


class TaskTableModel(QAbstractTableModel):
    HEADERS = ("Task", "Title", "Format", "Quality", "Status", "Progress", "Speed", "Time left")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ids = []
        self._positions = {}
        self._tasks = {}
        register(self, self._retranslate)

    def _retranslate(self):
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.HEADERS) - 1)
        if self._ids:
            self.update_tasks([(tid, self._tasks[tid].snapshot) for tid in self._ids])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._ids)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
                return tr(self.HEADERS[section])
            if orientation == Qt.Orientation.Vertical:
                return section + 1
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._ids):
            return None
        if not 0 <= index.column() < len(self.HEADERS):
            return None
        tid = self._ids[index.row()]
        row = self._tasks[tid]
        column = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return row.display[column]
        if role == Qt.ItemDataRole.UserRole:
            return tid
        if role == SORT_ROLE:
            return row.sort[column]
        if role == PROGRESS_ROLE:
            return row.progress if column == 5 else None
        if role == Qt.ItemDataRole.TextAlignmentRole and column not in (1, 4):
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row, column)
        return None

    @staticmethod
    def _tooltip(row, column):
        if column == 2 and row.snapshot.get("output_mode") in ("video", "audio"):
            task = row.snapshot
            details = tr('Mode: {value}', value=tr(task['output_mode'].title()))
            details += '\n' + tr('Container: {value}', value=tr(CONTAINERS.get(task.get('container'), 'Automatic')))
            if task.get("output_mode") != "audio":
                details += '\n' + tr('Video codec: {value}', value=tr(VIDEO_CODECS.get(task.get('video_codec'), 'Original')))
            details += '\n' + tr('Audio codec: {value}', value=tr(AUDIO_CODECS.get(task.get('audio_codec'), 'Original')))
            if task.get("audio_codec") in ("flac", "lpcm"):
                depth = task.get("audio_bit_depth", "original")
                details += '\n' + tr('Bit depth: {value}', value=tr('Original' if depth == 'original' else depth + '-bit'))
            if task.get("output_path"):
                details += '\n' + tr('Saved as: {value}', value=task['output_path'])
            return details
        if column == 1:
            return row.display[1] + "\n" + row.snapshot.get("url", "")
        if column == 4:
            if row.snapshot.get("status", "").startswith("Error") or row.snapshot["warnings"]:
                return tr('{status}. Right-click for diagnostic details.', status=row.display[4])
            if row.snapshot.get("output_path"):
                return tr("Double-click to show the downloaded file in its folder.")
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled

    def update_task(self, tid, task):
        self.update_tasks([(tid, task)])

    def update_tasks(self, tasks):
        """Append new rows in one transaction; retain existing row identities."""
        rows = {tid: _make_row(tid, task) for tid, task in tasks}
        added = [tid for tid in rows if tid not in self._positions]
        existing = [tid for tid in rows if tid in self._positions]
        if added:
            first = len(self._ids)
            self.beginInsertRows(QModelIndex(), first, first + len(added) - 1)
            self._ids.extend(added)
            for position, tid in enumerate(added, first):
                self._positions[tid] = position
                self._tasks[tid] = rows[tid]
            self.endInsertRows()
        for tid in existing:
            position = self._positions[tid]
            previous = self._tasks[tid]
            self._tasks[tid] = rows[tid]
            self._notify_changes(position, previous, rows[tid])

    def _notify_changes(self, position, previous, current):
        changes = [[] for _ in self.HEADERS]
        for column, (old, new) in enumerate(zip(previous.display, current.display)):
            if old != new:
                changes[column].extend((Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole))
            if previous.sort[column] != current.sort[column]:
                changes[column].append(SORT_ROLE)
        if previous.progress != current.progress:
            changes[5].append(PROGRESS_ROLE)
        # A changed diagnostic status need not change its concise table label.
        # Compare actual tooltips as well, without notifying unrelated columns.
        for column in (1, 2, 4):
            if self._tooltip(previous, column) != self._tooltip(current, column):
                changes[column].append(Qt.ItemDataRole.ToolTipRole)
        first = 0
        while first < len(changes):
            roles = changes[first]
            last = first
            while last + 1 < len(changes) and changes[last + 1] == roles:
                last += 1
            if roles:
                self.dataChanged.emit(self.index(position, first), self.index(position, last), roles)
            first = last + 1

    def remove_task(self, tid):
        self.remove_tasks([tid])

    def remove_tasks(self, tids):
        """Remove contiguous ranges backwards, rebuilding positions once per batch."""
        positions = sorted({self._positions[tid] for tid in tids if tid in self._positions})
        if not positions:
            return
        ranges = []
        first = last = positions[0]
        for position in positions[1:]:
            if position == last + 1:
                last = position
            else:
                ranges.append((first, last))
                first = last = position
        ranges.append((first, last))
        for first, last in reversed(ranges):
            self.beginRemoveRows(QModelIndex(), first, last)
            for tid in self._ids[first:last + 1]:
                del self._tasks[tid]
                del self._positions[tid]
            del self._ids[first:last + 1]
            self.endRemoveRows()
        for position in range(positions[0], len(self._ids)):
            self._positions[self._ids[position]] = position

    def task_for_id(self, tid):
        row = self._tasks.get(tid)
        if row is None:
            return None
        return {**row.snapshot, "warnings": list(row.snapshot["warnings"])}
