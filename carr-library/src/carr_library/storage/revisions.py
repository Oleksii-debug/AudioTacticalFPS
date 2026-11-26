"""SQLite revision storage candidate. Catalog IDs are supplied by the caller."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sqlite3
import os
from tempfile import TemporaryDirectory

SCHEMA = '''
CREATE TABLE revision(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, edition TEXT NOT NULL,
 asset TEXT NOT NULL, config TEXT NOT NULL, UNIQUE(workspace, edition, id));
CREATE TABLE page(id INTEGER PRIMARY KEY, revision TEXT NOT NULL REFERENCES revision(id),
 ordinal INTEGER NOT NULL CHECK(ordinal>0), physical INTEGER CHECK(physical>0),
 printed TEXT, verified INTEGER NOT NULL CHECK(verified IN (0,1)), raw TEXT NOT NULL,
 digest TEXT NOT NULL, UNIQUE(revision,ordinal), UNIQUE(revision,physical),
 CHECK(verified=0 OR (printed IS NOT NULL AND physical IS NOT NULL)));
CREATE TABLE sealed(revision TEXT PRIMARY KEY REFERENCES revision(id));
CREATE TRIGGER sealed_bu BEFORE UPDATE ON sealed BEGIN SELECT RAISE(ABORT,'immutable seal'); END;
CREATE TRIGGER sealed_bd BEFORE DELETE ON sealed BEGIN SELECT RAISE(ABORT,'immutable seal'); END;
CREATE TRIGGER page_bi BEFORE INSERT ON page WHEN EXISTS(SELECT 1 FROM sealed WHERE revision=new.revision)
 BEGIN SELECT RAISE(ABORT,'sealed revision'); END;
CREATE TABLE active(workspace TEXT NOT NULL, edition TEXT NOT NULL, revision TEXT NOT NULL REFERENCES sealed(revision),
 PRIMARY KEY(workspace,edition), FOREIGN KEY(workspace,edition,revision)
 REFERENCES revision(workspace,edition,id));
CREATE TABLE indexed(id INTEGER PRIMARY KEY REFERENCES page(id), raw TEXT NOT NULL);
CREATE VIRTUAL TABLE page_fts USING fts5(raw,content='indexed',content_rowid='id',
 tokenize='unicode61 remove_diacritics 0');
CREATE TRIGGER indexed_ai AFTER INSERT ON indexed BEGIN
 INSERT INTO page_fts(rowid,raw) VALUES(new.id,new.raw); END;
CREATE TRIGGER indexed_ad AFTER DELETE ON indexed BEGIN
 INSERT INTO page_fts(page_fts,rowid,raw) VALUES('delete',old.id,old.raw); END;
CREATE TRIGGER indexed_au BEFORE UPDATE ON indexed BEGIN SELECT RAISE(ABORT,'replace index row'); END;
CREATE TRIGGER page_bu BEFORE UPDATE ON page BEGIN SELECT RAISE(ABORT,'immutable page'); END;
CREATE TRIGGER page_bd BEFORE DELETE ON page BEGIN SELECT RAISE(ABORT,'immutable page'); END;
CREATE TRIGGER revision_bu BEFORE UPDATE ON revision BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER revision_bd BEFORE DELETE ON revision BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE VIEW active_page AS SELECT p.*,r.workspace,r.edition,r.asset FROM page p
 JOIN revision r ON r.id=p.revision JOIN active a ON a.revision=r.id;
PRAGMA user_version=1;
'''


def _sidecars(path):
    # Same namespace guard as the CARR-10 restore candidate (PR #31).
    return any(os.path.lexists(str(path) + suffix)
               for suffix in ('-wal', '-shm', '-journal'))


@dataclass(frozen=True)
class Page:
    raw: str
    physical: int | None = None
    printed: str | None = None
    verified: bool = False

class Storage:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            with self.transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    if self.db.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone():
                        raise ValueError('unversioned existing database')
                    # executescript commits implicitly; execute each complete statement instead.
                    sql = ''
                    for line in SCHEMA.splitlines():
                        sql += line + '\n'
                        if sqlite3.complete_statement(sql):
                            self.db.execute(sql)
                            sql = ''
                elif version != 1:
                    raise ValueError('unsupported schema version')
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def publish_revision(self, workspace, edition, revision, asset, config, pages):
        if not all(isinstance(v,str) and v for v in (workspace,edition,revision,config)):
            raise ValueError('missing identity or extraction config')
        if len(asset) != 64 or any(c not in '0123456789abcdef' for c in asset):
            raise ValueError('invalid SHA256')
        with self.transaction():
            self.db.execute('INSERT INTO revision VALUES(?,?,?,?,?)',(revision,workspace,edition,asset,config))
            count = 0
            for count,p in enumerate(pages,1):
                if not isinstance(p,Page) or '\x00' in p.raw:
                    raise ValueError('invalid raw page')
                self.db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) VALUES(?,?,?,?,?,?,?)',
                    (revision,count,p.physical,p.printed,int(p.verified),p.raw,sha256(p.raw.encode()).hexdigest()))
            if not count:
                raise ValueError('empty revision')
            self.db.execute('INSERT INTO sealed VALUES(?)',(revision,))
            self.db.execute('DELETE FROM indexed WHERE id IN (SELECT id FROM active_page WHERE workspace=? AND edition=?)',(workspace,edition))
            self.db.execute('INSERT INTO active VALUES(?,?,?) ON CONFLICT(workspace,edition) DO UPDATE SET revision=excluded.revision',(workspace,edition,revision))
            self.db.execute('INSERT INTO indexed SELECT id,raw FROM page WHERE revision=?',(revision,))

    def get_page(self, workspace, revision, ordinal):
        return self.db.execute('SELECT p.* FROM page p JOIN revision r ON r.id=p.revision WHERE r.workspace=? AND p.revision=? AND p.ordinal=?',(workspace,revision,ordinal)).fetchone()

    def _check(self):
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('database integrity failure')
        for row in self.db.execute('SELECT raw,digest FROM page'):
            if sha256(row[0].encode()).hexdigest()!=row[1]:
                raise ValueError('raw digest mismatch')
        if self.db.execute('SELECT id,raw FROM active_page EXCEPT SELECT id,raw FROM indexed').fetchone() or self.db.execute('SELECT id,raw FROM indexed EXCEPT SELECT id,raw FROM active_page').fetchone():
            raise ValueError('active index content mismatch')
        self.db.execute("INSERT INTO page_fts(page_fts,rank) VALUES('integrity-check',1)")

    def check(self):
        with self.transaction():
            self._check()

    def rebuild(self):
        with self.transaction():
            self.db.execute("INSERT INTO page_fts(page_fts) VALUES('rebuild')")
            self.db.execute('DELETE FROM indexed')
            self.db.execute('INSERT INTO indexed SELECT id,raw FROM active_page')
            self._check()

    def backup(self, path):
        """Publish a checked SQLite snapshot without replacing any existing path.

        Requires a local filesystem supporting hard links. Interrupted processes
        may leave a staging directory, never an incomplete final snapshot.
        The caller must own a stable private parent directory and exclude
        concurrent namespace writers; these checks are not a hostile-directory lock.
        """
        path = Path(path)
        if os.path.lexists(path) or _sidecars(path):
            raise FileExistsError(path)
        with TemporaryDirectory(prefix='.carr-backup-', dir=path.parent) as work:
            staged = Path(work) / 'snapshot.db'
            dst = sqlite3.connect(staged)
            try:
                self.db.backup(dst)
                # A backup of a WAL source may retain the WAL file format.
                # Convert only the private snapshot to a standalone database.
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                    raise ValueError('snapshot journal mode is not DELETE')
            finally:
                dst.close()
            copy = Storage(staged)
            try:
                copy.check()
            finally:
                copy.close()
            if _sidecars(staged):
                raise ValueError('snapshot has sidecars')
            with staged.open('r+b') as file:
                os.fsync(file.fileno())
            # Atomic no-replace publication, including a competing writer.
            # Unsupported filesystems fail closed; no overwrite/copy fallback.
            if _sidecars(path):
                raise FileExistsError(path)
            os.link(staged, path)
