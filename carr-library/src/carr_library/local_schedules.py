"""Foreground, explicitly armed schedules over the existing backup/import services.

There is no scheduler thread, OS task, replacement job queue or automatic restart.
The desktop timer calls has_due (memory only), then the current host worker calls
tick. Live tickets disappear on close, disarm, root change or clock rollback.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import Event, RLock
from uuid import uuid4

from .durable_state import atomic_bytes, encode, local_path, read_json_snapshot, regular
from .local_schedule_model import (
    MAX_BYTES, MAX_HORIZON, MAX_RULES, SCHEMA, rule_id, timestamp, utc, validate_record, validate_result,
)
from .operations._locks import NativeLock

DIRECTORY = 'local-schedules'


def _now():
    return datetime.now(timezone.utc)


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _copy(value):
    return json.loads(encode(value))


def directory_identity(path):
    path = local_path(path, exists=True)
    info = regular(path, directory=True)
    if info.st_ino <= 0:
        raise ValueError('Choose an ordinary local directory with a stable identity.')
    return path, [info.st_dev, info.st_ino]


def _outside(path, root):
    if path.is_relative_to(root) or root.is_relative_to(path):
        raise ValueError('Choose a folder outside the current library and its ancestors.')


@dataclass(frozen=True)
class RuleView:
    record: dict
    sha256: str
    durability_confirmed: bool = True
    file_identity: tuple = ()

    @property
    def id(self):
        return self.record['id']


class ScheduleStore:
    """Bounded policy/last-attempt metadata; existing domain journals own work."""
    def __init__(self, app, *, clock=_now):
        if not callable(clock):
            raise TypeError('Use a UTC schedule clock.')
        self.app, self.clock = app, clock
        self.root, self.root_identity = directory_identity(app.root)
        self.directory = self.root / 'state' / DIRECTORY
        self.directory_identity = None
        if os.path.lexists(self.directory):
            _, self.directory_identity = directory_identity(self.directory)

    def current(self):
        path, identity = directory_identity(self.app.root)
        if path != self.root or identity != self.root_identity:
            raise PermissionError('The library directory changed. Reopen and explicitly review local schedules.')
        return path

    def _directory(self, *, create=False):
        self.current()
        if not os.path.lexists(self.directory):
            if self.directory_identity is not None:
                raise PermissionError('The local schedule directory was replaced or removed.')
            if not create:
                return None
            local_path(self.directory)
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path, identity = directory_identity(self.directory)
        if self.directory_identity is not None and identity != self.directory_identity:
            raise PermissionError('The local schedule directory changed.')
        self.directory_identity = identity
        self.current()
        return path

    def _inventory(self):
        directory = self._directory()
        if directory is None:
            return ()
        rows = []
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_RULES * 4 + 64:
                    raise ValueError('The local schedule namespace exceeds its bounded allowance.')
                regular(Path(entry.path))
                if entry.name == '.catalog.lock' or re.fullmatch(r'\.pending-[a-zA-Z0-9_]{8}', entry.name):
                    continue
                if not re.fullmatch(r'[0-9a-f]{32}\.json', entry.name):
                    raise ValueError('Unknown file in the local schedule namespace.')
                rows.append(Path(entry.path))
                if len(rows) > MAX_RULES:
                    raise ValueError('Too many local schedule policies.')
        self._directory()
        return tuple(sorted(rows))

    @contextmanager
    def locked(self, *, create=False):
        directory = self._directory(create=create)
        if directory is None:
            raise FileNotFoundError('No local schedule policies are saved.')
        lock = NativeLock(directory / '.catalog.lock')
        if not lock.acquire():
            raise RuntimeError('A local schedule is already running or being edited.')
        try:
            self._directory()
            self._inventory()
            yield
        finally:
            lock.release()

    def get(self, identity):
        rule_id(identity)
        directory = self._directory()
        if directory is None:
            raise FileNotFoundError('The selected local schedule is unavailable.')
        self._inventory()
        path = directory / (identity + '.json')
        before = _file_identity(regular(path))
        value, checksum = read_json_snapshot(path, max_bytes=MAX_BYTES)
        after = _file_identity(regular(path))
        if before != after:
            raise PermissionError('The saved schedule changed while it was reviewed.')
        validate_record(value, identity_hint=identity)
        self._directory()
        return RuleView(_copy(value), checksum, True, after)

    def list(self):
        return tuple(self.get(path.stem) for path in self._inventory())

    def _write(self, value, *, expected=None):
        validate_record(value)
        raw = encode(value)
        if len(raw) > MAX_BYTES:
            raise ValueError('A local schedule exceeds its metadata allowance.')
        path = self._directory(create=True) / (value['id'] + '.json')
        if expected is not None:
            if self.get(value['id']).sha256 != expected:
                raise PermissionError('The reviewed local schedule changed.')
        elif os.path.lexists(path):
            raise FileExistsError(path)
        self.current()
        try:
            atomic_bytes(path, raw, replace=expected is not None)
        except FileExistsError:
            raise
        except OSError:
            # A visible matching commit is an actual saved policy, but uncertain
            # directory sync never authorizes the next scheduled side effect.
            current, actual = read_json_snapshot(path, max_bytes=MAX_BYTES)
            if actual != sha256(raw).hexdigest() or encode(current) != raw:
                raise
            return RuleView(_copy(value), actual, False, _file_identity(regular(path)))
        return RuleView(_copy(value), sha256(raw).hexdigest(), True, _file_identity(regular(path)))

    def _bound(self, record):
        self.current()
        if (record['data_dir'] != str(self.root)
                or record['root_identity'] != self.root_identity):
            raise PermissionError('This policy was restored or moved. Save a newly reviewed rule for this library.')

    def _target(self, record, *, authorize=True):
        self._bound(record)
        path, identity = directory_identity(record['target'])
        if identity != record['target_identity']:
            raise PermissionError('The explicitly selected schedule folder changed. Review it again.')
        _outside(path, self.root)
        if authorize and record['kind'] == 'watch':
            with self.app.read(record['workspace']):
                pass
        return path

    def create(self, kind, title, target, *, first_due, interval_seconds=3600,
               lateness_seconds=3600, workspace=None, recursive=None, collections=(),
               config=None, parent=None, include_generated=False, cancelled=lambda: False):
        from .extract.page_ocr import Config
        if not callable(cancelled):
            raise TypeError('Use the existing cancellation callback.')
        if not isinstance(collections, (list, tuple)) or len(collections) > 128:
            raise ValueError('Select a bounded explicit collection list.')
        created = utc(self.clock())
        due = utc(first_due)
        if not created <= due <= created + timedelta(seconds=MAX_HORIZON):
            raise ValueError('Choose a UTC first start from now to 366 days ahead.')
        self.current()
        if cancelled():
            raise InterruptedError('Saving the schedule was cancelled.')
        selected, selected_identity = directory_identity(target)
        _outside(selected, self.root)
        if kind == 'backup':
            if workspace is not None or collections or config is not None or recursive is not None and recursive is not False:
                raise ValueError('Backup rules do not accept watched-folder options.')
            from . import incremental_backup
            vault = incremental_backup._vault(selected)
            if vault.head() != parent:
                raise PermissionError('Supply the separately retained current snapshot token, or use a new empty repository.')
            options = dict(workspace=None, recursive=False, collections=[], config=None,
                           parent=parent, include_generated=include_generated)
        elif kind == 'watch':
            if parent is not None or include_generated is not False:
                raise ValueError('Watched folders do not accept snapshot options.')
            recursive = True if recursive is None else recursive
            with self.app.read(workspace):
                pass
            from .catalog.membership import Catalog
            with self.app.read(workspace) as db:
                for collection in collections:
                    Catalog(db, workspace)._active_collection(collection)
            options = dict(workspace=workspace, recursive=recursive, collections=list(collections),
                           config=asdict(config or Config()), parent=None, include_generated=False)
        else:
            raise ValueError('Choose backup or watch.')
        record = dict(schema=SCHEMA, id=uuid4().hex, title=title, data_dir=str(self.root),
            root_identity=list(self.root_identity), kind=kind, target=str(selected),
            target_identity=selected_identity, created=timestamp(created), next_due=timestamp(due),
            interval_seconds=interval_seconds, lateness_seconds=lateness_seconds, disabled=False,
            sequence=0, last=None, **options)
        validate_record(record)
        with self.locked(create=True):
            if len(self._inventory()) >= MAX_RULES:
                raise ValueError('At most 32 explicit local schedule policies are retained.')
            self._target(record)
            if cancelled():
                raise InterruptedError('Saving the schedule was cancelled.')
            return self._write(record)

    def disable(self, identity, *, expected):
        with self.locked():
            view = self.get(identity)
            if view.sha256 != expected:
                raise PermissionError('Refresh the changed policy before disabling it.')
            value = view.record
            value['disabled'] = True
            return self._write(value, expected=expected)

    def acknowledge(self, identity, *, expected, next_due, parent=None, cancelled=lambda: False):
        """Explicit review of unresolved work; never infer or relabel its success."""
        with self.locked():
            view = self.get(identity)
            if view.sha256 != expected:
                raise PermissionError('Refresh the changed policy before reviewing it.')
            value = view.record
            self._bound(value)
            due, current = utc(next_due), utc(self.clock())
            if not current <= due <= current + timedelta(seconds=MAX_HORIZON):
                raise ValueError('Choose a new explicit future UTC start.')
            self._target(value)
            if value['kind'] == 'backup':
                from . import incremental_backup
                vault = incremental_backup._vault(value['target'])
                if vault.head() != parent:
                    raise PermissionError('Review the actual saved snapshot token before continuing backups.')
                if parent is not None:
                    incremental_backup.verify_snapshot(value['target'], parent, cancelled=cancelled)
                value['parent'] = parent
            elif parent is not None:
                raise ValueError('A watched folder has no snapshot parent.')
            if cancelled():
                raise InterruptedError('Schedule review cancelled.')
            if value['last'] is not None:
                value['last'].update(status='reviewed', finished=timestamp(max(current, _now(), utc(value['last']['finished'])
                    if value['last']['finished'] else current)),
                    message='Explicitly reviewed. The prior attempt is not reclassified as successful.')
            value.update(next_due=timestamp(due), disabled=False)
            return self._write(value, expected=expected)


@dataclass
class _Ticket:
    checksum: str
    file_identity: tuple
    due: datetime
    stopped: Event


class ScheduleSession:
    """Only the current process/session can arm reviewed policy hashes."""
    def __init__(self, store, *, scope_guard=lambda: None):
        if not isinstance(store, ScheduleStore) or not callable(scope_guard):
            raise TypeError('Use the existing local schedule store and current host scope.')
        self.store, self.scope_guard = store, scope_guard
        self._guard = RLock()
        self._tickets = {}
        self._closed = False
        self._running = False
        self._generation = 0
        self._last_clock = None

    @property
    def armed(self):
        with self._guard:
            return tuple(sorted(self._tickets))

    def _clock(self):
        value = utc(self.store.clock())
        with self._guard:
            if self._last_clock is not None and value < self._last_clock:
                self.disarm()
                raise InterruptedError('UTC moved backwards. Local schedules were disarmed; review them again.')
            self._last_clock = value
        return value

    def disarm(self, identities=None):
        with self._guard:
            selected = tuple(self._tickets) if identities is None else tuple(identities)
            for identity in selected:
                ticket = self._tickets.pop(identity, None)
                if ticket is not None:
                    ticket.stopped.set()
            self._generation += 1

    def close(self):
        with self._guard:
            self._closed = True
            self.disarm()

    def arm(self, identity, *, expected, cancelled=lambda: False):
        with self._guard:
            if self._closed:
                raise RuntimeError('This local scheduling session is closed.')
            generation = self._generation
        self.scope_guard()
        view = self.store.get(identity)
        record = view.record
        if view.sha256 != expected or record['disabled']:
            raise PermissionError('Review the current, enabled local policy before arming it.')
        if record['last'] is not None and record['last']['status'] in ('dispatching', 'attention'):
            raise PermissionError('An unresolved attempt requires explicit review; automatic replay is disabled.')
        self.store._target(record)
        current = self._clock()
        if current >= utc(record['next_due']) + timedelta(seconds=record['lateness_seconds']):
            raise PermissionError('The approved start window expired. Review a new time.')
        if record['kind'] == 'backup':
            from . import incremental_backup
            vault = incremental_backup._vault(record['target'])
            if vault.head() != record['parent']:
                raise PermissionError('The backup repository advanced outside this policy. Review its saved token.')
            if record['parent'] is not None:
                incremental_backup.verify_snapshot(record['target'], record['parent'], cancelled=cancelled)
        if cancelled():
            raise InterruptedError('Arming the local schedule was cancelled.')
        self.scope_guard()
        self.store._target(record)
        if self.store.get(identity).sha256 != expected:
            raise PermissionError('The policy changed during arming.')
        with self._guard:
            if self._closed or generation != self._generation:
                raise InterruptedError('The scheduling session changed before arming.')
            self._tickets[identity] = _Ticket(expected, view.file_identity, utc(record['next_due']), Event())
        return identity

    def has_due(self):
        """Memory/time only: safe for the existing native event-thread timer."""
        current = self._clock()
        with self._guard:
            return (not self._closed and not self._running
                    and any(ticket.due <= current for ticket in self._tickets.values()))

    def _scan(self, record, stop):
        from .ingest.batch_scan import MAX_EXISTING_ASSETS, scan_paths
        from .operations.broker import ResourceBroker
        from .operations.models import ResourceRequest
        broker = ResourceBroker.for_app(self.store.root, cancelled=stop)
        with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=192 * 1024**2),
                            operation_id='schedule-scan:' + record['id'], cancelled=stop) as lease:
            if stop():
                raise InterruptedError('The watched-folder scan was stopped.')
            self.store._target(record)
            with self.store.app.read(record['workspace']) as db:
                existing = tuple(row[0] for row in db.execute(
                    'SELECT DISTINCT asset FROM catalog_edition WHERE workspace=? LIMIT ?',
                    (record['workspace'], MAX_EXISTING_ASSETS + 1)))
            rows = scan_paths([record['target']], recursive=record['recursive'],
                existing_assets=existing, cancelled=lambda: stop() or bool(lease.checkpoint()),
                skip_unsupported=True)
            self.store._target(record)
            if stop():
                raise InterruptedError('The watched-folder scan was stopped.')
        if any(row['state'] == 'rejected' for row in rows):
            raise ValueError('The watched folder contains a changed, unsupported linked or unreadable input. '
                             'Inspect it in Import Center before rearming.')
        return [row for row in rows if row['state'] == 'queued']

    def _watch(self, record, stop, checkpoint, progress):
        rows = self._scan(record, stop)
        if not rows:
            return dict(batch_id=None, state='complete', counts={}, total=0)
        from .extract.page_ocr import Config
        from .ingest.batches import ImportBatchStore
        store = ImportBatchStore(self.store.app)
        self.store._target(record)
        if stop():
            raise InterruptedError('The watched-folder attempt was stopped before planning.')
        batch = store.plan(record['workspace'], [row['source'] for row in rows],
            collections=record['collections'], recursive=False,
            config=Config(**record['config']), cancelled=stop)
        checkpoint(batch['batch_id'])
        expected = {row['source']: row['manifest'] for row in rows}
        if (len(batch['rows']) != len(expected)
                or any(row['source'] not in expected or row['manifest'] != expected[row['source']]
                       or row['state'] not in ('queued', 'duplicate') for row in batch['rows'])):
            raise PermissionError('The source set changed between scanning and the existing durable batch.')
        self.store._target(record)
        if stop():
            raise InterruptedError('The planned batch was stopped; use Import Center to review it.')
        def observed(value):
            if progress is not None:
                try:
                    progress(value['counts'].get('complete', 0), value['total'], 'importing watched folder')
                except Exception:
                    pass  # Observation cannot cancel a committed import; stop remains independent.
        result = store.run(record['workspace'], batch['batch_id'], cancelled=stop,
                           retry_failed=False, progress=observed)
        return dict(batch_id=result['batch_id'], state=result['state'],
                    counts=result['counts'], total=result['total'])

    def _backup(self, record, stop):
        from . import incremental_backup
        from .operations.broker import ResourceBroker
        from .operations.models import ResourceRequest
        broker = ResourceBroker.for_app(self.store.root, cancelled=stop)
        # Declared CPU/memory admission only. Canonical backup/Vault retain their
        # independent byte, free-space and exact publication checks.
        result, control_cleanup_pending = None, False
        try:
            with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024**2),
                                operation_id='schedule-backup:' + record['id'], cancelled=stop) as lease:
                self.store._target(record)
                result = incremental_backup.create_snapshot(self.store.app, record['target'],
                    parent=record['parent'], include_generated=record['include_generated'],
                    cancelled=lambda: stop() or bool(lease.checkpoint()))
        except Exception:
            if result is None:
                raise
            control_cleanup_pending = True
        keys = ('snapshot_sha256', 'backup_manifest_sha256', 'parent', 'files', 'payload_bytes',
                'published', 'durability_confirmed', 'new_chunks', 'reused_chunks', 'new_bytes', 'reused_bytes')
        return {**{key: result[key] for key in keys},
                'staging_cleanup_pending': result.get('staging_cleanup_pending', False),
                'control_cleanup_pending': control_cleanup_pending}

    def tick(self, *, cancelled=lambda: False, progress=None):
        if not callable(cancelled) or progress is not None and not callable(progress):
            raise TypeError('Use the existing host cancellation and progress callbacks.')
        current = self._clock()
        with self._guard:
            if self._closed or self._running:
                return None
            due = sorted((ticket.due, identity, ticket) for identity, ticket in self._tickets.items()
                         if ticket.due <= current)
            if not due:
                return None
            _, identity, ticket = due[0]
            self._running = True
        outcome = None
        settled = None
        record = None
        def stop():
            if ticket.stopped.is_set() or cancelled():
                return True
            self.scope_guard()
            self.store.current()
            if record is not None:
                self.store._target(record, authorize=False)
            self._clock()
            return False
        try:
            if stop():
                raise InterruptedError('The scheduled operation was stopped before execution.')
            with self.store.locked():
                view = self.store.get(identity)
                if view.sha256 != ticket.checksum or view.file_identity != ticket.file_identity:
                    raise PermissionError('The armed policy was changed or restored before dispatch.')
                record = view.record
                self.store._target(record)
                if record['disabled'] or record['last'] is not None and record['last']['status'] in ('dispatching', 'attention'):
                    raise PermissionError('The armed policy no longer permits this reviewed attempt.')
                current = self._clock()
                record['sequence'] += 1
                if current >= utc(record['next_due']) + timedelta(seconds=record['lateness_seconds']):
                    record['last'] = dict(sequence=record['sequence'], status='attention', started=None,
                        finished=timestamp(current), batch_id=None, result=None,
                        message='The start window expired. Nothing was run; review a new time explicitly.')
                    saved = self.store._write(record, expected=view.sha256)
                    outcome = dict(status='attention', schedule_id=identity, kind=record['kind'],
                                   result=None, recorded=True, saved_rule=asdict(saved), message=record['last']['message'])
                    return outcome
                if stop():
                    raise InterruptedError('The scheduled operation was stopped before its durable claim.')
                record['last'] = dict(sequence=record['sequence'], status='dispatching',
                    started=timestamp(current), finished=None, batch_id=None, result=None, message='')
                saved = self.store._write(record, expected=view.sha256)
                if not saved.durability_confirmed:
                    outcome = dict(status='attention', schedule_id=identity, kind=record['kind'], result=None,
                        recorded=True, saved_rule=asdict(saved), message='The dispatch claim is visible but disk sync is unconfirmed. Nothing was run.')
                    return outcome
                def checkpoint(batch_id):
                    nonlocal saved
                    record['last']['batch_id'] = rule_id(batch_id)
                    saved = self.store._write(record, expected=saved.sha256)
                    if not saved.durability_confirmed:
                        raise RuntimeError('The batch was planned, but its schedule link needs verification before execution.')
                try:
                    if stop():
                        raise InterruptedError('The scheduled operation was stopped after its durable claim.')
                    result = (self._backup(record, stop) if record['kind'] == 'backup'
                              else self._watch(record, stop, checkpoint, progress))
                    validate_result(record['kind'], result)
                except BaseException as error:
                    record['last'].update(status='attention', finished=timestamp(max(current, _now())),
                        message='The attempt did not return verified completion. Inspect its existing domain job before rearming.')
                    try:
                        self.store._write(record, expected=saved.sha256)
                    except Exception:
                        pass  # The earlier dispatch claim remains unresolved and cannot auto-replay.
                    self.disarm((identity,))
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    outcome = dict(status='cancelled' if isinstance(error, InterruptedError) else 'attention',
                        schedule_id=identity, kind=record['kind'], result=None,
                        batch_id=record['last']['batch_id'], recorded=False, message=record['last']['message'])
                    return outcome
                # Only a returned canonical result reaches this point. No user
                # observer, clock, cancellation or source callback follows it.
                finished = max(current, _now())
                complete = record['kind'] == 'backup' or result['state'] == 'complete'
                qualified = (complete and (record['kind'] != 'backup'
                    or result['durability_confirmed'] and not result['staging_cleanup_pending']
                       and not result['control_cleanup_pending']))
                record['last'].update(status='complete' if complete else 'attention',
                    finished=timestamp(finished), result=_copy(result),
                    message='' if qualified else 'The returned result needs attention before another automatic attempt.')
                if record['kind'] == 'backup':
                    record['parent'] = result['snapshot_sha256']
                record['next_due'] = timestamp(finished + timedelta(seconds=record['interval_seconds']))
                outcome = dict(status='complete' if complete else 'attention', schedule_id=identity,
                    kind=record['kind'], result=_copy(result), recorded=False,
                    message=record['last']['message'])
                try:
                    settled = self.store._write(record, expected=saved.sha256)
                    outcome['recorded'] = True
                    outcome['saved_rule'] = asdict(settled)
                    if not settled.durability_confirmed:
                        qualified = False
                        outcome['message'] = 'Completion returned and was saved, but schedule disk synchronization is unconfirmed.'
                except Exception:
                    qualified = False
                    outcome['message'] = 'Completion returned, but its schedule receipt could not be saved. Keep this result; review before retrying.'
                if not qualified:
                    settled = None
                return outcome
        except BaseException as error:
            # Native lock release failures cannot erase a result already returned
            # by the authoritative service, nor authorize its automatic retry.
            if outcome is not None:
                outcome['message'] = (outcome.get('message', '') +
                    ' Local schedule ownership cleanup needs attention.').strip()
                settled = None
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    error.schedule_result = outcome
                    raise
                return outcome
            raise
        finally:
            with self._guard:
                self._running = False
                still_owned = self._tickets.get(identity) is ticket
                if (settled is not None and outcome is not None and outcome['status'] == 'complete'
                        and still_owned and not ticket.stopped.is_set() and not self._closed):
                    ticket.checksum = settled.sha256
                    ticket.file_identity = settled.file_identity
                    ticket.due = utc(settled.record['next_due'])
                else:
                    if still_owned:
                        self._tickets.pop(identity, None)
                    ticket.stopped.set()
