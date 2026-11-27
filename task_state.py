"""Task lifecycle values, independent of user-facing status descriptions."""
from enum import StrEnum


class TaskState(StrEnum):
    QUEUED = 'queued'
    WAITING = 'waiting'
    RUNNING = 'running'
    DOWNLOADING = 'downloading'
    PAUSED = 'paused'
    CANCELLING = 'cancelling'
    CANCELLED = 'cancelled'
    COMPLETED = 'completed'
    FAILED = 'failed'


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})
STOPPED_STATES = TERMINAL_STATES | {TaskState.CANCELLING}
PENDING_STATES = frozenset({TaskState.QUEUED, TaskState.WAITING})
CLEARABLE_STATES = TERMINAL_STATES | PENDING_STATES


def legacy_status_state(status):
    """Adapt historical status-only API calls and snapshots at their boundary.

    New producers must supply an explicit state. Existing integrations can keep
    submitting the old English labels while migrating to the structured API.
    """
    for prefix, state in (('Completed', TaskState.COMPLETED), ('Error', TaskState.FAILED),
                          ('Cancelled', TaskState.CANCELLED), ('Cancel', TaskState.CANCELLING),
                          ('Queu', TaskState.QUEUED), ('Wait', TaskState.WAITING),
                          ('Paused', TaskState.PAUSED), ('Downloading', TaskState.DOWNLOADING)):
        if status.startswith(prefix):
            return state
    return TaskState.RUNNING


def state_of(task):
    """Prefer explicit state; accept historical external snapshots without it."""
    state = task.get('state')
    return TaskState(state) if state is not None else legacy_status_state(task.get('status', ''))
