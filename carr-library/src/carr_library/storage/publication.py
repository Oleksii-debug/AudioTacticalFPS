"""Bounded validation for known publication deltas in an owned import session.

This reuses one existing SQLite connection. It is not a hostile-file authenticator:
full startup/backup checks and exact retained-source checks remain authoritative.
Unknown connection writes, schema changes and custom publication hooks require
the existing full check before a transaction can commit.
"""
from contextlib import contextmanager
from hashlib import sha256
import sqlite3

_RECEIPT_INSERT = (
    'INSERT INTO import_publication(revision,request_id,attempt,sha256) '
    'VALUES(?,?,?,?)'
)
_PRIMITIVES = (type(None), str, bytes, int, float, bool)


def _version(db):
    return (
        db.total_changes,
        db.execute('PRAGMA data_version').fetchone()[0],
        db.execute('PRAGMA schema_version').fetchone()[0],
    )


def _parameters(values):
    if type(values) not in (tuple, list):
        raise ValueError('Publication SQL parameters must be a bounded primitive sequence.')
    if len(values) > 256 or any(type(value) not in _PRIMITIVES for value in values):
        raise ValueError('Publication SQL parameters must be primitive values.')
    return tuple(values)


class _TrackedConnection:
    def __init__(self, owner):
        self.owner = owner
        self.raw = owner.storage.db

    @property
    def in_transaction(self):
        return self.raw.in_transaction

    @property
    def row_factory(self):
        return self.raw.row_factory

    def execute(self, sql, parameters=()):
        if type(sql) is not str:
            raise ValueError('Publication SQL must be a string.')
        values = _parameters(parameters)
        self.owner.observe()
        if any((type(value), sqlite3.PrepareProtocol) in sqlite3.adapters for value in values):
            self.owner.tainted = True
        if self.owner.receipt_only:
            # Catalog construction checks the actual foreign-key setting while
            # assembling the immutable receipt. Only this exact parameter-free
            # read is known; assignments and every other PRAGMA still fall back
            # to the full-store check before this transaction can commit.
            catalog_read = sql == 'PRAGMA foreign_keys' and not values
            if (not sql.lstrip().upper().startswith('SELECT ')
                    and sql != _RECEIPT_INSERT and not catalog_read):
                self.owner.tainted = True
        try:
            return self.raw.execute(sql, values)
        finally:
            self.owner.expected_changes = self.raw.total_changes

    def executemany(self, sql, parameters):
        if type(sql) is not str:
            raise ValueError('Publication SQL must be a string.')
        # Consume lazy input outside SQLite's adapter/step call. A generator
        # performing a raw write must be observed, never adopted as our delta.
        rows = []
        for values in parameters:
            self.owner.observe()
            if len(rows) >= 1024:
                raise ValueError('A publication SQL batch exceeds its bounded allowance.')
            values = _parameters(values)
            if any((type(value), sqlite3.PrepareProtocol) in sqlite3.adapters for value in values):
                self.owner.tainted = True
            rows.append(values)
        self.owner.observe()
        if self.owner.receipt_only:
            self.owner.tainted = True
        try:
            return self.raw.executemany(sql, rows)
        finally:
            self.owner.expected_changes = self.raw.total_changes


class PublicationSession:
    """Validate the full database once, then known committed publication deltas."""
    def __init__(self, storage):
        self.storage = storage
        self.trusted = None
        self.active = False

    def __enter__(self):
        if self.storage.db.in_transaction or getattr(self.storage, '_publication_session', None) is not None:
            raise ValueError('An incremental publication session cannot be nested.')
        with self.storage.transaction():
            self.storage._check()
            stamp = _version(self.storage.db)
        self.trusted = stamp
        self.active = True
        self.storage._publication_session = self
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                # A complete slice closes with the original whole-store check.
                self.storage.check()
        finally:
            self.trusted = None
            self.active = False
            self.storage._publication_session = None

    def prepare(self):
        if not self.active or not self.storage.db.in_transaction:
            raise ValueError('An active publication transaction is required.')
        self.storage._require_pragmas()
        self.storage._check_schema()
        if self.trusted != _version(self.storage.db):
            self.storage._check()

    def check_current(self):
        try:
            with self.storage.transaction():
                self.prepare()
                stamp = _version(self.storage.db)
            self.trusted = stamp
        except BaseException:
            self.trusted = None
            raise


class PublicationTransaction:
    def __init__(self, storage, workspace, edition, revision, asset, config):
        self.storage = storage
        self.identity = (workspace, edition, revision, asset, config)
        self.session = getattr(storage, '_publication_session', None)
        self.db = _TrackedConnection(self) if self.session is not None else storage.db
        self.expected_changes = None
        self.initial_schema = None
        self.tainted = False
        self.receipt_only = False
        self.validated = False

    def observe(self):
        raw = self.storage.db
        if raw.total_changes != self.expected_changes:
            self.tainted = True
        if raw.execute('PRAGMA schema_version').fetchone()[0] != self.initial_schema:
            self.tainted = True

    def require_full_check(self):
        self.tainted = True

    def validate_candidate(self):
        if self.session is None:
            self.storage._check()
        else:
            self.observe()
            if self.tainted:
                self.storage._check()
            else:
                self._check_revision()
            self.expected_changes = self.storage.db.total_changes
        self.validated = True

    @contextmanager
    def receipt(self):
        old = self.receipt_only
        self.receipt_only = True
        try:
            yield self.db
        finally:
            self.receipt_only = old

    def _check_revision(self):
        workspace, edition, revision, asset, config = self.identity
        db = self.storage.db
        self.storage._require_pragmas()
        self.storage._check_schema()
        row = db.execute(
            'SELECT workspace,edition,asset,config FROM revision WHERE id=?',
            (revision,),
        ).fetchone()
        if row is None or tuple(row) != (workspace, edition, asset, config):
            raise ValueError('Published revision identity changed.')
        if not db.execute('SELECT 1 FROM sealed WHERE revision=?', (revision,)).fetchone():
            raise ValueError('Published revision is not sealed.')
        active = db.execute(
            'SELECT revision FROM active WHERE workspace=? AND edition=?',
            (workspace, edition),
        ).fetchone()
        if active is None or active[0] != revision:
            raise ValueError('Published active revision changed.')
        count = 0
        for raw, digest in db.execute('SELECT raw,digest FROM page WHERE revision=?', (revision,)):
            if type(raw) is not str or sha256(raw.encode()).hexdigest() != digest:
                raise ValueError('raw digest mismatch')
            count += 1
        if not count:
            raise ValueError('empty revision')
        self.storage._check_locators(revision)
        # The joined actual IDs are a subset of this revision's unique page IDs.
        # Check each exact value without materializing its whole raw-text set.
        if db.execute("""
            SELECT 1 FROM page p WHERE p.revision=?
              AND NOT EXISTS (
                  SELECT 1 FROM indexed i WHERE i.id=p.id
                    AND (+i.raw COLLATE BINARY) IS (+p.raw COLLATE BINARY)
              )
            LIMIT 1
        """, (revision,)).fetchone():
            raise ValueError('active index content mismatch')
        if db.execute(
                'SELECT 1 FROM indexed i JOIN page p ON p.id=i.id '
                'JOIN revision r ON r.id=p.revision '
                'WHERE r.workspace=? AND r.edition=? AND r.id<>? LIMIT 1',
                (workspace, edition, revision)).fetchone():
            raise ValueError('Previous active pages remain indexed.')
        if not db.execute(
                'SELECT 1 FROM catalog_edition e JOIN catalog_asset a ON a.digest=e.asset '
                'WHERE e.workspace=? AND e.id=? AND e.asset=?',
                (workspace, edition, asset)).fetchone():
            raise ValueError('Published catalog source changed.')
        if not db.execute(
                'SELECT 1 FROM source_metadata WHERE workspace=? AND edition=? AND revision=?',
                (workspace, edition, revision)).fetchone():
            raise ValueError('Published source metadata is missing.')
        if db.execute(
                'SELECT 1 FROM source_page_review v JOIN page p '
                'ON p.revision=v.revision AND p.physical=v.physical '
                'WHERE v.revision=? AND v.raw_sha256<>p.digest LIMIT 1',
                (revision,)).fetchone():
            raise ValueError('page review text digest mismatch')

    @contextmanager
    def run(self):
        if getattr(self.storage, '_publication_write', None) is not None:
            raise ValueError('Publication verification cannot be nested.')
        try:
            with self.storage.transaction():
                if self.session is not None:
                    # The SQLite writer lock precedes comparison with the last
                    # verified generation, including for uncooperative writers.
                    self.session.prepare()
                    current = _version(self.storage.db)
                    self.expected_changes, _, self.initial_schema = current
                    self.storage._publication_write = self
                try:
                    yield self
                    if not self.validated:
                        raise ValueError('The publication candidate was not validated.')
                    if self.session is not None:
                        self.observe()
                        if self.tainted:
                            self.storage._check()
                        stamp = _version(self.storage.db)
                    elif self.tainted:
                        # A nondefault hook is never trusted merely because it
                        # returned. Preserve its rollback boundary.
                        self.storage._check()
                finally:
                    self.storage._publication_write = None
            if self.session is not None:
                # Use the data_version captured under BEGIN IMMEDIATE. Reading
                # a fresh version after commit could adopt a racing writer.
                self.session.trusted = stamp
        except BaseException:
            if self.session is not None:
                self.session.trusted = None
            raise
