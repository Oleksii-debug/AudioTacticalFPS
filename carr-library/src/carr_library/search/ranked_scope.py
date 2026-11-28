"""Complete scoped ranking using the existing FTS5 and MiniLM contracts.

The private SQLite spool holds ordering, never citation authority. Every source
row is read from an authorized snapshot; returned rows are resolved again by
LibraryApp. A disposable per-scope FTS cache updates only changed rows. It is
revalidated against current source bytes before use, including FTS integrity.
No global-library statistics, background executor or new model loader is used.
"""
from contextlib import closing, contextmanager
from hashlib import sha256
from itertools import islice
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from unicodedata import normalize
from uuid import uuid4

from filelock import FileLock, Timeout as LockTimeout

from ..durable_state import _open_json_file, _replace_state, local_path, regular, sync_directory
from .paging import SearchResources, canonical, select_window
from .thematic import _groups, _quote

_APPLICATION_ID = 0x43415253
_VERSION = 1
_CHUNK = 64 * 1024
_CACHE_NAME = re.compile(r"scope-v1-([0-9a-f]{64})\.sqlite\Z")
_BASE_SCHEMA = (
    "CREATE TABLE source (id INTEGER PRIMARY KEY,workspace TEXT NOT NULL,work TEXT NOT NULL,"
    "edition TEXT NOT NULL,revision TEXT NOT NULL,ordinal INTEGER NOT NULL,"
    "digest TEXT NOT NULL,text TEXT NOT NULL)",
    "CREATE INDEX source_order ON source(edition,ordinal,id)",
    """CREATE VIRTUAL TABLE scoped USING fts5(text,content='source',content_rowid='id',
        tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'")""",
)
_ROW_COLUMNS = ("p.id,r.workspace,r.edition,p.revision,r.asset,p.ordinal,"
                "p.physical,p.printed,p.verified,p.raw,p.digest")
# These reads already have at most 100 exact page IDs, or one exact page.
# Preserve that bounded outer loop instead of revisiting the whole workspace.
_CURRENT_PAGE_JOIN = (" FROM page p CROSS JOIN revision r ON r.id=p.revision "
                      "CROSS JOIN active a ON a.workspace=r.workspace "
                      "AND a.edition=r.edition AND a.revision=r.id ")


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _directory(path):
    path = local_path(path, exists=True)
    regular(path, directory=True)
    value = path.stat()
    return value.st_dev, value.st_ino


def _schema(db):
    return tuple(tuple(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))


def _new_index(db):
    for statement in _BASE_SCHEMA:
        db.execute(statement)
    db.execute("PRAGMA application_id=" + str(_APPLICATION_ID))
    db.execute("PRAGMA user_version=" + str(_VERSION))


def _expected_schema():
    with closing(sqlite3.connect(":memory:")) as db:
        _new_index(db)
        return _schema(db)


def _configure(db, resources, maximum, check):
    # Main and attached data pages are finite. SQLite cache size is a working
    # allowance, not a claim of a hard process-RSS or OS temporary-file quota.
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, resources.source_characters * 16 + 64 * 1024)
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA mmap_size=0")
    db.execute("PRAGMA cache_size=-" + str(max(1024, resources.memory_bytes // (8 * 1024))))
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA synchronous=FULL")
    if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise ValueError("Unexpected search-spool journal mode.")
    size = db.execute("PRAGMA page_size").fetchone()[0]
    maximum_pages = maximum // size
    if maximum_pages < 16 or db.execute("PRAGMA max_page_count=" + str(maximum_pages)).fetchone()[0] > maximum_pages:
        raise ValueError("The search index exceeds its selected disk allowance.")

    def interrupt():
        try:
            check()
        except (TimeoutError, InterruptedError):
            return 1
        return 0
    db.set_progress_handler(interrupt, 1000)


def _copy_snapshot(source, destination, maximum, check):
    """Copy one non-following, held cache descriptor into an owned directory."""
    check()
    before = regular(source)
    if before.st_size > maximum:
        raise ValueError("The derived search cache exceeds its disk allowance.")
    parent = _directory(source.parent)
    with _open_json_file(source) as incoming, destination.open("xb") as outgoing:
        opened = os.fstat(incoming.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, "st_file_attributes", 0) & 0x400
                or _stamp(opened) != _stamp(before)):
            raise ValueError("The derived search cache changed before capture.")
        count = 0
        while True:
            check()
            block = incoming.read(min(_CHUNK, before.st_size - count + 1))
            if not block:
                break
            count += len(block)
            if count > before.st_size:
                raise ValueError("The derived search cache grew during capture.")
            outgoing.write(block)
        if (count != before.st_size or _stamp(os.fstat(incoming.fileno())) != _stamp(before)
                or _stamp(regular(source)) != _stamp(before) or _directory(source.parent) != parent):
            raise ValueError("The derived search cache changed during capture.")
        outgoing.flush()
        os.fsync(outgoing.fileno())
    check()


class _IndexCache:
    """A bounded, optional cache of exact per-library/filter FTS content.

    Only this reserved namespace is touched. Unknown entries disable caching;
    a fresh private index remains available. Cached SQL is never executed before
    its exact schema is checked on the private captured copy.
    """
    def __init__(self, app, identity, resources):
        self.root = Path(app.root) / "search-cache"
        self.path = self.root / ("scope-v1-" + identity + ".sqlite")
        self.maximum = resources.disk_bytes // 4
        self.available = True
        self.notice = ""

    @contextmanager
    def locked(self, check):
        check()
        _directory(self.root.parent)
        self.root.mkdir(mode=0o700, exist_ok=True)
        _directory(self.root)
        lock_path = self.root / ".cache.lock"
        if os.path.lexists(lock_path):
            value = regular(lock_path)
            if value.st_size > 4096:
                raise ValueError("Unexpected derived cache lock.")
        with FileLock(lock_path, timeout=0):
            check()
            yield

    def capture(self, destination, check):
        try:
            with self.locked(check):
                if not os.path.lexists(self.path):
                    return False
                _copy_snapshot(self.path, destination, self.maximum, check)
                return True
        except (OSError, ValueError, LockTimeout) as exc:
            if isinstance(exc, (TimeoutError, InterruptedError)) and not isinstance(exc, LockTimeout):
                raise
            self.available = False
            self.notice = "The optional derived index was unavailable; a private index was rebuilt."
            if destination.exists():
                destination.unlink()
            return False

    def publish(self, source, check):
        """Publish only derived bytes; a failed cache save cannot alter sources."""
        if not self.available:
            return
        temporary = None
        try:
            with self.locked(check):
                rows, total = [], 0
                with os.scandir(self.root) as entries:
                    for index, entry in enumerate(entries):
                        check()
                        if index >= 4096:
                            raise ValueError("The derived cache inventory exceeds its bounded metadata allowance.")
                        if entry.name == ".cache.lock":
                            continue
                        if re.fullmatch(r"\.active-[a-z0-9_]{8}", entry.name):
                            regular(self.root / entry.name, directory=True)
                            continue
                        if not _CACHE_NAME.fullmatch(entry.name):
                            raise ValueError("Unexpected entry in the derived search cache.")
                        path = self.root / entry.name
                        info = regular(path)
                        if info.st_size > self.maximum:
                            raise ValueError("A derived cache entry exceeds the selected disk allowance.")
                        rows.append((info.st_mtime_ns, entry.name, path, _stamp(info)))
                        total += info.st_size
                size = regular(source).st_size
                if size > self.maximum:
                    return
                existing = next((r for r in rows if r[2] == self.path), None)
                needed = total - (existing[3][2] if existing else 0) + size
                # Evict only ordinary files in our reserved cache namespace.
                # These files are derived data and never source/publication state.
                for _, _, path, stamp in sorted(rows):
                    if needed <= self.maximum:
                        break
                    if path == self.path:
                        continue
                    check()
                    if _stamp(regular(path)) != stamp:
                        raise ValueError("The derived cache inventory changed.")
                    path.unlink()
                    needed -= stamp[2]
                if needed > self.maximum:
                    return
                temporary = self.root / (".pending-" + uuid4().hex)
                _copy_snapshot(source, temporary, self.maximum, check)
                check()
                if existing and _stamp(regular(self.path)) != existing[3]:
                    raise ValueError("The selected derived cache changed.")
                if not existing and os.path.lexists(self.path):
                    raise ValueError("A derived cache appeared during publication.")
                _replace_state(temporary, self.path)
                temporary = None
                sync_directory(self.root)
        except (OSError, ValueError, LockTimeout) as exc:
            if isinstance(exc, (TimeoutError, InterruptedError)) and not isinstance(exc, LockTimeout):
                raise
            self.notice = "Search completed using the private index; the optional cache was not updated."
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _checked_source(row, workspace, allowed):
    page_id, ws, edition, revision, asset, ordinal, physical, printed, verified, raw, digest = row
    if (ws != workspace or edition not in allowed or type(page_id) is not int or page_id < 1
            or type(ordinal) is not int or ordinal < 1
            or not isinstance(revision, str) or not revision or len(revision) > 256
            or not isinstance(asset, str) or not re.fullmatch("[0-9a-f]{64}", asset)
            or type(verified) is not int or verified not in (0, 1)
            or (physical is not None and (type(physical) is not int or physical < 1))
            or (physical is None and (printed is not None or verified))
            or (verified and (not isinstance(printed, str) or not printed.strip()))
            or not isinstance(raw, str) or "\x00" in raw
            or sha256(raw.encode("utf-8")).hexdigest() != digest):
        raise ValueError("Invalid active source identity, locator or digest.")
    return page_id, ws, allowed[edition], edition, revision, ordinal, digest, normalize("NFC", raw)


def _insert_source(index, value):
    previous = index.execute("SELECT id,workspace,work,edition,revision,ordinal,digest,text "
                             "FROM source WHERE id=?", (value[0],)).fetchone()
    if previous is not None and tuple(previous) == value:
        return False
    if previous is not None:
        index.execute("INSERT INTO scoped(scoped,rowid,text) VALUES('delete',?,?)",
                      (previous[0], previous[-1]))
        index.execute("DELETE FROM source WHERE id=?", (previous[0],))
    index.execute("INSERT INTO source VALUES(?,?,?,?,?,?,?,?)", value)
    index.execute("INSERT INTO scoped(rowid,text) VALUES(?,?)", (value[0], value[-1]))
    return True


def _sync(index, source_db, catalog, workspace, scope, resources, filters, check, report):
    """Reauthorize every current row; update only changed derived FTS entries."""
    index.execute("CREATE TABLE current_source(id INTEGER PRIMARY KEY)")
    index.execute("CREATE TABLE current_edition(id TEXT PRIMARY KEY)")
    current, pages, characters, changed = None, 0, 0, 0
    digest = sha256()
    while True:
        check()
        window = select_window(source_db, catalog, scope=scope, cursor=current,
                               resources=resources, check=check, **filters)
        if window.page_ids:
            sql = "SELECT " + _ROW_COLUMNS + _CURRENT_PAGE_JOIN + \
                  "WHERE a.workspace=? AND p.id IN (" + \
                  ",".join("?" for _ in window.page_ids) + ") ORDER BY r.edition,p.ordinal,p.id"
            cursor = source_db.execute(sql, (workspace, *window.page_ids))
            try:
                count = 0
                for row in cursor:
                    check()
                    value = _checked_source(tuple(row), workspace, window.editions)
                    if row[9]:
                        index.execute("INSERT INTO current_source VALUES(?)", (value[0],))
                        changed += _insert_source(index, value)
                    index.execute("INSERT OR IGNORE INTO current_edition VALUES(?)", (value[3],))
                    digest.update(canonical(value[:-1]).encode("utf-8"))
                    digest.update(b"\n")
                    pages += 1
                    count += 1
                    characters += len(row[9])
                if count != len(window.page_ids):
                    raise ValueError("The authorized source window changed during indexing.")
            finally:
                cursor.close()
        if report is not None:
            report(f"Checked {pages} source pages; updated {changed} derived index rows.")
        check()
        current = window.next_cursor
        if current is None:
            break
    # Remove no-longer-authorized rows before either MATCH or BM25 is evaluated.
    removed = index.execute("SELECT count(*) FROM source AS s LEFT JOIN current_source AS c "
                            "ON c.id=s.id WHERE c.id IS NULL").fetchone()[0]
    index.execute("INSERT INTO scoped(scoped,rowid,text) SELECT 'delete',s.id,s.text "
                  "FROM source AS s LEFT JOIN current_source AS c ON c.id=s.id WHERE c.id IS NULL")
    index.execute("DELETE FROM source WHERE id NOT IN (SELECT id FROM current_source)")
    changed += removed
    editions = index.execute("SELECT count(*) FROM current_edition").fetchone()[0]
    index.execute("DROP TABLE current_source")
    index.execute("DROP TABLE current_edition")
    check()
    # rank=1 compares the FTS index with the external content, not just itself.
    index.execute("INSERT INTO scoped(scoped,rank) VALUES('integrity-check',1)")
    check()
    return pages, editions, characters, digest.hexdigest(), changed


def _lexical(index, query, check):
    groups = _groups(query)
    index.execute("CREATE TABLE lexraw(id INTEGER PRIMARY KEY,score REAL NOT NULL,"
                  "edition TEXT NOT NULL,revision TEXT NOT NULL,pagekey TEXT NOT NULL,work TEXT NOT NULL)")
    index.execute("CREATE INDEX lexical_order ON lexraw(score,edition,revision,pagekey)")
    index.execute("CREATE TABLE lexsort(id INTEGER PRIMARY KEY,kind INTEGER NOT NULL,"
                  "position INTEGER NOT NULL,score REAL NOT NULL)")
    index.execute("CREATE INDEX lexical_diversity ON lexsort(kind,position)")
    index.execute("CREATE TABLE lex(id INTEGER PRIMARY KEY,position INTEGER UNIQUE NOT NULL,score REAL NOT NULL)")
    index.execute("CREATE TABLE seen_work(id TEXT PRIMARY KEY)")
    if groups:
        match = " AND ".join("(" + " OR ".join(map(_quote, group)) + ")" for group in groups)
        rows = index.execute("SELECT s.id,bm25(scoped),s.edition,s.revision,CAST(s.id AS TEXT),s.work "
                             "FROM scoped JOIN source AS s ON s.id=scoped.rowid WHERE scoped MATCH ?", (match,))
        try:
            for row in rows:
                check()
                if not math.isfinite(row[1]):
                    raise ValueError("Invalid native BM25 score.")
                index.execute("INSERT INTO lexraw VALUES(?,?,?,?,?,?)", tuple(row))
        finally:
            rows.close()
        rows = index.execute("SELECT id,score,work FROM lexraw INDEXED BY lexical_order "
                             "ORDER BY score,edition,revision,pagekey")
        try:
            for position, (page_id, score, work) in enumerate(rows, 1):
                check()
                first = index.execute("INSERT OR IGNORE INTO seen_work VALUES(?)", (work,)).rowcount == 1
                index.execute("INSERT INTO lexsort VALUES(?,?,?,?)", (page_id, 0 if first else 1, position, -score))
        finally:
            rows.close()
        rows = index.execute("SELECT id,score FROM lexsort INDEXED BY lexical_diversity ORDER BY kind,position")
        try:
            for position, (page_id, score) in enumerate(rows, 1):
                check()
                index.execute("INSERT INTO lex VALUES(?,?,?)", (page_id, position, score))
        finally:
            rows.close()
    return bool(groups)


def _semantic(index, source_db, workspace, query, ranker, cache, budget, report):
    """Score every authorized source through the existing bounded native batch."""
    from ..semantic import OfflineSemanticRanker
    from .semantic_windows import TOKEN_LIMIT, _encoding, iter_source_windows
    if not isinstance(ranker, OfflineSemanticRanker):
        raise ValueError("An existing verified local ranker is required.")
    index.execute("CREATE TABLE semantic_best(id INTEGER PRIMARY KEY,score REAL NOT NULL,"
                  "edition TEXT NOT NULL,revision TEXT NOT NULL,windowkey TEXT NOT NULL,"
                  "start INTEGER NOT NULL,end INTEGER NOT NULL)")
    index.execute("CREATE INDEX semantic_order ON semantic_best(score DESC,edition,revision,windowkey)")
    index.execute("CREATE TABLE sem(id INTEGER PRIMARY KEY,position INTEGER UNIQUE NOT NULL,"
                  "score REAL NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL)")
    if _encoding(ranker.tokenizer, query)[0] > TOKEN_LIMIT:
        if report is not None:
            report("The query exceeds the model's complete token allowance; full-scope keyword ranking used.")
        return False, False
    batch, lookup = [], {}
    completed = 0

    def flush():
        nonlocal completed
        if not batch:
            return
        budget.check()
        ranked = ranker.scores(query, tuple(batch), cancelled=budget.stopped,
                               timeout=budget.remaining(), cache=cache)
        budget.check()
        seen = set()
        for key, score in ranked:
            if key not in lookup or key in seen or type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("The model returned an unauthorized or invalid scored locator.")
            seen.add(key)
            page_id, edition, revision, a, b = lookup[key]
            previous = index.execute("SELECT score,windowkey FROM semantic_best WHERE id=?", (page_id,)).fetchone()
            if previous is None or (-score, key[-1]) < (-previous[0], previous[1]):
                index.execute("INSERT OR REPLACE INTO semantic_best VALUES(?,?,?,?,?,?,?)",
                              (page_id, score, edition, revision, key[-1], a, b))
        completed += len(batch)
        batch.clear()
        lookup.clear()
        if report is not None:
            report(f"Scored {completed} complete source windows with the selected local model.")
        budget.check()

    rows = index.execute("SELECT id,edition,revision,digest FROM source INDEXED BY source_order "
                         "ORDER BY edition,ordinal,id")
    try:
        for page_id, edition, revision, digest in rows:
            budget.check()
            row = source_db.execute("SELECT p.raw,p.digest" + _CURRENT_PAGE_JOIN +
                                    "WHERE a.workspace=? AND p.id=? AND a.edition=? AND p.revision=?",
                                    (workspace, page_id, edition, revision)).fetchone()
            if row is None or row[1] != digest or sha256(row[0].encode("utf-8")).hexdigest() != digest:
                raise ValueError("An authorized source changed before model scoring.")
            raw = row[0]
            if not raw:
                continue
            for a, b in iter_source_windows(ranker.tokenizer, raw, check=budget.check):
                identity = "window-v1:" + sha256(repr((workspace, edition, revision, str(page_id),
                                                       digest, a, b)).encode()).hexdigest()
                key = workspace, edition, revision, identity
                batch.append((key, raw[a:b]))
                lookup[key] = page_id, edition, revision, a, b
                if len(batch) == 500:
                    flush()
        flush()
    finally:
        rows.close()
    rows = index.execute("SELECT id,score,start,end FROM semantic_best INDEXED BY semantic_order "
                         "ORDER BY score DESC,edition,revision,windowkey")
    try:
        for position, (page_id, score, a, b) in enumerate(rows, 1):
            budget.check()
            index.execute("INSERT INTO sem VALUES(?,?,?,?,?)", (page_id, position, score, a, b))
    finally:
        rows.close()
    return index.execute("SELECT 1 FROM sem LIMIT 1").fetchone() is not None, True


def _fuse(index, check):
    """The current RRF/work/edition policy, streamed through indexed tables."""
    index.execute("CREATE TABLE fused(id INTEGER PRIMARY KEY,score REAL NOT NULL,"
                  "edition TEXT NOT NULL,revision TEXT NOT NULL,pagekey TEXT NOT NULL,work TEXT NOT NULL)")
    index.execute("CREATE INDEX fused_order ON fused(score DESC,edition,revision,pagekey)")
    # This UNION yields keys only; each input is a primary-key table. No source
    # text or unbounded Python list participates in the merge.
    rows = index.execute("SELECT id FROM lex UNION ALL SELECT s.id FROM sem AS s "
                         "LEFT JOIN lex AS l ON l.id=s.id WHERE l.id IS NULL")
    try:
        for (page_id,) in rows:
            check()
            lex = index.execute("SELECT position FROM lex WHERE id=?", (page_id,)).fetchone()
            sem = index.execute("SELECT position FROM sem WHERE id=?", (page_id,)).fetchone()
            source = index.execute("SELECT edition,revision,work FROM source WHERE id=?", (page_id,)).fetchone()
            score = (1.0 / (60 + lex[0]) if lex else 0.0) + (1.0 / (60 + sem[0]) if sem else 0.0)
            index.execute("INSERT INTO fused VALUES(?,?,?,?,?,?)",
                          (page_id, score, source[0], source[1], str(page_id), source[2]))
    finally:
        rows.close()
    index.execute("DELETE FROM seen_work")
    index.execute("CREATE TABLE seen_edition(id TEXT PRIMARY KEY)")
    index.execute("CREATE TABLE fused_sort(id INTEGER PRIMARY KEY,kind INTEGER NOT NULL,"
                  "position INTEGER NOT NULL,score REAL NOT NULL)")
    index.execute("CREATE INDEX final_diversity ON fused_sort(kind,position)")
    rows = index.execute("SELECT id,score,edition,work FROM fused INDEXED BY fused_order "
                         "ORDER BY score DESC,edition,revision,pagekey")
    try:
        for position, (page_id, score, edition, work) in enumerate(rows, 1):
            check()
            first = index.execute("INSERT OR IGNORE INTO seen_work VALUES(?)", (work,)).rowcount == 1
            if first:
                index.execute("INSERT OR IGNORE INTO seen_edition VALUES(?)", (edition,))
            index.execute("INSERT INTO fused_sort VALUES(?,?,?,?)",
                          (page_id, 0 if first else 3, position, score))
    finally:
        rows.close()
    position = 0
    while True:
        check()
        row = index.execute("SELECT f.id,s.edition,f.position FROM fused_sort AS f INDEXED BY final_diversity "
                            "JOIN source AS s ON s.id=f.id WHERE f.kind=3 AND f.position>? "
                            "ORDER BY f.position LIMIT 1", (position,)).fetchone()
        if row is None:
            break
        page_id, edition, position = row
        first = index.execute("INSERT OR IGNORE INTO seen_edition VALUES(?)", (edition,)).rowcount == 1
        index.execute("UPDATE fused_sort SET kind=? WHERE id=?", (1 if first else 2, page_id))


class RankedScope:
    """One completed private order, retained by the existing cursor owner."""
    def __init__(self, app, resources, *, available_bytes):
        self.resources = resources
        self.token = uuid4().hex
        self._closed = False
        root = Path(app.root) / "search-cache"
        _directory(root.parent)
        root.mkdir(mode=0o700, exist_ok=True)
        _directory(root)
        self._temporary = tempfile.TemporaryDirectory(prefix=".active-", dir=root)
        self.directory = Path(self._temporary.name)
        self._directory_identity = _directory(self.directory)
        self._result_stamp = None
        self._result_db = None
        self.path = self.directory / "results.sqlite"
        self.index_path = self.directory / "index.sqlite"
        self._available = available_bytes
        self.total = self.source_pages = self.source_editions = self.source_characters = 0
        self.identity = ""
        self.changed_rows = 0
        self.index_bytes = 0
        self.notice = ""
        self.semantic = False
        self.windowed = False
        self.model = None
        self.fallback = ""

    @property
    def bytes(self):
        if self._closed:
            return 0
        total = 0
        for name in ("results.sqlite", "results.sqlite-journal", "index.sqlite", "index.sqlite-journal"):
            path = self.directory / name
            if os.path.lexists(path):
                total += regular(path).st_size
        return total

    def build(self, app, source_db, catalog, workspace, query, mode, filters, scope, budget, report=None):
        if mode not in ("thematic", "semantic"):
            raise ValueError("Complete relevance ranking is available for thematic and semantic modes.")
        budget.check()
        cache_identity = sha256(canonical([str(app.root), workspace, filters, "scoped-unicode61-nfc/1"]).encode()).hexdigest()
        cache = _IndexCache(app, cache_identity, self.resources)
        captured = cache.capture(self.index_path, budget.check)
        if captured:
            try:
                with closing(sqlite3.connect(self.index_path, timeout=0, isolation_level=None)) as candidate:
                    _configure(candidate, self.resources, self._available // 2, budget.check)
                    if (_schema(candidate) != _expected_schema()
                            or candidate.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                            or candidate.execute("PRAGMA user_version").fetchone()[0] != _VERSION):
                        raise ValueError("Unrecognized derived scoped index schema.")
                    candidate.execute("INSERT INTO scoped(scoped,rank) VALUES('integrity-check',1)")
                    budget.check()
            except (sqlite3.DatabaseError, ValueError):
                budget.check()
                self.index_path.unlink()
                captured = False
                cache.available = False
                cache.notice = "The optional derived index failed validation; a private index was rebuilt."
        try:
            with closing(sqlite3.connect(self.index_path, timeout=0, isolation_level=None)) as index:
                _configure(index, self.resources, self._available // 2, budget.check)
                if captured:
                    if (_schema(index) != _expected_schema()
                            or index.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                            or index.execute("PRAGMA user_version").fetchone()[0] != _VERSION):
                        raise ValueError("The derived scoped index has an unexpected schema.")
                else:
                    _new_index(index)
                index.execute("BEGIN IMMEDIATE")
                try:
                    counts = _sync(index, source_db, catalog, workspace, scope, self.resources,
                                   filters, budget.check, report)
                    index.commit()
                except BaseException:
                    index.rollback()
                    raise
            # Persist only the complete source-derived index, never query
            # ordering or a half-built checkpoint. Source authority is unchanged.
            self.index_bytes = regular(self.index_path).st_size
            cache.publish(self.index_path, budget.check)
            self.notice = cache.notice
            self.source_pages, self.source_editions, self.source_characters, self.identity, self.changed_rows = counts
            with closing(sqlite3.connect(self.index_path, timeout=0, isolation_level=None)) as index:
                _configure(index, self.resources, self._available // 2, budget.check)
                index.execute("BEGIN IMMEDIATE")
                has_concepts = _lexical(index, query, budget.check)
                if mode == "semantic" and app.semantic_ranker is not None and has_concepts:
                    self.model = app.semantic_ranker
                    self.semantic, self.windowed = _semantic(index, source_db, workspace, query, self.model,
                                              app.semantic_cache, budget, report)
                    if not self.windowed:
                        self.fallback = "Query exceeds complete model token coverage; "
                elif mode == "semantic" and app.semantic_ranker is None and report is not None:
                    report("Local model unavailable; complete selected-scope keyword ranking used.")
                if self.semantic:
                    _fuse(index, budget.check)
                index.commit()
                result = sqlite3.connect(self.path, timeout=0, isolation_level=None, check_same_thread=False)
                self._result_db = result
                try:
                    _configure(result, self.resources, self._available // 4, budget.check)
                    result.execute("CREATE TABLE result (position INTEGER PRIMARY KEY,page_id INTEGER NOT NULL,"
                                   "score REAL NOT NULL,start INTEGER,end INTEGER,lexical INTEGER NOT NULL)")
                    result.execute("BEGIN IMMEDIATE")
                    sql = ("SELECT id,score FROM fused_sort INDEXED BY final_diversity ORDER BY kind,position"
                           if self.semantic else "SELECT id,score FROM lex ORDER BY position")
                    rows = index.execute(sql)
                    try:
                        for position, (page_id, score) in enumerate(rows, 1):
                            budget.check()
                            lexical = index.execute("SELECT 1 FROM lex WHERE id=?", (page_id,)).fetchone() is not None
                            span = index.execute("SELECT start,end FROM sem WHERE id=?", (page_id,)).fetchone() if self.semantic else None
                            a, b = span if span is not None else (None, None)
                            result.execute("INSERT INTO result VALUES(?,?,?,?,?,?)",
                                           (position, page_id, score, a, b, int(lexical)))
                            self.total = position
                    finally:
                        rows.close()
                    budget.check()
                    result.commit()
                    result.execute("PRAGMA query_only=ON")
                except BaseException:
                    # close() owns this exact handle even after failed build.
                    result.rollback()
                    raise
            self.index_path.unlink()
            self._result_stamp = _stamp(regular(self.path))
            if _directory(self.directory) != self._directory_identity:
                raise ValueError("The private completed search directory changed.")
            budget.check()
            return self
        except sqlite3.OperationalError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff == sqlite3.SQLITE_FULL:
                raise ValueError("The full-scope index reached the selected disk allowance. Increase Disk MiB and retry.") from exc
            if (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff == sqlite3.SQLITE_INTERRUPT:
                budget.check()
                raise TimeoutError("Complete-scope search cancelled or timed out.") from exc
            raise

    def read(self, source_db, catalog, workspace, query, mode, filters, *, offset, limit, budget):
        if self._closed:
            raise ValueError("This completed search order is closed. Restart search.")
        if mode == "semantic" and self.model is not None:
            budget.check()
        if offset >= self.total:
            return (), False
        self._check_result_file()
        hits = []
        result = self._result_db
        if result is None:
            raise ValueError("The completed search handle is no longer retained. Restart search.")
        # This is the same SQLite handle that completed the result transaction.
        # Never reopen a checked pathname where a FIFO/link could be substituted.
        result.set_progress_handler(lambda: int(budget.stopped()), 1000)
        try:
            rows = result.execute("SELECT page_id,score,start,end,lexical FROM result "
                                  "WHERE position>? ORDER BY position LIMIT ?", (offset, limit))
            try:
                for page_id, score, a, b, lexical in rows:
                    budget.check()
                    row = source_db.execute("SELECT " + _ROW_COLUMNS + _CURRENT_PAGE_JOIN +
                                            "WHERE a.workspace=? AND p.id=?", (workspace, page_id)).fetchone()
                    if row is None:
                        raise ValueError("A ranked source is no longer active.")
                    page_id, ws, edition, revision, asset, ordinal, physical, printed, verified, raw, digest = row
                    from ..catalog.read_views import browse_editions
                    admitted = browse_editions(catalog, edition_id=edition, limit=1, **filters)
                    if not admitted or admitted[0].asset != asset:
                        raise ValueError("A ranked source is outside the current selected catalog scope.")
                    _checked_source(tuple(row), workspace, {edition: admitted[0].work_id})
                    if sha256(raw.encode("utf-8")).hexdigest() != digest:
                        raise ValueError("The ranked source digest changed.")
                    if lexical:
                        a, b = 0, len(raw)
                        if self.windowed:
                            a, b = _excerpt(self.model.tokenizer, raw, query, self.directory,
                                            self.resources, budget, (workspace, edition, revision, str(page_id), digest))
                    if a is None or b is None or not 0 <= a < b <= len(raw):
                        raise ValueError("Invalid completed ranked source span.")
                    reason = ("hybrid RRF rank over the complete selected scope; not semantic verification"
                              if self.semantic else
                              "lexical BM25 over the complete selected scope; not semantic verification")
                    if mode == "semantic" and self.model is None:
                        reason = "Semantic model unavailable; " + reason
                    elif self.fallback:
                        reason = self.fallback + reason
                    hits.append(dict(workspace_id=ws, edition_id=edition, revision_id=revision,
                        asset_sha256=asset, page_id=page_id, segment_index=ordinal if physical is None else None,
                        locator_kind="logical_segment" if physical is None else "physical_page",
                        physical_index=physical, printed_label=printed if verified else None,
                        printed_label_verified=bool(verified), raw_text_sha256=digest,
                        start=a, end=b, raw_quote=raw[a:b], score=score, reason=reason))
            finally:
                rows.close()
        finally:
            result.set_progress_handler(None, 0)
        budget.check()
        self._check_result_file()
        return tuple(hits), offset + limit < self.total

    def _check_result_file(self):
        local_path(self.path, exists=True)
        if (_directory(self.directory) != self._directory_identity
                or _stamp(regular(self.path)) != self._result_stamp):
            raise ValueError("The private completed search order changed. Restart search.")

    def close(self):
        if not self._closed:
            if self._result_db is not None:
                self._result_db.close()
                self._result_db = None
            self._temporary.cleanup()
            self._closed = True
        return True


def _excerpt(tokenizer, raw, query, directory, resources, budget, source_identity):
    """Keep current lexical-within-window behavior without retaining all windows."""
    from .semantic_windows import iter_source_windows
    groups = _groups(query)
    if not groups:
        return 0, len(raw)
    path = directory / ("excerpt-" + uuid4().hex + ".sqlite")
    try:
        with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as db:
            _configure(db, resources, resources.disk_bytes // 8, budget.check)
            db.execute("CREATE VIRTUAL TABLE excerpt USING fts5(text,start UNINDEXED,end UNINDEXED,windowkey UNINDEXED,"
                       """tokenize="unicode61 remove_diacritics 0 categories 'L* N* Co M*'")""")
            db.execute("BEGIN IMMEDIATE")
            for a, b in iter_source_windows(tokenizer, raw, check=budget.check):
                key = "window-v1:" + sha256(repr((*source_identity, a, b)).encode()).hexdigest()
                db.execute("INSERT INTO excerpt(text,start,end,windowkey) VALUES(?,?,?,?)",
                           (normalize("NFC", raw[a:b]), a, b, key))
            db.commit()
            match = " AND ".join("(" + " OR ".join(map(_quote, group)) + ")" for group in groups)
            best = None
            for score, key, a, b in db.execute("SELECT bm25(excerpt),windowkey,start,end FROM excerpt WHERE excerpt MATCH ?", (match,)):
                budget.check()
                candidate = score, key, a, b
                if best is None or candidate < best:
                    best = candidate
            return (best[2], best[3]) if best is not None else (0, len(raw))
    finally:
        if path.exists():
            path.unlink()
