"""SQLite revision storage candidate. Catalog IDs are supplied by the caller."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import sqlite3
import os
from tempfile import TemporaryDirectory

from ..citations.anchors import Locator
from .research_schema import RESEARCH_SCHEMA
from .academic_schema import ACADEMIC_SCHEMA

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


# Catalog DDL reused from PR53 (blob 5ab88793b8e4041127f88c2a1dd7662bbe42094e).
# V2 is a fresh catalog/revision candidate, not a live migration.
CATALOG_V2 = '''
CREATE TABLE workspace(id TEXT PRIMARY KEY);
CREATE TABLE catalog_asset(
    digest TEXT PRIMARY KEY CHECK(length(digest)=64),
    mime TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(typeof(size)='integer' AND size>0)
);
CREATE TABLE catalog_work(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL,
    title TEXT,
    PRIMARY KEY(workspace,id)
);
CREATE TABLE catalog_author(
    workspace TEXT NOT NULL,
    work TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position>=0),
    name TEXT NOT NULL,
    PRIMARY KEY(workspace,work,position),
    FOREIGN KEY(workspace,work) REFERENCES catalog_work(workspace,id)
);
CREATE TABLE catalog_edition(
    workspace TEXT NOT NULL,
    id TEXT NOT NULL,
    work TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES catalog_asset(digest),
    language TEXT,
    edition TEXT,
    volume TEXT,
    PRIMARY KEY(workspace,id),
    UNIQUE(workspace,id,asset),
    FOREIGN KEY(workspace,work) REFERENCES catalog_work(workspace,id)
);
CREATE TABLE catalog_collection(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    PRIMARY KEY(workspace,id)
);
CREATE UNIQUE INDEX catalog_collection_name ON catalog_collection(workspace,name) WHERE archived=0;
CREATE TABLE catalog_membership(
    workspace TEXT NOT NULL,
    collection TEXT NOT NULL,
    edition TEXT NOT NULL,
    PRIMARY KEY(workspace,collection,edition),
    FOREIGN KEY(workspace,collection) REFERENCES catalog_collection(workspace,id),
    FOREIGN KEY(workspace,edition) REFERENCES catalog_edition(workspace,id)
);
CREATE TRIGGER catalog_edition_identity_immutable BEFORE UPDATE OF workspace,id,work,asset ON catalog_edition
BEGIN SELECT RAISE(ABORT,'immutable edition identity'); END;
CREATE TRIGGER catalog_edition_no_delete BEFORE DELETE ON catalog_edition
BEGIN SELECT RAISE(ABORT,'destructive deletion not supported'); END;
CREATE TRIGGER catalog_asset_no_update BEFORE UPDATE ON catalog_asset
BEGIN SELECT RAISE(ABORT,'immutable asset manifest'); END;
CREATE TRIGGER catalog_asset_no_delete BEFORE DELETE ON catalog_asset
BEGIN SELECT RAISE(ABORT,'destructive deletion not supported'); END;
CREATE TRIGGER catalog_work_identity_immutable BEFORE UPDATE OF workspace,id ON catalog_work
BEGIN SELECT RAISE(ABORT,'immutable work identity'); END;
'''
SCHEMA_V2 = CATALOG_V2 + SCHEMA.replace(
    'UNIQUE(workspace, edition, id));',
    'UNIQUE(workspace, edition, id), FOREIGN KEY(workspace,edition,asset) '
    'REFERENCES catalog_edition(workspace,id,asset));',
).replace('PRAGMA user_version=1;', 'PRAGMA user_version=2;')
SCHEMA_V3 = SCHEMA_V2.replace('PRAGMA user_version=2;', '') + RESEARCH_SCHEMA + '\nPRAGMA user_version=3;\n'
SCHEMA_V4 = SCHEMA_V3.replace('PRAGMA user_version=3;', '') + ACADEMIC_SCHEMA + '\nPRAGMA user_version=4;\n'
IMPORT_PUBLICATION_SCHEMA = '''
CREATE TABLE import_publication(
 revision TEXT PRIMARY KEY NOT NULL REFERENCES sealed(revision),
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt TEXT NOT NULL CHECK(length(attempt)=32 AND attempt NOT GLOB '*[^0-9a-f]*'),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 UNIQUE(request_id,attempt)
);
CREATE TRIGGER import_publication_no_update BEFORE UPDATE ON import_publication
BEGIN SELECT RAISE(ABORT,'immutable import publication'); END;
CREATE TRIGGER import_publication_no_delete BEFORE DELETE ON import_publication
BEGIN SELECT RAISE(ABORT,'immutable import publication'); END;
CREATE TRIGGER import_publication_no_replace BEFORE INSERT ON import_publication
WHEN EXISTS(SELECT 1 FROM import_publication WHERE revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'import publication already exists'); END;
'''
SCHEMA_V5 = SCHEMA_V4.replace('PRAGMA user_version=4;', '') + IMPORT_PUBLICATION_SCHEMA + '\nPRAGMA user_version=5;\n'
IMPORT_REGISTRATION_SCHEMA = '''
CREATE TABLE import_registration(
 revision TEXT NOT NULL REFERENCES sealed(revision),
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt TEXT NOT NULL CHECK(length(attempt)=32 AND attempt NOT GLOB '*[^0-9a-f]*'),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 PRIMARY KEY(request_id,attempt)
);
CREATE TRIGGER import_registration_no_update BEFORE UPDATE ON import_registration
BEGIN SELECT RAISE(ABORT,'immutable import registration'); END;
CREATE TRIGGER import_registration_no_delete BEFORE DELETE ON import_registration
BEGIN SELECT RAISE(ABORT,'immutable import registration'); END;
CREATE TRIGGER import_registration_no_replace BEFORE INSERT ON import_registration
WHEN EXISTS(SELECT 1 FROM import_registration
            WHERE request_id=NEW.request_id AND attempt=NEW.attempt)
BEGIN SELECT RAISE(ABORT,'import registration already exists'); END;
'''
SCHEMA_V6 = SCHEMA_V5.replace('PRAGMA user_version=5;', '') + IMPORT_REGISTRATION_SCHEMA + '\nPRAGMA user_version=6;\n'
CURRENT_SCHEMA_VERSION = 6
SCHEMAS = {1: SCHEMA, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA_V5, 6: SCHEMA_V6}


def _schema_signature(db):
    # Reuse the native schema signature from CARR-10 restore (PR #31).
    return tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))


@lru_cache(maxsize=6)
def _expected_signature(version):
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(SCHEMAS[version])
        return _schema_signature(reference)


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
    def __init__(self, path, *, schema_version=None):
        if schema_version is not None and (type(schema_version) is not int or schema_version not in SCHEMAS):
            raise ValueError('unsupported schema version')
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            # SQLite REPLACE fires implicit delete triggers only with recursion enabled.
            self.db.execute('PRAGMA recursive_triggers=ON')
            with self.transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    if self.db.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone():
                        raise ValueError('unversioned existing database')
                    version = schema_version if schema_version is not None else 1
                    # executescript commits implicitly; execute each complete statement instead.
                    sql = ''
                    for line in SCHEMAS[version].splitlines():
                        sql += line + '\n'
                        if sqlite3.complete_statement(sql):
                            self.db.execute(sql)
                            sql = ''
                elif (version in (3, 4, 5) and schema_version in (4, 5, 6)
                        and version < schema_version):
                    # Validate each exact released layout and its data before
                    # extending it. The existing transaction rolls back the
                    # entire upgrade, including a chained V3 -> V4 -> V5 -> V6.
                    while version < schema_version:
                        self.schema_version = version
                        self._check()
                        migration = {3: ACADEMIC_SCHEMA, 4: IMPORT_PUBLICATION_SCHEMA,
                                     5: IMPORT_REGISTRATION_SCHEMA}[version]
                        sql = ''
                        for line in migration.splitlines():
                            sql += line + '\n'
                            if sqlite3.complete_statement(sql):
                                self.db.execute(sql)
                                sql = ''
                        if sql.strip():
                            raise ValueError('incomplete storage schema migration')
                        version += 1
                        self.db.execute(f'PRAGMA user_version={version}')
                elif version not in SCHEMAS or (schema_version is not None and version != schema_version):
                    raise ValueError('unsupported or mismatched schema version; no live upgrade')
                self.schema_version = version
                self._check_schema()
        except BaseException:
            self.db.close()
            raise

    def _require_pragmas(self):
        if self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise ValueError('foreign keys required')
        if self.db.execute('PRAGMA recursive_triggers').fetchone()[0] != 1:
            raise ValueError('recursive triggers required for immutable revisions')

    @contextmanager
    def transaction(self):
        self._require_pragmas()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            if not self.db.in_transaction:
                raise RuntimeError('publication transaction ended unexpectedly')
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def _check_schema(self):
        if (self.db.execute('PRAGMA user_version').fetchone()[0] != self.schema_version or
                _schema_signature(self.db) != _expected_signature(self.schema_version)):
            raise ValueError('revision storage schema mismatch')

    def publish_revision(self, workspace, edition, revision, asset, config, pages):
        with self.transaction():
            self.stage_revision(workspace, edition, revision, asset, config, pages)

    def stage_revision(self, workspace, edition, revision, asset, config, pages):
        """Stage in the caller's transaction; never commit the outer unit."""
        if not self.db.in_transaction:
            raise ValueError('caller transaction required')
        self._require_pragmas()
        if not all(isinstance(v,str) and v for v in (workspace,edition,revision,config)):
            raise ValueError('missing identity or extraction config')
        if len(asset) != 64 or any(c not in '0123456789abcdef' for c in asset):
            raise ValueError('invalid SHA256')
        self._check_schema()
        self.db.execute('SAVEPOINT stage_revision')
        try:
            self.db.execute('INSERT INTO revision VALUES(?,?,?,?,?)',(revision,workspace,edition,asset,config))
            count = 0
            for count,p in enumerate(pages,1):
                if not isinstance(p,Page) or not isinstance(p.raw,str) or '\x00' in p.raw:
                    raise ValueError('invalid raw page')
                # Validate original Python values before SQLite affinity or int()
                # can turn a malformed locator into an apparently valid one.
                try:
                    Locator('logical_segment' if p.physical is None else 'physical_page',
                            count if p.physical is None else p.physical,
                            p.printed, p.verified)
                except ValueError as exc:
                    raise sqlite3.IntegrityError('invalid source locator') from exc
                self.db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) VALUES(?,?,?,?,?,?,?)',
                    (revision,count,p.physical,p.printed,int(p.verified),p.raw,sha256(p.raw.encode()).hexdigest()))
            if not count:
                raise ValueError('empty revision')
            self._check_locators(revision)
            self.db.execute('INSERT INTO sealed VALUES(?)',(revision,))
            self.db.execute('DELETE FROM indexed WHERE id IN (SELECT id FROM active_page WHERE workspace=? AND edition=?)',(workspace,edition))
            self.db.execute('INSERT INTO active VALUES(?,?,?) ON CONFLICT(workspace,edition) DO UPDATE SET revision=excluded.revision',(workspace,edition,revision))
            self.db.execute('INSERT INTO indexed SELECT id,raw FROM page WHERE revision=?',(revision,))
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK TO stage_revision')
                self.db.execute('RELEASE stage_revision')
            raise
        self.db.execute('RELEASE stage_revision')

    def get_page(self, workspace, revision, ordinal):
        # Only sealed, workspace-scoped pages can serve as historical evidence.
        row = self.db.execute('SELECT p.* FROM page p JOIN revision r ON r.id=p.revision JOIN sealed s ON s.revision=r.id WHERE r.workspace=? AND p.revision=? AND p.ordinal=?',(workspace,revision,ordinal)).fetchone()
        if row is not None and (not isinstance(row['raw'], str) or sha256(row['raw'].encode()).hexdigest() != row['digest']):
            raise ValueError('raw digest mismatch')
        return row

    def _check_locators(self, revision=None):
        # Native SQLite window functions validate sequence and locator kind.
        # Existing schema derives kind from physical IS NULL; no migration here.
        invalid = self.db.execute("""
            WITH ordered AS (
                SELECT ordinal, physical, printed, verified,
                    row_number() OVER w AS expected,
                    lag(physical) OVER w AS previous_physical,
                    lag(physical IS NULL) OVER w AS previous_logical
                FROM page WHERE (? IS NULL OR revision=?)
                WINDOW w AS (PARTITION BY revision ORDER BY ordinal)
            )
            SELECT 1 FROM ordered WHERE ordinal != expected
                OR (physical IS NULL AND (printed IS NOT NULL OR verified != 0))
                OR (physical IS NOT NULL AND typeof(physical) != 'integer')
                OR (printed IS NOT NULL AND (typeof(printed)!='text' OR instr(printed,char(0))>0))
                OR (verified=1 AND (printed IS NULL OR trim(printed, char(9,10,13,32))=''))
                OR (expected>1 AND previous_logical != (physical IS NULL))
                OR (physical IS NOT NULL AND physical <= previous_physical)
            LIMIT 1
        """, (revision, revision)).fetchone()
        if invalid:
            raise sqlite3.IntegrityError('invalid source locator sequence or label')
        # Apply the citation boundary to every authoritative label, including
        # inactive historical revisions checked before backup or recovery.
        for physical, printed in self.db.execute(
                'SELECT physical,printed FROM page WHERE verified=1 AND (? IS NULL OR revision=?)',
                (revision, revision)):
            try:
                Locator('physical_page', physical, printed, True)
            except ValueError as exc:
                raise sqlite3.IntegrityError('invalid verified printed label') from exc

    def _check(self):
        self._check_schema()
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('database integrity failure')
        # Atomic publication never leaves a committed unsealed or empty revision.
        if self.db.execute("""
            SELECT 1 FROM revision r
            WHERE NOT EXISTS (SELECT 1 FROM sealed s WHERE s.revision=r.id)
               OR NOT EXISTS (SELECT 1 FROM page p WHERE p.revision=r.id)
            LIMIT 1
        """).fetchone():
            raise ValueError('incomplete revision')
        # Schema v1 has no archive operation: every published edition has an active pointer.
        if self.db.execute("""
            SELECT 1 FROM revision r WHERE NOT EXISTS (
                SELECT 1 FROM active a WHERE a.workspace=r.workspace AND a.edition=r.edition
            ) LIMIT 1
        """).fetchone():
            raise ValueError('missing active edition')
        self._check_locators()
        for row in self.db.execute('SELECT raw,digest FROM page'):
            if sha256(row[0].encode()).hexdigest()!=row[1]:
                raise ValueError('raw digest mismatch')
        if self.db.execute('SELECT id,raw FROM active_page EXCEPT SELECT id,raw FROM indexed').fetchone() or self.db.execute('SELECT id,raw FROM indexed EXCEPT SELECT id,raw FROM active_page').fetchone():
            raise ValueError('active index content mismatch')
        self.db.execute("INSERT INTO page_fts(page_fts,rank) VALUES('integrity-check',1)")
        if self.schema_version >= 3:
            self._check_research()
        if self.schema_version >= 4:
            from ..research.academic_library import validate_academic_database
            validate_academic_database(self.db)

    def _check_research(self):
        """Check persisted research identities and source slices before backup.

        This validates the stored snapshot, not an external reviewer's authority.
        Citation actions must still use fresh text/metadata attestors and assets.
        """
        from ..citations.anchors import Identity, Locator, Metadata, resolve, validate_span
        from ..research.saved_evidence import _note
        from ..research.span_snapshot import _decode_snapshot
        from ..research.saved_topic_query import _definition
        from ..research.topic_tree import _id, _name, _position

        # Reuse the canonical assembly's workspace-profile invariant with this
        # host's table names. A backup must not silently strand existing sources
        # and evidence behind a missing or malformed library presentation row.
        if self.db.execute('SELECT 1 FROM workspace w LEFT JOIN workspace_meta m '
                'ON m.workspace=w.id WHERE m.workspace IS NULL LIMIT 1').fetchone():
            raise ValueError('workspace profile missing')
        for ws, name, description, archived in self.db.execute(
                'SELECT workspace,name,description,archived FROM workspace_meta'):
            _id(ws)
            if (not isinstance(name, str) or not name.strip() or len(name) > 512
                    or '\x00' in name):
                raise ValueError('invalid workspace name')
            if (not isinstance(description, str) or len(description) > 100000
                    or '\x00' in description):
                raise ValueError('invalid workspace description')
            if type(archived) is not int or archived not in (0, 1):
                raise ValueError('invalid workspace archive state')

        if self.db.execute('''WITH RECURSIVE reachable(workspace,id) AS (
            SELECT workspace,id FROM research_topic WHERE parent IS NULL
            UNION SELECT t.workspace,t.id FROM research_topic t
            JOIN reachable r ON t.workspace=r.workspace AND t.parent=r.id
        ) SELECT workspace,id FROM research_topic
          EXCEPT SELECT workspace,id FROM reachable LIMIT 1''').fetchone():
            raise ValueError('unreachable research topic or cycle')
        for ws, ident, name, position in self.db.execute(
                'SELECT workspace,id,name,position FROM research_topic'):
            _id(ws)
            _id(ident)
            _name(name)
            _position(position)
        for row in self.db.execute('SELECT name,query,field,result_limit FROM research_saved_query'):
            _definition(*row)
        for ws, topic, ident, payload, checksum, note in self.db.execute(
                'SELECT workspace,topic,id,payload,checksum,note FROM research_evidence'):
            _id(ws)
            _id(topic)
            _id(ident)
            _note(note)
            for anchor in validate_span(_decode_snapshot(payload, checksum)):
                if anchor.identity.workspace != ws:
                    raise ValueError('invalid evidence snapshot identity')
                identity, locator = anchor.identity, anchor.locator
                row = self.db.execute('''SELECT r.workspace,r.edition,r.asset,r.id,
                    p.ordinal,p.physical,p.printed,p.verified,p.raw FROM revision r
                    JOIN sealed s ON s.revision=r.id JOIN page p ON p.revision=r.id
                    WHERE r.workspace=? AND r.edition=? AND r.asset=? AND r.id=?
                    AND ((?='physical_page' AND p.physical=?) OR
                         (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
                    (ws, identity.edition_id, identity.asset_sha256, identity.revision,
                     locator.kind, locator.index, locator.kind, locator.index)).fetchone()
                if row is None:
                    raise ValueError('missing sealed evidence source')
                source_id = Identity(*row[:4])
                source_locator = (Locator('physical_page', row[5], row[6], bool(row[7]))
                                  if row[5] is not None else Locator('logical_segment', row[4]))
                resolve(anchor, source_id, source_locator, row[8])
                metadata = self.db.execute('''SELECT author,title,volume,edition_label
                    FROM source_metadata WHERE workspace=? AND edition=? AND revision=?''',
                    (ws, identity.edition_id, identity.revision)).fetchone()
                if metadata is None or Metadata(*metadata) != anchor.metadata:
                    raise ValueError('evidence metadata snapshot drift')
        if self.db.execute('''SELECT 1 FROM source_page_review v JOIN page p
            ON p.revision=v.revision AND p.physical=v.physical
            WHERE v.raw_sha256<>p.digest LIMIT 1''').fetchone():
            raise ValueError('page review text digest mismatch')

    def check(self):
        with self.transaction():
            self._check()

    def rebuild(self):
        with self.transaction():
            self._check_schema()
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
        if self.db.in_transaction:
            raise ValueError('backup forbidden inside publication transaction')
        self._require_pragmas()
        self._check_schema()
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


def migrate_v2_snapshot(source, destination):
    """Create a separately checked V3 library; never replace or upgrade source.

    V1 lacks authoritative catalog bindings and cannot be upgraded by guessing.
    V2 has no research tables, so adding V3 leaves every source/revision intact.
    No extraction, printed-page or bibliographic review is manufactured here.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    if os.path.lexists(destination) or _sidecars(destination):
        raise FileExistsError(destination)
    with TemporaryDirectory(prefix='.carr-migrate-', dir=destination.parent) as work:
        staged = Path(work) / 'library.db'
        original = Storage(source, schema_version=2)
        try:
            original.backup(staged)
        finally:
            original.close()
        with closing(sqlite3.connect(staged, isolation_level=None)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA recursive_triggers=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                sql = ''
                for line in RESEARCH_SCHEMA.splitlines():
                    sql += line + '\n'
                    if sqlite3.complete_statement(sql):
                        db.execute(sql)
                        sql = ''
                db.execute('INSERT INTO workspace_meta(workspace,name) SELECT id,id FROM workspace')
                db.execute('PRAGMA user_version=3')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        upgraded = Storage(staged, schema_version=3)
        try:
            upgraded.backup(destination)
        finally:
            upgraded.close()
    return destination
