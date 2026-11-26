"""Catalog candidate on caller-owned SQLite transaction; no automatic migration.

Work and edition IDs are explicit, not derived from titles or bibliographic hints.
Asset manifests must originate from the bounded intake service (PR20); this
service does not read or store source bytes or attest rights or printed pages.
"""
import re
import sqlite3


def _identity(value):
    if not isinstance(value, str) or not value or len(value) > 200 or '\x00' in value:
        raise ValueError('invalid identity')
    return value


def _metadata(value):
    if value is not None and (not isinstance(value, str) or len(value) > 4000 or '\x00' in value):
        raise ValueError('invalid metadata')
    return value


class Catalog:
    def __init__(self, db: sqlite3.Connection, workspace: str):
        self.db = db
        self.workspace = _identity(workspace)
        if not db.execute('PRAGMA foreign_keys').fetchone()[0]:
            raise ValueError('foreign keys required')
        if not db.execute('SELECT 1 FROM workspace WHERE id=?', (workspace,)).fetchone():
            raise ValueError('unknown workspace')

    def _write(self):
        if not self.db.in_transaction or not self.db.execute('PRAGMA foreign_keys').fetchone()[0]:
            raise ValueError('caller transaction and foreign keys required')

    def add_work(self, work, title=None, authors=()):
        self._write()
        work, title = _identity(work), _metadata(title)
        if not isinstance(authors, (tuple, list)) or len(authors) > 100:
            raise ValueError('invalid authors')
        authors = tuple(_metadata(a) for a in authors)
        if any(not a or not a.strip() for a in authors):
            raise ValueError('empty author')
        self.db.execute('INSERT INTO catalog_work VALUES(?,?,?)', (self.workspace, work, title))
        self.db.executemany('INSERT INTO catalog_author VALUES(?,?,?,?)',
                            ((self.workspace, work, i, a) for i, a in enumerate(authors)))

    def add_edition(self, edition_id, work, manifest, *, language=None, edition=None, volume=None):
        self._write()
        edition_id, work = _identity(edition_id), _identity(work)
        if not self.db.execute('SELECT 1 FROM catalog_work WHERE workspace=? AND id=?',
                               (self.workspace, work)).fetchone():
            raise ValueError('unknown work')
        language, edition, volume = map(_metadata, (language, edition, volume))
        digest, mime, size = (manifest[k] for k in ('sha256', 'mime', 'size'))
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid asset manifest')
        if not isinstance(mime, str) or not mime or len(mime) > 200 or '\x00' in mime or type(size) is not int or size <= 0:
            raise ValueError('invalid asset manifest')
        # Savepoint keeps the manifest + edition operation atomic even if the
        # caller catches a duplicate or conflict and commits the outer transaction.
        self.db.execute('SAVEPOINT catalog_register_edition')
        try:
            self.db.execute('INSERT INTO catalog_asset VALUES(?,?,?) ON CONFLICT(digest) DO NOTHING',
                            (digest, mime, size))
            row = self.db.execute('SELECT mime,size FROM catalog_asset WHERE digest=?', (digest,)).fetchone()
            if tuple(row) != (mime, size):
                raise ValueError('inconsistent asset manifest')
            self.db.execute('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)',
                            (self.workspace, edition_id, work, digest, language, edition, volume))
        except BaseException:
            self.db.execute('ROLLBACK TO catalog_register_edition')
            self.db.execute('RELEASE catalog_register_edition')
            raise
        self.db.execute('RELEASE catalog_register_edition')

    def create_collection(self, collection, name):
        self._write()
        collection, name = _identity(collection), _metadata(name)
        if not name or not name.strip():
            raise ValueError('empty collection name')
        self.db.execute('INSERT INTO catalog_collection(workspace,id,name) VALUES(?,?,?)',
                        (self.workspace, collection, name))

    def _active_collection(self, collection):
        if not self.db.execute('SELECT 1 FROM catalog_collection WHERE workspace=? AND id=? AND archived=0',
                               (self.workspace, _identity(collection))).fetchone():
            raise ValueError('unknown or archived collection')

    def assign(self, collection, edition):
        self._write()
        self._active_collection(collection)
        if not self.db.execute('SELECT 1 FROM catalog_edition WHERE workspace=? AND id=?',
                               (self.workspace, _identity(edition))).fetchone():
            raise ValueError('unknown edition')
        self.db.execute('INSERT INTO catalog_membership VALUES(?,?,?) ON CONFLICT DO NOTHING',
                        (self.workspace, collection, edition))

    def remove(self, collection, edition):
        self._write()
        self._active_collection(collection)
        return self.db.execute('DELETE FROM catalog_membership WHERE workspace=? AND collection=? AND edition=?',
                               (self.workspace, collection, _identity(edition))).rowcount == 1

    def archive(self, collection):
        self._write()
        self._active_collection(collection)
        self.db.execute('UPDATE catalog_collection SET archived=1 WHERE workspace=? AND id=?',
                        (self.workspace, collection))

    def editions(self, collection=None):
        if collection is not None:
            self._active_collection(collection)
        rows = self.db.execute('''SELECT e.id,e.work,e.asset,w.title,e.language,e.edition,e.volume
            FROM catalog_edition e JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work
            WHERE e.workspace=? AND (? IS NULL OR EXISTS (
                SELECT 1 FROM catalog_membership m WHERE m.workspace=e.workspace AND m.edition=e.id AND m.collection=?))
            ORDER BY e.id''', (self.workspace, collection, collection))
        return tuple(tuple(r) for r in rows)
