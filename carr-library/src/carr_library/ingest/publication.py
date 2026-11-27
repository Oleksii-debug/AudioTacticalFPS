"""Bounded read-only verification of a stopped child's publication receipt.

Only an immutable receipt committed by this request's current attempt is
authority. Reconciliation neither replays parsing nor writes catalog state,
and does not depend on the extraction journal's later commit.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import time

from ..durable_state import regular
from ..storage.revisions import CURRENT_SCHEMA_VERSION, _schema_signature, _expected_signature
from .retained_assets import MAX_BYTES, SUFFIXES
from .writer_guard import exclusive_writer, _regular_if_present


@contextmanager
def _read_database(path, expired, max_payload):
    regular(path)
    for suffix in ('-wal', '-shm', '-journal'):
        _regular_if_present(Path(str(path) + suffix))
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                         isolation_level=None, timeout=0)
    try:
        db.row_factory = sqlite3.Row
        # Bound individual fetched strings/rows as well as source-file hashing.
        # The extra allowance covers the small metadata beside a page payload.
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_payload + 256 * 1024)
        db.set_progress_handler(lambda: int(expired()), 1000)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            yield db
        except sqlite3.Error as error:
            if expired():
                raise TimeoutError('Publication reconciliation time limit reached.') from error
            raise
    finally:
        db.set_progress_handler(None, 0)
        db.close()


def completed_request(root, workspace, request_id, *, attempt, timeout=1.0):
    """Return this attempt's verified committed result, or None without a receipt.

    This runs only after the child is reaped. The library is opened read-only,
    writer contention never waits, and hashing/SQLite work cooperatively share
    at most one additional second. A sidecar alone can never establish success.
    """
    if type(timeout) not in (int, float) or not 0 < timeout <= 1:
        raise ValueError('Publication reconciliation is limited to one second.')
    if not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{32}', attempt):
        raise ValueError('Invalid publication recovery attempt.')
    deadline = time.monotonic() + timeout

    def expired():
        return time.monotonic() >= deadline

    def check():
        if expired():
            raise TimeoutError('Publication reconciliation time limit reached.')

    def hash_check():
        check()
        return False

    from ..application import LibraryApp
    from ..pending_backup import _entry, _request, MAX_PAYLOAD
    from .worker import load_request, _publication_state

    check()
    root = Path(root).resolve()
    with exclusive_writer(root, timeout=0):
        check()
        # load_request normally supports creating the request directory. Require
        # it to exist here so verification cannot create a missing namespace.
        regular(root / 'import-requests', directory=True)
        record = load_request(root, request_id)
        check()
        if record.get('workspace') != workspace:
            raise ValueError('Import publication workspace changed.')
        candidate = record.get('publication_candidate')
        if candidate is None:
            return None
        if (record.get('attempt') != attempt or not isinstance(candidate, dict)
                or set(candidate) != {'attempt', 'revision', 'sha256', 'completed', 'manifest'}
                or candidate.get('attempt') != attempt
                or not isinstance(candidate.get('revision'), str)
                or not re.fullmatch('[0-9a-f]{64}', candidate['revision'])
                or not isinstance(candidate.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', candidate['sha256'])
                or type(candidate.get('completed')) is not int or candidate['completed'] < 1
                or not isinstance(candidate.get('manifest'), dict)):
            raise ValueError('Import publication candidate changed.')
        with _read_database(root / 'library.sqlite', expired, MAX_PAYLOAD) as db:
            check()
            if (db.execute('PRAGMA user_version').fetchone()[0] != CURRENT_SCHEMA_VERSION
                    or _schema_signature(db) != _expected_signature(CURRENT_SCHEMA_VERSION)):
                raise ValueError('The library schema has changed.')
            check()
            LibraryApp._authorize(db, workspace)
            _request(record, request_id, str(root), db)
            receipt = db.execute('SELECT request_id,attempt,sha256 FROM import_publication '
                                 'WHERE revision=?', (candidate['revision'],)).fetchone()
            if receipt is None or tuple(receipt[:2]) != (request_id, attempt):
                # A competitor can publish an identical revision after rollback;
                # its receipt cannot complete this stopped request.
                return None
            if receipt['sha256'] != candidate['sha256']:
                raise ValueError('Import publication receipt differs from its candidate.')
            state = _publication_state(db, record, candidate['revision'],
                                       check=check, max_payload=MAX_PAYLOAD)
            if state is None:
                return None
            if state != {key: candidate[key] for key in ('sha256', 'completed', 'manifest')}:
                raise ValueError('Committed import differs from its publication candidate.')
            manifest = state['manifest']
            asset, mime, size = manifest['sha256'], manifest['mime'], manifest['size']
            if mime not in SUFFIXES or type(size) is not int or not 0 < size <= MAX_BYTES:
                raise ValueError('Retained source manifest changed.')
            if mime == 'application/pdf' and record.get('job') != candidate['revision']:
                raise ValueError('The PDF publication does not match its saved job.')
            retained = root / 'originals' / (asset + SUFFIXES[mime])
            regular(retained.parent, directory=True)
            if regular(retained).st_size != size:
                raise ValueError('Retained source size changed.')
            if sum(os.path.lexists(root / 'originals' / (asset + suffix))
                   for suffix in SUFFIXES.values()) != 1:
                raise ValueError('Retained source identity is ambiguous.')
            check()
            if _entry(retained, retained.name, hash_check)['sha256'] != asset:
                raise ValueError('Retained source digest changed.')
            check()
            return dict(state='complete', id=candidate['revision'], workspace=workspace,
                        edition=record['metadata']['edition_id'], asset=asset,
                        completed=state['completed'], total=state['completed'], error=None,
                        verified=mime == 'text/plain', publication_recovered=True,
                        message='Import completed before the worker stopped; the committed source was verified.')
