"""Opt-in foreground scheduling of existing approved local assistant jobs.

No thread, OS task, startup hook, periodic replay or new tool executor is
installed here. A current caller explicitly arms exact pending schedule hashes,
then calls tick from its existing worker. The one existing job lock, journal,
tool dispatcher, progress stream and recovery path remain authoritative.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, RLock

from .models import JobReport, local_path
from .schedule_model import (
    SCHEMA, MAX_ARMED, MAX_HORIZON, MAX_LATENESS_SECONDS,
    ScheduleView, fingerprint, timestamp, utc, validate_schedule,
)


def _utc_now():
    return datetime.now(timezone.utc)


def _root_identity(root):
    root = local_path(root)
    info = root.stat()
    if not root.is_dir() or info.st_ino <= 0:
        raise ValueError('Scheduling requires a stable ordinary local data-directory identity.')
    return root, (info.st_dev, info.st_ino)


def _never_started(record):
    return (record['status'] == 'queued'
            and all(step['status'] == 'pending' and not step['intent'] and step['result'] is None
                    for step in record['steps']))


class ScheduleManager:
    def __init__(self, service, *, clock=_utc_now):
        from .service import AssistantService
        if not isinstance(service, AssistantService) or not callable(clock):
            raise TypeError('Use the existing local assistant service and a UTC clock.')
        self.service, self.clock = service, clock
        self.root, self.root_identity = _root_identity(service.app.root)

    def _current(self):
        root, identity = _root_identity(self.service.app.root)
        if root != self.root or identity != self.root_identity:
            raise PermissionError('The scheduling data directory changed. Reopen and review the current library.')
        return root

    def _now(self):
        return utc(self.clock())

    def _bound(self, record):
        self._current()
        schedule = record.get('schedule')
        return (schedule is not None and not record.get('requires_reapproval')
                and record['data_dir'] == str(self.root)
                and tuple(schedule['root_identity']) == self.root_identity)

    def _view(self, record):
        schedule = record.get('schedule')
        if schedule is None:
            raise ValueError('This saved assistant job has no local schedule.')
        validate_schedule(schedule, record)
        bound = self._bound(record)
        state, message = schedule['state'], schedule['message']
        if not bound:
            state, message = 'attention', (
                'This schedule was restored or moved. Review and submit a new plan in the current library.')
        elif state == 'dispatching':
            state, message = 'attention', (
                'A scheduled attempt has no settled completion. Review the saved job and use ordinary Resume; '
                'it will not be dispatched again automatically.')
        return ScheduleView(record['id'], schedule['due'], schedule['expires'],
            schedule['plan_sha256'], fingerprint(schedule), state, record['status'], message, bound)

    def get(self, identity):
        return self._view(self.service.store.load(identity))

    def list(self):
        return tuple(view for view, _ in self.review())

    def review(self, *, cancelled=lambda: False):
        """Capture bounded exact existing plans with their current schedule pins."""
        self._current()
        # Reuse the typed producer's complete count/byte/regular-file inventory,
        # rather than reading an unbounded directory of optional state.
        from .snapshots import state_paths
        paths = state_paths(self.root, cancelled=cancelled)
        result = []
        for path in paths:
            if cancelled():
                raise InterruptedError('Schedule review cancelled.')
            if path.name.endswith('.control.json'):
                continue
            record = self.service.store.load(path.stem)
            if 'schedule' in record:
                result.append((self._view(record), record['plan']))
        if cancelled():
            raise InterruptedError('Schedule review cancelled.')
        return tuple(sorted(result, key=lambda row: (row[0].due, row[0].id)))

    def create(self, identity, due, *, expected_plan_sha256, lateness_seconds=3600,
               expected_schedule_sha256=None):
        """Attach one concrete time window to an already reviewed queued job."""
        self._current()
        if type(lateness_seconds) is not int or not 1 <= lateness_seconds <= MAX_LATENESS_SECONDS:
            raise ValueError('Choose a lateness allowance from one second to one day.')
        selected, created = utc(due), self._now()
        if not created <= selected <= created + MAX_HORIZON:
            raise ValueError('Choose a future UTC time within 366 days.')
        with self.service.store.lock(identity):
            record, _ = self.service._load(identity)
            if record['plan_sha256'] != expected_plan_sha256 or not _never_started(record):
                raise PermissionError('Schedule only this exact reviewed, never-started queued plan.')
            old = record.get('schedule')
            if old is not None:
                if fingerprint(old) != expected_schedule_sha256 or old['started'] is not None:
                    raise PermissionError('The previous schedule changed or already attempted execution.')
            elif expected_schedule_sha256 is not None:
                raise ValueError('The selected previous schedule is unavailable.')
            record['schedule'] = dict(schema=SCHEMA, created=timestamp(created), due=timestamp(selected),
                expires=timestamp(selected + timedelta(seconds=lateness_seconds)),
                root_identity=list(self.root_identity), plan_sha256=expected_plan_sha256,
                state='pending', started=None, finished=None, message='')
            self._current()
            self.service.store.save(record)
            return self._view(record)

    def disable(self, identity, *, expected_schedule_sha256):
        """Disable a stored slot under the same actual job lock; never run it."""
        self._current()
        with self.service.store.lock(identity):
            record = self.service.store.load(identity)
            old = record.get('schedule')
            if old is None or fingerprint(old) != expected_schedule_sha256:
                raise ValueError('The selected schedule changed. Refresh before disabling it.')
            old.update(state='disabled', message='Disabled by an explicit local action.')
            self.service.store.save(record)
            return self._view(record)

    def _pending(self, identity, expected):
        record, _ = self.service._load(identity)
        schedule = record.get('schedule')
        if (schedule is None or not self._bound(record) or fingerprint(schedule) != expected
                or schedule['state'] != 'pending' or not _never_started(record)):
            raise PermissionError('The reviewed pending schedule changed or needs a new approval.')
        return record

    def _expire(self, identity, expected, current):
        with self.service.store.lock(identity):
            record = self._pending(identity, expected)
            if current < utc(record['schedule']['expires']):
                raise ValueError('The selected schedule has not expired.')
            record['schedule'].update(state='attention', finished=timestamp(current),
                message='The approved start window expired. Nothing was run; review a new time explicitly.')
            self.service.store.save(record)
            return JobReport(identity, 'attention', 0, len(record['steps']), (),
                             record['schedule']['message'])


@dataclass(frozen=True)
class _Ticket:
    owner: object
    identity: str
    expected: str
    cancelled: Event


class ScheduleSession:
    """In-memory arming dies with this caller; saving or restoring never arms it."""

    def __init__(self, manager):
        if not isinstance(manager, ScheduleManager):
            raise TypeError('Use the current assistant schedule manager.')
        self.manager = manager
        self._lock, self._closed = RLock(), Event()
        self._armed, self._running, self._last_clock = {}, None, None

    @property
    def armed(self):
        with self._lock:
            return tuple(sorted(self._armed))

    def has_due(self):
        with self._lock:
            if self._closed.is_set() or self._running is not None:
                return False
            current = self.manager._now()
            return any(utc(row[1]) <= current for row in self._armed.values())

    def arm(self, selected):
        """The caller supplies exact schedule hashes after reviewing their plans."""
        if (type(selected) is not dict or not 1 <= len(selected) <= MAX_ARMED
                or any(type(key) is not str or type(value) is not str for key, value in selected.items())):
            raise ValueError('Choose one to 32 exact reviewed local schedule identities.')
        captured = {}
        for identity, expected in selected.items():
            record = self.manager._pending(identity, expected)
            captured[identity] = (expected, record['schedule']['due'], record['schedule']['expires'])
        with self._lock:
            if self._closed.is_set() or self._running is not None:
                raise RuntimeError('Finish the active scheduled operation before changing arming.')
            if len(set(self._armed) | set(captured)) > MAX_ARMED:
                raise ValueError('This foreground session can arm at most 32 concrete jobs.')
            self._armed.update(captured)
        return self.armed

    def disarm(self, identities=None):
        with self._lock:
            selected = set(self._armed) if identities is None else set(identities)
            if identities is None and self._running is not None:
                selected.add(self._running.identity)
            for identity in selected:
                self._armed.pop(identity, None)
            if self._running is not None and self._running.identity in selected:
                self._running.cancelled.set()

    def close(self):
        self._closed.set()
        self.disarm()

    def _check_clock(self):
        current = self.manager._now()
        if self._last_clock is not None and current < self._last_clock:
            self.disarm()
            raise RuntimeError('The UTC clock moved backwards. Review schedules and arm a new foreground session.')
        self._last_clock = current
        return current

    def _admit(self, service, record, ticket):
        with self._lock:
            if (ticket is not self._running or ticket.owner is not self or service is not self.manager.service
                    or self._closed.is_set() or ticket.cancelled.is_set()):
                raise InterruptedError('The foreground schedule is no longer armed.')
            schedule = record.get('schedule')
            if (record['id'] != ticket.identity or schedule is None or not self.manager._bound(record)
                    or fingerprint(schedule) != ticket.expected or schedule['state'] != 'pending'
                    or not _never_started(record)):
                raise PermissionError('The approved schedule or its exact local job changed.')
            current = self._check_clock()
            if not utc(schedule['due']) <= current < utc(schedule['expires']):
                raise PermissionError('The scheduled start is outside its approved time window.')
            validate_schedule(schedule, record)
            schedule.update(state='dispatching', started=timestamp(current), finished=None,
                            message='A single scheduled attempt was admitted; completion is not yet confirmed.')

    def tick(self, *, cancelled=lambda: False, progress=None):
        """Run at most one due approved job, through the caller's ordinary worker."""
        if not callable(cancelled):
            raise TypeError('A cancellation callback is required.')
        with self._lock:
            if self._closed.is_set() or self._running is not None or cancelled():
                return None
            self.manager._current()
            current = self._check_clock()
            candidates = sorted((due, identity, expected, expires)
                for identity, (expected, due, expires) in self._armed.items()
                if utc(due) <= current)
            if not candidates:
                return None
            _, identity, expected, expires = candidates[0]
            # Consume this in-memory admission even if lock/path checks fail.
            # A crash or uncertain dispatch is never retried by another tick.
            self._armed.pop(identity)
            ticket = _Ticket(self, identity, expected, Event())
            self._running = ticket
        try:
            if current >= utc(expires):
                return self.manager._expire(identity, expected, current)
            return self.manager.service.run(identity, _schedule_ticket=ticket,
                cancelled=lambda: self._closed.is_set() or ticket.cancelled.is_set() or cancelled(),
                progress=progress)
        except (OSError, ValueError, RuntimeError) as error:
            return JobReport(identity, 'attention', 0, 0, (),
                ('The scheduled job was not automatically retried. Review its saved checkpoint. ' + str(error))[:2000])
        finally:
            with self._lock:
                if self._running is ticket:
                    self._running = None


def prepare_run(service, record, ticket):
    """Called only inside AssistantService.run's existing non-reentrant job lock."""
    if ticket is not None:
        if type(ticket) is not _Ticket or not isinstance(ticket.owner, ScheduleSession):
            raise TypeError('A current explicit schedule-session admission is required.')
        ticket.owner._admit(service, record, ticket)
    elif 'schedule' in record and record['schedule']['state'] not in ('complete', 'disabled'):
        record['schedule'].update(state='disabled',
            message='Ordinary explicit Run/Resume took ownership; this slot will not run automatically.')


def settle_run(record, ticket):
    """Publish with the same final assistant checkpoint, never ahead of outputs."""
    if ticket is None or record.get('schedule', {}).get('state') != 'dispatching':
        return
    schedule = record['schedule']
    current = ticket.owner.manager._now()
    if current < utc(schedule['started']):
        schedule.update(state='attention', finished=None,
            message='The UTC clock moved backwards after admission. The job result is retained; review before any new schedule.')
    else:
        complete = record['status'] == 'complete'
        schedule.update(state='complete' if complete else 'attention', finished=timestamp(current),
            message=('The existing assistant verified all scheduled operations.' if complete else
                     'The single scheduled attempt stopped or failed. Review the saved job and use ordinary Resume.'))
