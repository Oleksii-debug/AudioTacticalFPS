"""One fully validated post-worker snapshot for a bounded import slice.

The current Storage transaction performs its unchanged full integrity check.
This context grants no native-launch authority and never replaces the child's
writer/session owner. It is opened only after the actual process family closes.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import threading

from ..durable_state import local_path, regular
from ..storage.revisions import CURRENT_SCHEMA, Storage
from .writer_guard import exclusive_writer

_TOKEN = object()


def _identity(path, *, directory=False):
    info = regular(local_path(path), directory=directory)
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            getattr(info, 'st_file_attributes', 0))


def _generation(db):
    return (db.total_changes, db.execute('PRAGMA data_version').fetchone()[0],
            db.execute('PRAGMA schema_version').fetchone()[0])


class ReconciliationContext:
    """Private exact app/root/connection/snapshot capability, never serializable."""
    def __init__(self, token, app, workspace, storage):
        if token is not _TOKEN:
            raise TypeError('Use the existing post-worker reconciliation context.')
        self.app, self.workspace, self.storage = app, workspace, storage
        self.root = local_path(app.root)
        self.database = self.root / 'library.sqlite'
        self.db = storage.db
        self._thread = threading.get_ident()
        self._active = True
        self._root_identity = _identity(self.root, directory=True)
        self._database_identity = _identity(self.database)
        self._generation = _generation(self.db)
        self.check(self.root, workspace)

    def check(self, root, workspace):
        if (not self._active or threading.get_ident() != self._thread
                or Path(root) != self.root or workspace != self.workspace
                or Path(self.app.root) != self.root or Path(self.app.database) != self.database
                or self.storage.db is not self.db or not self.db.in_transaction):
            raise ValueError('The post-worker reconciliation owner changed.')
        if (_identity(self.root, directory=True) != self._root_identity
                or _identity(self.database) != self._database_identity):
            raise ValueError('The reconciliation root or database was replaced.')
        self.storage._require_pragmas()
        if _generation(self.db) != self._generation:
            raise ValueError('The validated reconciliation generation changed.')
        self.app._authorize(self.db, workspace)
        return self.app, self.db

    def close(self):
        self._active = False


@contextmanager
def reconciliation(app, workspace):
    from ..application import LibraryApp
    if type(app) is not LibraryApp:
        raise TypeError('Reconciliation requires the current exact library application.')
    with exclusive_writer(app.root):
        app._check_database_paths()
        with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
            # The full FTS check uses its existing integrity-check control INSERT.
            # Keep the ordinary BEGIN IMMEDIATE snapshot, including that check,
            # through all exact receipt/source reads and sidecar outcome guards.
            with storage.transaction():
                storage.db.execute('SELECT rootpage FROM sqlite_master LIMIT 1').fetchone()
                storage._check()
                app._authorize(storage.db, workspace)
                owner = ReconciliationContext(_TOKEN, app, workspace, storage)
                try:
                    yield owner
                    owner.check(app.root, workspace)
                finally:
                    owner.close()
