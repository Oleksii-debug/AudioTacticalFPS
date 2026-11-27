"""Shared, measured progress and opt-in notification preferences.

Progress describes work; it does not authorize operations or make their result
durable. A producer calls complete(verified=True) only after its existing
verification/commit has returned. Unknown totals stay indeterminate. The hub
keeps a bounded latest snapshot rather than a queue of every worker update.
It has no wx, audio renderer, network, or speech-engine dependency.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import json
import math
import os
import re
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile
import threading
import time
from uuid import uuid4

from .ingest.writer_guard import exclusive_writer

FILENAME = 'progress-preferences.json'
MAX_BYTES = 16 * 1024
MODES = ('off', 'beep', 'speech', 'beep_speech')
STATES = frozenset(('running', 'paused', 'complete', 'failed', 'cancelled'))
TERMINAL = frozenset(('complete', 'failed', 'cancelled'))
_CURRENT = ContextVar('carr_current_progress', default=None)
_UNSET = object()


def _number(value, minimum, maximum, label):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value)):
        raise ValueError(f'{label} must be between {minimum} and {maximum}.')
    return float(value)


def _text(value, label, *, maximum=500, empty=True):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip())
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise ValueError(f'{label} must be bounded, readable single-line text.')
    return value


def display_message(value):
    """Keep a worker's status readable; never interpret text as a command."""
    return ' '.join(str(value).split())[:500]


@dataclass(frozen=True)
class ProgressPreferences:
    mode: str = 'off'
    interval_seconds: float = 5.0
    volume: float = 0.12
    muted: bool = False
    quiet: bool = False
    background: bool = False
    suppress_during_media: bool = True
    voice_id: str = ''
    voice_language: str = ''
    voice_fingerprint: str = ''

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError('Choose Off, Beep, Speech, or Beep and Speech.')
        _number(self.interval_seconds, 0.5, 60, 'Notification interval')
        _number(self.volume, 0, 1, 'Notification volume')
        for key in ('muted', 'quiet', 'background', 'suppress_during_media'):
            if type(getattr(self, key)) is not bool:
                raise ValueError(f'{key} must be true or false.')
        _text(self.voice_id, 'Progress voice identity', maximum=2048)
        if any((self.voice_id, self.voice_language, self.voice_fingerprint)):
            if (not self.voice_id or self.voice_language not in ('en', 'uk', 'sk')
                    or not isinstance(self.voice_fingerprint, str)
                    or not re.fullmatch('[0-9a-f]{64}', self.voice_fingerprint)):
                raise ValueError('Select one verified local voice and its notification language.')
        elif type(self.voice_language) is not str or type(self.voice_fingerprint) is not str:
            raise ValueError('Invalid progress voice selection.')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate progress preference field.')
        result[key] = value
    return result


def decode_preferences(raw):
    """Validate immutable backup/import bytes without filesystem side effects."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Progress preferences must be nonempty and at most 16 KiB.')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if type(data) is dict and set(data) == {'version', 'preferences'}:
            # Canonical pre-convergence settings use a disjoint envelope and
            # integer percentages. Reading is passive; only an explicit save
            # rewrites them into the current schema. No voice is inferred.
            expected = {'mode', 'interval_seconds', 'volume', 'muted',
                        'allow_background', 'quiet_during_media'}
            values = data['preferences']
            if (len(raw) > 8192 or type(data['version']) is not int or data['version'] != 1
                    or type(values) is not dict or set(values) != expected):
                raise ValueError('Unsupported canonical progress preferences format.')
            interval = _number(values['interval_seconds'], 1, 60, 'Canonical notification interval')
            if type(values['volume']) is not int or not 0 <= values['volume'] <= 100:
                raise ValueError('Canonical notification volume must be an integer from 0 to 100.')
            return ProgressPreferences(mode=values['mode'], interval_seconds=interval,
                volume=values['volume'] / 100, muted=values['muted'],
                background=values['allow_background'],
                suppress_during_media=values['quiet_during_media'])
        if (type(data) is not dict or set(data) != {'version', 'kind', 'preferences'}
                or type(data['version']) is not int or data['version'] not in (1, 2)
                or data['kind'] != 'carr-progress'
                or type(data['preferences']) is not dict
                or set(data['preferences']) != (set(ProgressPreferences.__dataclass_fields__)
                    - ({'voice_id', 'voice_language', 'voice_fingerprint'} if data['version'] == 1 else set()))):
            raise ValueError('Unsupported progress preferences format.')
        return ProgressPreferences(**data['preferences'])
    except (UnicodeError, TypeError, RecursionError) as exc:
        raise ValueError('Invalid progress preferences JSON.') from exc


def encode_preferences(preferences):
    if not isinstance(preferences, ProgressPreferences):
        raise ValueError('Use validated progress preferences.')
    raw = (json.dumps({'version': 2, 'kind': 'carr-progress',
                       'preferences': asdict(preferences)}, sort_keys=True, indent=2) + '\n').encode('utf-8')
    decode_preferences(raw)
    return raw


def _checked_path(path):
    path = Path(path).absolute()
    if str(path).startswith(('//', '\\\\')):
        raise ValueError('Progress settings require a local directory.')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Progress settings cannot use symbolic links or reparse points.')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Progress settings need a local directory.')
        if part == path and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError('Progress settings must be a regular file without hard links.')
    return path


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def load_preferences(root):
    path = _checked_path(Path(root) / FILENAME)
    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    except FileNotFoundError:
        return ProgressPreferences()
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (_identity(before) != _identity(opened) or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1 or opened.st_size > MAX_BYTES):
            raise ValueError('Progress settings changed or exceed the size limit.')
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    _checked_path(path)
    if (_identity(before) != _identity(after) or _identity(before) != _identity(path.lstat())
            or len(raw) != before.st_size):
        raise ValueError('Progress settings changed while reading.')
    return decode_preferences(raw)


def save_preferences(root, preferences):
    raw = encode_preferences(preferences)
    path = _checked_path(Path(root) / FILENAME)
    with exclusive_writer(path.parent):
        _checked_path(path)
        temporary = None
        try:
            with NamedTemporaryFile(dir=path.parent, prefix='.progress-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())  # Writable descriptor, including Windows.
            _checked_path(path)
            os.replace(temporary, path)
            if hasattr(os, 'O_DIRECTORY'):
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return preferences


@dataclass(frozen=True)
class ProgressEvent:
    operation_id: str
    title: str
    phase: str
    state: str
    completed: int
    total: int | None
    unit: str
    message: str
    sequence: int
    timestamp: float
    parent_id: str | None = None
    verified: bool = False
    transition: str = 'update'
    eta_seconds: float | None = None
    total_changed_reason: str = ''

    @property
    def token(self):
        """Native callbacks use the same immutable operation identity."""
        return self.operation_id

    @property
    def percent(self):
        if self.state == 'complete' and self.verified:
            return 100
        if self.total is None:
            return None
        return min(99, self.completed * 100 // self.total) if self.total else 0

    @property
    def terminal(self):
        return self.state in TERMINAL

    @property
    def text(self):
        parts = [self.title, self.state]
        parts.append('total unknown' if self.percent is None else f'{self.percent}%')
        if self.total is not None:
            parts.append(f'{self.completed} of {self.total} {self.unit}')
        elif self.completed:
            parts.append(f'{self.completed} {self.unit} completed')
        if self.phase:
            parts.append(self.phase)
        if self.message and self.message != self.phase:
            parts.append(self.message)
        if self.total_changed_reason:
            parts.append('Total changed: ' + self.total_changed_reason)
        if self.eta_seconds is not None:
            parts.append(f'estimated {math.ceil(self.eta_seconds)} seconds remaining from measured work')
        return '; '.join(parts) + '.'


def _counts(completed, total):
    if type(completed) is not int or completed < 0:
        raise ValueError('Completed work must be a nonnegative integer.')
    if total is not None and (type(total) is not int or total < 0 or completed > total):
        raise ValueError('Total work must be a nonnegative integer at least as large as completed work.')


class ProgressHub:
    """Thread-safe latest events; concurrent operations never share a percent."""
    def __init__(self, *, clock=time.monotonic, maximum=128):
        if type(maximum) is not int or not 1 <= maximum <= 4096:
            raise ValueError('Progress history size must be between 1 and 4096.')
        self._clock, self._maximum = clock, maximum
        self._lock = threading.RLock()
        self._events, self._rates = {}, {}
        self._sequence = 0
        self._media = set()

    def start(self, title, *, total=None, unit='items', phase='', parent_id=None):
        _text(title, 'Operation title', empty=False)
        _text(unit, 'Work unit', maximum=60, empty=False)
        _text(phase, 'Phase')
        _counts(0, total)
        with self._lock:
            if parent_id is not None and parent_id not in self._events:
                raise ValueError('A progress subtask needs an existing parent operation.')
            if len(self._events) >= self._maximum:
                removable = next((key for key, value in self._events.items() if value.terminal
                                  and not any(e.parent_id == key and not e.terminal for e in self._events.values())), None)
                if removable is None:
                    raise ValueError('Too many concurrent progress operations.')
                self._events.pop(removable)
                self._rates.pop(removable, None)
            identity, now = uuid4().hex, self._clock()
            self._sequence += 1
            self._events[identity] = ProgressEvent(identity, title, phase, 'running', 0, total,
                unit, '', self._sequence, now, parent_id=parent_id, transition='start')
            self._rates[identity] = (now, 0)
            return ProgressReporter(self, identity)

    def snapshot(self):
        with self._lock:
            return tuple(self._events.values())

    def get(self, operation_id):
        with self._lock:
            return self._events[operation_id]

    def _update(self, operation_id, *, completed=_UNSET, total=_UNSET, phase=_UNSET,
                message=_UNSET, state=None, verified=False, transition='update',
                total_changed_reason=''):
        with self._lock:
            old = self._events[operation_id]
            if old.terminal:
                raise ValueError('A finished operation cannot change; start a new operation.')
            target = state or old.state
            if target not in STATES:
                raise ValueError('Unsupported progress state.')
            if target == 'complete' and verified is not True:
                raise ValueError('Completion requires verified durable success.')
            if old.state == 'paused' and target == 'complete':
                raise ValueError('Resume an operation before marking it complete.')
            done = old.completed if completed is _UNSET else completed
            size = old.total if total is _UNSET else total
            _counts(done, size)
            changed = size != old.total
            _text(total_changed_reason, 'Changed-total explanation')
            if changed and old.total is not None and not total_changed_reason.strip():
                raise ValueError('Explain why the known work total changed.')
            if done < old.completed and not changed:
                raise ValueError('Completed work cannot decrease for an unchanged total.')
            next_phase = old.phase if phase is _UNSET else _text(phase, 'Phase')
            next_message = old.message if message is _UNSET else _text(message, 'Progress message')
            if target == 'complete' and size is not None and done != size:
                raise ValueError('All known work units must be accounted for before completion.')
            now = self._clock()
            if changed or transition in ('pause', 'resume'):
                self._rates[operation_id] = (now, done)
            start_time, start_count = self._rates[operation_id]
            eta = None
            if (target == 'running' and size is not None and done < size
                    and done > start_count and now - start_time >= 1):
                eta = (size - done) * (now - start_time) / (done - start_count)
            self._sequence += 1
            event = replace(old, state=target, completed=done, total=size,
                phase=next_phase, message=next_message,
                verified=target == 'complete' and verified,
                transition=transition, sequence=self._sequence, timestamp=now,
                eta_seconds=eta, total_changed_reason=total_changed_reason or old.total_changed_reason)
            self._events[operation_id] = event
            return event

    @contextmanager
    def media_activity(self):
        """Suppress optional notifications during app recording/live output."""
        token = object()
        with self._lock:
            self._media.add(token)
        try:
            yield
        finally:
            with self._lock:
                self._media.discard(token)

    @property
    def media_active(self):
        with self._lock:
            return bool(self._media)


class ProgressReporter:
    def __init__(self, hub, operation_id):
        self.hub, self.operation_id = hub, operation_id

    @property
    def event(self):
        return self.hub.get(self.operation_id)

    def update(self, **values):
        return self.hub._update(self.operation_id, **values)

    def complete(self, *, verified=False, message=''):
        return self.update(state='complete', verified=verified, message=display_message(message), transition='complete')

    def fail(self, message=''):
        return self.update(state='failed', message=display_message(message), transition='failed')

    def cancel(self, message=''):
        return self.update(state='cancelled', message=display_message(message), transition='cancelled')

    def pause(self, message=''):
        return self.update(state='paused', message=display_message(message), transition='pause')

    def resume(self, message=''):
        if self.event.state != 'paused':
            raise ValueError('Only a paused operation can resume.')
        return self.update(state='running', message=display_message(message), transition='resume')

    def child(self, title, **values):
        return self.hub.start(title, parent_id=self.operation_id, **values)

    @contextmanager
    def bind(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)


def current_progress():
    return _CURRENT.get()


def report_progress(*, phase=None, message=None, completed=_UNSET, total=_UNSET,
                    total_changed_reason=''):
    """Optional backend hook; unobserved CLI/service calls remain unchanged."""
    reporter = current_progress()
    if reporter is not None:
        values = dict(completed=completed, total=total, total_changed_reason=total_changed_reason)
        if phase is not None:
            values['phase'] = display_message(phase)
        if message is not None:
            values['message'] = display_message(message)
        return reporter.update(**values)
    return None


def batch_progress(reporter, batch):
    """Adapt the existing durable import-batch counts without interpreting text."""
    counts = batch.get('counts', {})
    total = batch.get('total', len(batch.get('rows', ())))
    completed = sum(counts.get(key, 0) for key in
                    ('complete', 'duplicate', 'excluded', 'rejected', 'failed', 'quarantined'))
    reason = 'The saved import selection changed.' if reporter.event.total not in (None, total) else ''
    return reporter.update(completed=completed, total=total,
                           phase='Importing and indexing files', total_changed_reason=reason)


def finish_result(reporter, result, *, cancelled=False):
    """Resolve actual nested receipts without hiding failure behind a stop request.

    Plain read/query return values can finish normally. Explicit unknown states
    require attention. A committed typed success remains successful when a
    cancellation request arrives after its publication.
    """
    states, all_counts, pending, inspected = [], {}, [result], 0
    while pending:
        value = pending.pop()
        inspected += 1
        if inspected > 100_000:
            return reporter.fail('The operation returned too many progress receipts to verify.')
        if isinstance(value, (tuple, list)):
            pending.extend(value)
            continue
        if isinstance(value, dict):
            for key in ('state', 'status'):
                if key in value and value[key] is not None:
                    states.append(value[key].casefold() if isinstance(value[key], str) else 'unknown')
            counts = value.get('counts', {})
            if not isinstance(counts, dict):
                states.append('unknown')
            else:
                for key in ('failed', 'rejected', 'quarantined', 'queued', 'running', 'cancelled'):
                    if counts.get(key):
                        all_counts[key] = True
            for key in ('rows', 'results'):
                if isinstance(value.get(key), (tuple, list)):
                    pending.append(value[key])
        else:
            state = getattr(value, 'status', '')
            if state:
                states.append(state.casefold() if isinstance(state, str) else 'unknown')
    if (all_counts.get('failed') or all_counts.get('rejected')
            or any(s in ('failed', 'error', 'rejected', 'timed_out', 'timeout', 'corrupt', 'invalid') for s in states)):
        return reporter.fail('One or more operations failed; review the result.')
    if all_counts.get('cancelled') or any(s in ('cancelled', 'canceled', 'aborted') for s in states):
        return reporter.cancel('The operation was cancelled.')
    if all_counts.get('quarantined') or any(
            s in ('quarantined', 'needs_review', 'needs_reapproval', 'review_required', 'attention') for s in states):
        return reporter.pause('Review is required before this operation can finish.')
    if all_counts.get('queued') or all_counts.get('running') or any(
            s in ('paused', 'partial', 'queued', 'running', 'interrupted') for s in states):
        return reporter.pause('Work remains; resume from the saved checkpoint.')
    successful = ('complete', 'completed', 'success', 'succeeded', 'ok', 'passed', 'pass',
                  'ready', 'verified', 'saved', 'exported', 'duplicate', 'excluded')
    if any(s not in successful for s in states):
        return reporter.pause('The service did not report a supported verified completion state.')
    explicit = bool(states) and all(s in successful for s in states)
    counts = result.get('counts', {}) if isinstance(result, dict) else {}
    if cancelled and not explicit and not counts:
        return reporter.cancel('Cancellation was requested; no verified completion was reported.')
    if reporter.event.total is not None and reporter.event.completed != reporter.event.total:
        return reporter.pause('The service returned before all measured work was accounted for.')
    return reporter.complete(verified=True, message='The operation returned its verified result.')
