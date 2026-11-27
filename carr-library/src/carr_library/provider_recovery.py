"""Passive recovery of current provider settings and nonsecret usage history.

The live ProfileStore and BudgetLedger stay authoritative. Complete backups
capture their bounded data into immutable JSON records; restore never installs
a live profile file, ledger database, credential reference or remote approval.
"""
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from .audio.jobs import file_identity, regular_file, verified_reader
from .backup_files import optional_present
from .ingest.restore import RestoreError
from .providers import MAX_REQUEST, ProviderProfile, decode_profile_import


DIRECTORY = 'state/provider-recovery'
SCHEMA = 'carr-provider-recovery/1'
MAX_BYTES = 64 * 1024**2
MAX_DATABASE_BYTES = 128 * 1024**2
MAX_FILES = 256
MAX_COLLECTION_BYTES = 512 * 1024**2
MAX_ROWS = 100000
_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_STATES = frozenset(('reserved', 'uncertain', 'complete', 'cancelled_before_send', 'reconciled_no_charge'))
_OPERATION_FIELDS = ('id', 'job', 'request_hash', 'estimate_micros', 'state',
                     'output_sha256', 'decoded_translation_sha256')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Provider recovery capture cancelled.')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError('Duplicate provider recovery field.')
        result[key] = value
    return result


def _digest(value, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise RestoreError('Invalid provider recovery checksum.')


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RestoreError('Invalid provider recovery operation identity.')


def _ledger(value):
    if value is None:
        return
    if (type(value) is not dict or set(value) != {'approvals', 'operations'}
            or type(value['approvals']) is not list or type(value['operations']) is not list
            or len(value['approvals']) > MAX_ROWS or len(value['operations']) > MAX_ROWS):
        raise RestoreError('Provider usage history exceeds its typed row budget.')
    approvals, operations = set(), set()
    for row in value['approvals']:
        if type(row) is not dict or set(row) != {'id', 'digest'}:
            raise RestoreError('Invalid provider approval fingerprint record.')
        _identity(row['id'])
        _digest(row['digest'])
        if row['id'] in approvals:
            raise RestoreError('Duplicate provider approval fingerprint.')
        approvals.add(row['id'])
    for row in value['operations']:
        if type(row) is not dict or set(row) != set(_OPERATION_FIELDS):
            raise RestoreError('Invalid provider operation history record.')
        _identity(row['id'])
        _identity(row['job'])
        _digest(row['request_hash'])
        _digest(row['output_sha256'], optional=True)
        _digest(row['decoded_translation_sha256'], optional=True)
        if (row['id'] in operations or row['job'] not in approvals
                or type(row['estimate_micros']) is not int or not 0 <= row['estimate_micros'] <= 10**12
                or row['state'] not in _STATES):
            raise RestoreError('Provider usage history has inconsistent authority or operation records.')
        if ((row['state'] == 'complete') != (row['output_sha256'] is not None)
                or row['decoded_translation_sha256'] is not None and row['state'] != 'complete'):
            raise RestoreError('Provider usage history has an inconsistent completion receipt.')
        operations.add(row['id'])


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise RestoreError('Provider recovery record exceeds its byte budget.')
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError()))
        if (type(value) is not dict or set(value) != {'schema', 'source', 'profiles', 'ledger'}
                or value['schema'] != SCHEMA or type(value['source']) is not dict
                or set(value['source']) != {'profiles_file_sha256', 'ledger_file_sha256'}):
            raise RestoreError('Unsupported passive provider recovery record.')
        for key in value['source']:
            _digest(value['source'][key], optional=True)
        profiles, ledger = value['profiles'], value['ledger']
        if (profiles is None) != (value['source']['profiles_file_sha256'] is None):
            raise RestoreError('Provider settings provenance is incomplete.')
        if (ledger is None) != (value['source']['ledger_file_sha256'] is None):
            raise RestoreError('Provider usage provenance is incomplete.')
        if profiles is None and ledger is None:
            raise RestoreError('An empty provider recovery record is not a snapshot.')
        if profiles is not None:
            selected = decode_profile_import(_json(profiles))
            # Imports clear references; a recovery record itself must already
            # contain only disconnected settings, never a usable vault target.
            if profiles != {'version': 1, 'profiles': [
                    {**asdict(item), 'capabilities': list(item.capabilities)} for item in selected]}:
                raise RestoreError('Recovered provider settings contain credential bindings or unsupported fields.')
        _ledger(ledger)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, RestoreError):
            raise
        raise RestoreError('Invalid passive provider recovery data.') from None


def is_state_path(name):
    return isinstance(name, str) and bool(re.fullmatch(
        re.escape(DIRECTORY) + r'/[0-9a-f]{64}\.json', name))


def state_limit(name):
    return MAX_BYTES if is_state_path(name) else 0


def _read(path, maximum, cancelled):
    before = regular_file(path)
    if before.st_nlink != 1 or not 0 < before.st_size <= maximum:
        raise RestoreError('Provider recovery inputs must be bounded regular files without links.')
    chunks, count = [], 0
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        while True:
            _check(cancelled)
            block = stream.read(min(1024 * 1024, maximum + 1 - count))
            if not block:
                break
            chunks.append(block)
            count += len(block)
            if count > maximum:
                raise RestoreError('Provider recovery input exceeded its byte budget.')
    if count != before.st_size:
        raise RestoreError('Provider recovery input changed during capture.')
    return b''.join(chunks)


def read_record(path, *, cancelled=lambda: False):
    raw = _read(Path(path), MAX_BYTES, cancelled)
    return raw, decode(raw)


def state_paths(root, *, cancelled=lambda: False):
    base = Path(root) / DIRECTORY
    if not optional_present(base):
        return ()
    from .audio.jobs import checked_directory
    checked_directory(base)
    found, total = [], 0
    with os.scandir(base) as children:
        for entry in children:
            _check(cancelled)
            path = Path(entry.path)
            if len(found) >= MAX_FILES or not is_state_path(DIRECTORY + '/' + entry.name):
                raise RestoreError('Unknown provider recovery file or snapshot count exceeded.')
            info = regular_file(path)
            if info.st_nlink != 1 or not 0 < info.st_size <= MAX_BYTES:
                raise RestoreError('Invalid provider recovery file.')
            total += info.st_size
            if total > MAX_COLLECTION_BYTES:
                raise RestoreError('Provider recovery history exceeds its collection byte budget.')
            found.append(path)
    return tuple(sorted(found))


def validate_state_tree(root, *, cancelled=lambda: False):
    for path in state_paths(root, cancelled=cancelled):
        raw, _ = read_record(path, cancelled=cancelled)
        if path.stem != sha256(raw).hexdigest():
            raise RestoreError('Provider recovery filename does not bind its exact snapshot bytes.')


def _read_ledger(path, cancelled):
    # The current BudgetLedger uses a normal SQLite DELETE journal. An active
    # or foreign WAL/hot-journal state needs a retry, never an incomplete copy.
    for suffix in ('-journal', '-wal', '-shm'):
        if optional_present(Path(str(path) + suffix)):
            raise RestoreError('Provider usage has active journal state. Stop provider tasks and retry the backup.')
    before = regular_file(path)
    if before.st_nlink != 1 or not 0 < before.st_size <= MAX_DATABASE_BYTES:
        raise RestoreError('Provider usage database exceeds its bounded snapshot contract.')
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('PRAGMA trusted_schema=OFF')
                db.set_progress_handler(lambda: int(bool(cancelled())), 1000)
                db.execute('BEGIN')
                objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
                if set(objects) != {('table', 'approval'), ('table', 'operation')} or len(objects) != 2:
                    raise RestoreError('Unsupported provider usage database schema.')
                expected_approval = [('id', 'TEXT', 0, None, 1), ('digest', 'TEXT', 1, None, 0)]
                expected_operation = [('id', 'TEXT', 0, None, 1), ('job', 'TEXT', 1, None, 0),
                    ('request_hash', 'TEXT', 1, None, 0), ('estimate_micros', 'INTEGER', 1, None, 0),
                    ('state', 'TEXT', 1, None, 0), ('output_sha256', 'TEXT', 0, None, 0)]
                columns = [tuple(row[1:]) for row in db.execute('PRAGMA table_info(operation)')]
                if ([tuple(row[1:]) for row in db.execute('PRAGMA table_info(approval)')] != expected_approval
                        or columns not in (expected_operation,
                            expected_operation + [('decoded_translation_sha256', 'TEXT', 0, None, 0)])):
                    raise RestoreError('Unsupported provider usage column schema.')
                if (db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                        or db.execute('PRAGMA quick_check(1)').fetchone() != ('ok',)):
                    raise RestoreError('Provider usage needs repair or an idle ordinary journal before backup.')
                approvals, operations = [], []
                for row in db.execute('SELECT id,digest FROM approval ORDER BY id'):
                    _check(cancelled)
                    if len(approvals) >= MAX_ROWS:
                        raise RestoreError('Provider approval history exceeds its row budget.')
                    approvals.append(dict(zip(('id', 'digest'), row)))
                decoded = 'decoded_translation_sha256' if len(columns) == 7 else 'NULL'
                for row in db.execute('SELECT id,job,request_hash,estimate_micros,state,output_sha256,'
                                      + decoded + ' FROM operation ORDER BY id'):
                    _check(cancelled)
                    if len(operations) >= MAX_ROWS:
                        raise RestoreError('Provider operation history exceeds its row budget.')
                    operations.append(dict(zip(_OPERATION_FIELDS, row)))
                ledger = {'approvals': approvals, 'operations': operations}
                _ledger(ledger)
                digest, count = sha256(), 0
                while True:
                    _check(cancelled)
                    block = stream.read(min(1024 * 1024, MAX_DATABASE_BYTES + 1 - count))
                    if not block:
                        break
                    count += len(block)
                    if count > MAX_DATABASE_BYTES:
                        raise RestoreError('Provider usage database grew beyond its byte budget.')
                    digest.update(block)
                if count != before.st_size:
                    raise RestoreError('Provider usage database changed during capture.')
                db.rollback()
        except sqlite3.Error:
            _check(cancelled)
            raise RestoreError('Provider usage could not be captured read-only. Stop provider tasks and retry.') from None
    for suffix in ('-journal', '-wal', '-shm'):
        if optional_present(Path(str(path) + suffix)):
            raise RestoreError('Provider usage changed journal state during capture. Retry when idle.')
    return ledger, digest.hexdigest()


def snapshot_live(root, *, cancelled=lambda: False):
    root = Path(root).absolute()
    profiles_path, ledger_path = root / 'api-connections.json', root / 'api-operations.sqlite'
    profiles = ledger = profiles_hash = ledger_hash = None
    if optional_present(profiles_path):
        raw = _read(profiles_path, MAX_REQUEST, cancelled)
        selected = decode_profile_import(raw)
        profiles = {'version': 1, 'profiles': [
            {**asdict(item), 'capabilities': list(item.capabilities)} for item in selected]}
        profiles_hash = sha256(raw).hexdigest()
    if optional_present(ledger_path):
        ledger, ledger_hash = _read_ledger(ledger_path, cancelled)
    else:
        for suffix in ('-journal', '-wal', '-shm'):
            if optional_present(Path(str(ledger_path) + suffix)):
                raise RestoreError('Provider usage has an orphan journal. Repair it before backup.')
    _check(cancelled)
    if profiles is None and ledger is None:
        return None
    raw = _json({'schema': SCHEMA, 'source': {
        'profiles_file_sha256': profiles_hash, 'ledger_file_sha256': ledger_hash},
        'profiles': profiles, 'ledger': ledger})
    decode(raw)
    return raw


def capture_live(root, stage, *, cancelled=lambda: False):
    raw = snapshot_live(root, cancelled=cancelled)
    if raw is None:
        return None
    digest = sha256(raw).hexdigest()
    path = Path(stage) / DIRECTORY / (digest + '.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if optional_present(path):
        existing, _ = read_record(path, cancelled=cancelled)
        if existing != raw:
            raise RestoreError('Conflicting provider recovery snapshot.')
    else:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    validate_state_tree(stage, cancelled=cancelled)
    return raw


def snapshot_entries(raw):
    if raw is None:
        return ()
    decode(raw)
    digest = sha256(raw).hexdigest()
    return ({'path': DIRECTORY + '/' + digest + '.json', 'size_bytes': len(raw), 'sha256': digest},)


def check_live(root, captured, *, cancelled=lambda: False):
    if snapshot_live(root, cancelled=cancelled) != captured:
        raise RestoreError('Provider settings or usage changed during backup. Retry with provider tasks idle.')


def profile_export(value):
    """Return disconnected settings for the existing explicit import review."""
    decode(_json(value))
    return _json(value['profiles'] or {'version': 1, 'profiles': []})


def describe(value):
    decode(_json(value))
    profiles = value['profiles']['profiles'] if value['profiles'] else []
    ledger = value['ledger'] or {'approvals': [], 'operations': []}
    states = Counter(row['state'] for row in ledger['operations'])
    lines = ['Passive provider recovery; no saved remote approval can be resumed.',
        'Disconnected account settings: ' + str(len(profiles)),
        'Historical approval fingerprints: ' + str(len(ledger['approvals'])),
        'Historical operations: ' + str(len(ledger['operations']))]
    lines.extend(state + ': ' + str(states[state]) for state in sorted(states))
    lines.extend(['',
        'Reservation values are estimates, not bills; this ledger does not store their currencies.',
        'The history is never installed as a live budget ledger. Reconnect accounts and review new tasks explicitly.'])
    return '\n'.join(lines)
