"""One local progress contract for library, media and assistant producers.

Progress is an observation of work, not a scheduler or a completion journal.
Unknown work stays indeterminate. Producers report completed units and call
``succeed(durable=True)`` only after their actual durable operation succeeds.
Native consumers poll immutable snapshots; worker threads never touch wx.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import Enum
import math
import threading
import time
from uuid import uuid4


MAX_UNITS = 10 ** 15
_UNCHANGED = object()
_CURRENT_PROGRESS = ContextVar('carr_current_progress', default=None)


class ProgressError(ValueError):
    """An invalid state transition or untruthful progress observation."""


class ProgressState(str, Enum):
    RUNNING = 'running'
    PAUSED = 'paused'
    CANCELLING = 'cancelling'
    CANCELLED = 'cancelled'
    FAILED = 'failed'
    SUCCEEDED = 'succeeded'
    UNAVAILABLE = 'unavailable'


TERMINAL_STATES = frozenset((ProgressState.CANCELLED, ProgressState.FAILED,
                             ProgressState.SUCCEEDED, ProgressState.UNAVAILABLE))


def _text(value, name, limit, *, empty=True):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(ord(character) < 32 and character != '\n' for character in value)
            or '\x7f' in value):
        raise ProgressError(f'{name} must be bounded plain text.')
    return value


def _count(value, name):
    if type(value) is not int or not 0 <= value <= MAX_UNITS:
        raise ProgressError(f'{name} must be a nonnegative integer no greater than {MAX_UNITS}.')
    return value


def _total(value):
    return None if value is None else _count(value, 'Total units')


def _percent(completed, total):
    if total is None:
        return None
    return min(99, completed * 100 // total) if total else 0


@dataclass(frozen=True)
class ProgressSnapshot:
    operation_id: str
    title: str
    state: ProgressState
    phase: str
    message: str
    completed: int
    total: int | None
    unit: str
    percent: int | None
    eta_seconds: float | None
    revision: int
    started_at: float
    updated_at: float
    parent_id: str | None = None

    @property
    def terminal(self):
        return self.state in TERMINAL_STATES

    def describe(self):
        state = {
            ProgressState.RUNNING: 'Running', ProgressState.PAUSED: 'Paused',
            ProgressState.CANCELLING: 'Cancellation requested; waiting for the operation to stop',
            ProgressState.CANCELLED: 'Cancelled', ProgressState.FAILED: 'Failed',
            ProgressState.SUCCEEDED: 'Completed successfully',
            ProgressState.UNAVAILABLE: 'Progress reporting is unavailable; check the operation result',
        }[self.state]
        parts = [f'{self.title}. {state}.']
        if self.phase:
            parts.append(f'Phase: {self.phase}.')
        if self.percent is not None:
            parts.append(f'{self.percent} percent.')
        elif not self.terminal:
            parts.append('Progress is indeterminate; the total is not known.')
        if self.total is not None:
            parts.append(f'{self.completed} of {self.total} {self.unit}.')
            if self.completed == self.total and not self.terminal:
                parts.append('Reported units are complete; waiting for durable completion.')
        elif self.completed:
            parts.append(f'{self.completed} {self.unit} completed; total unknown.')
        if self.eta_seconds is not None:
            parts.append(f'Estimated remaining time: {math.ceil(self.eta_seconds)} seconds, '
                         'based on measured progress.')
        if self.message:
            parts.append(self.message)
        return '\n'.join(parts)


class ProgressHub:
    """Thread-safe latest snapshots with bounded completed-task history.

    Child tasks have their own measured progress. They never implicitly change
    a parent's percentage. A producer may aggregate only genuinely comparable
    units or a separately measured phase model.
    """
    def __init__(self, *, clock=time.monotonic, max_tasks=64):
        if not callable(clock) or type(max_tasks) is not int or not 1 <= max_tasks <= 256:
            raise ProgressError('A clock and a task limit between 1 and 256 are required.')
        self._clock, self._limit = clock, max_tasks
        self._lock = threading.RLock()
        self._tasks = {}

    def _now(self):
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ProgressError('The progress clock must return a finite monotonic time.')
        return float(value)

    def start(self, title, *, total=None, unit='units', parent_id=None):
        title, unit = _text(title, 'Operation title', 200, empty=False), _text(unit, 'Unit name', 40, empty=False)
        total = _total(total)
        with self._lock:
            if parent_id is not None:
                if (not isinstance(parent_id, str) or parent_id not in self._tasks
                        or self._tasks[parent_id]._value.terminal):
                    raise ProgressError('A child task needs an active parent in this progress hub.')
            now = self._now()
            if len(self._tasks) >= self._limit:
                parents = {task._value.parent_id for task in self._tasks.values()}
                victim = next((identifier for identifier, task in self._tasks.items()
                               if task._value.terminal and identifier not in parents), None)
                if victim is None:
                    raise ProgressError('The progress task limit is reached; active tasks are retained.')
                del self._tasks[victim]
            identifier = uuid4().hex
            value = ProgressSnapshot(identifier, title, ProgressState.RUNNING, '', '', 0, total, unit,
                                     _percent(0, total), None, 1, now, now, parent_id)
            task = ProgressTask(self, value)
            self._tasks[identifier] = task
            return task

    def snapshot(self, operation_id=None):
        with self._lock:
            if operation_id is not None:
                task = self._tasks.get(operation_id)
                return task._value if task is not None else None
            values = [task._value for task in self._tasks.values()]
            return next((value for value in reversed(values) if not value.terminal),
                        values[-1] if values else None)

    def snapshots(self, *, active_only=False):
        with self._lock:
            return tuple(task._value for task in self._tasks.values()
                         if not active_only or not task._value.terminal)


class ProgressTask:
    def __init__(self, hub, value):
        self._hub, self._value = hub, value
        self._rate_anchor = value.updated_at, value.completed

    @property
    def operation_id(self):
        return self._value.operation_id

    @property
    def cancellation_requested(self):
        with self._hub._lock:
            return self._value.state in (ProgressState.CANCELLING, ProgressState.CANCELLED)

    def snapshot(self):
        with self._hub._lock:
            return self._value

    def _publish(self, **changes):
        now = self._hub._now()
        if now < self._value.updated_at:
            raise ProgressError('The progress clock moved backwards.')
        self._value = replace(self._value, revision=self._value.revision + 1, updated_at=now, **changes)
        return self._value

    def update(self, *, completed=None, total=_UNCHANGED, phase=None, message=None):
        with self._hub._lock:
            previous = self._value
            if previous.state != ProgressState.RUNNING:
                raise ProgressError('Only a running task accepts unit updates.')
            completed = previous.completed if completed is None else _count(completed, 'Completed units')
            total = previous.total if total is _UNCHANGED else _total(total)
            phase = previous.phase if phase is None else _text(phase, 'Phase', 200)
            message = previous.message if message is None else _text(message, 'Progress message', 1000)
            if completed < previous.completed:
                raise ProgressError('Completed units cannot move backwards within an operation.')
            if total is not None and completed > total:
                raise ProgressError('Completed units cannot exceed the known total.')
            changed_total = total != previous.total
            if changed_total:
                old = 'unknown' if previous.total is None else str(previous.total)
                new = 'unknown' if total is None else str(total)
                explanation = f'Total changed from {old} to {new} {previous.unit}; progress was recalculated.'
                message = explanation + (' ' + message if message and not message.startswith('Total changed from ') else '')
                message = message[:1200]
            if (completed, total, phase, message) == (previous.completed, previous.total, previous.phase, previous.message):
                return previous
            now = self._hub._now()
            if now < previous.updated_at:
                raise ProgressError('The progress clock moved backwards.')
            anchor = (now, completed) if changed_total or phase != previous.phase else self._rate_anchor
            elapsed, advanced = now - anchor[0], completed - anchor[1]
            eta = None
            if total is not None and completed < total and elapsed >= 1.0 and advanced > 0:
                eta = (total - completed) * elapsed / advanced
            result = self._publish(completed=completed, total=total, phase=phase, message=message,
                                   percent=_percent(completed, total), eta_seconds=eta)
            self._rate_anchor = anchor
            return result

    def advance(self, units=1, *, phase=None, message=None):
        units = _count(units, 'Advanced units')
        with self._hub._lock:
            return self.update(completed=self._value.completed + units, phase=phase, message=message)

    def pause(self, message=''):
        message = _text(message, 'Pause message', 1000)
        with self._hub._lock:
            if self._value.state not in (ProgressState.RUNNING, ProgressState.CANCELLING):
                raise ProgressError('Only running or cancelling work can report a pause.')
            return self._publish(state=ProgressState.PAUSED, message=message, eta_seconds=None)

    def resume(self, message=''):
        message = _text(message, 'Resume message', 1000)
        with self._hub._lock:
            if self._value.state != ProgressState.PAUSED:
                raise ProgressError('Only a paused task can be resumed.')
            result = self._publish(state=ProgressState.RUNNING, message=message, eta_seconds=None)
            self._rate_anchor = result.updated_at, result.completed
            return result

    def request_cancel(self, message='Cancellation requested.'):
        message = _text(message, 'Cancellation message', 1000)
        with self._hub._lock:
            if self._value.state == ProgressState.CANCELLING:
                return self._value
            if self._value.terminal:
                raise ProgressError('A finished task cannot request cancellation.')
            return self._publish(state=ProgressState.CANCELLING, message=message, eta_seconds=None)

    def cancelled(self, message='Operation cancelled.'):
        return self._finish(ProgressState.CANCELLED, message)

    def fail(self, message):
        return self._finish(ProgressState.FAILED, message)

    def unavailable(self, message='Progress reporting stopped; the operation result is reported separately.'):
        """End a broken observer without falsely changing the work's outcome."""
        return self._finish(ProgressState.UNAVAILABLE, message)

    def succeed(self, *, durable=True, message=''):
        if durable is not True:
            raise ProgressError('Successful completion requires verified durable success.')
        with self._hub._lock:
            if self._value.state == ProgressState.PAUSED:
                raise ProgressError('Resume the task before confirming successful completion.')
            if self._value.total is not None and self._value.completed != self._value.total:
                raise ProgressError('Known units are incomplete; progress cannot claim 100 percent.')
            return self._finish(ProgressState.SUCCEEDED, message)

    def _finish(self, state, message):
        message = _text(message, 'Completion message', 1000)
        with self._hub._lock:
            if self._value.terminal:
                raise ProgressError('A finished task cannot change its terminal result.')
            return self._publish(state=state, message=message, eta_seconds=None,
                                 percent=100 if state == ProgressState.SUCCEEDED else self._value.percent)


def current_progress():
    """Return this worker's explicitly bound reporter, or None for legacy code."""
    return _CURRENT_PROGRESS.get()


@contextmanager
def bind_progress(task):
    if not isinstance(task, ProgressTask):
        raise ProgressError('Bind a ProgressTask, not a callback or executable instruction.')
    token = _CURRENT_PROGRESS.set(task)
    try:
        yield task
    finally:
        _CURRENT_PROGRESS.reset(token)


def operation_outcome(result):
    """Combine actual job/batch receipts without treating partial work as done.

    Plain values and rows without status fields remain ordinary successful
    return values. An explicit unknown status requires attention. Only typed
    status fields are inspected; user text in search results is never a state.
    """
    from .assistant.models import JobReport
    complete = {'complete', 'completed', 'succeeded', 'success', 'ok', 'passed', 'pass',
                'ready', 'verified', 'saved', 'exported', 'duplicate', 'excluded'}
    failure = {'failed', 'error', 'timed_out', 'timeout', 'corrupt', 'invalid'}
    cancelled = {'cancelled', 'canceled', 'aborted'}
    rank = {'succeeded': 0, 'paused': 1, 'cancelled': 2, 'failed': 3}
    outcome, pending, inspected = 'succeeded', [result], 0
    while pending:
        item = pending.pop()
        inspected += 1
        if inspected > 100_000:
            raise ValueError('Operation result exceeds the progress receipt limit.')
        if isinstance(item, JobReport):
            item = {'status': item.status, 'results': item.results}
        if isinstance(item, (list, tuple)):
            if len(pending) + len(item) + inspected > 100_000:
                raise ValueError('Operation result exceeds the progress receipt limit.')
            pending.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ('status', 'state'):
            state = item.get(key)
            if state is None:
                continue
            if not isinstance(state, str):
                current = 'paused'
            else:
                state = state.casefold()
                current = ('failed' if state in failure else 'cancelled' if state in cancelled
                           else 'succeeded' if state in complete else 'paused')
            if rank[current] > rank[outcome]:
                outcome = current
        for key in ('rows', 'results'):
            if isinstance(item.get(key), (list, tuple)):
                pending.append(item[key])
    return outcome

