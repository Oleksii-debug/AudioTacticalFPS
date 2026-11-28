"""Real SQLite row publication, rollback, cursor and native-plan controls.

Faults occur only after genuine native INSERTs or a genuine source-cursor row.
No publication result, query row, FTS result, or validation is fabricated.
"""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.catalog.registration import Registration
from carr_library.storage.revisions import CURRENT_SCHEMA, Page, Storage


INSERT = "INSERT INTO indexed(id,raw) VALUES(?,(SELECT raw FROM page WHERE id=?))"
SELECT_REVISION = "SELECT id FROM page WHERE revision=?"
SELECT_ACTIVE = "SELECT id FROM active_page"
TEMP_OPS = {"OpenEphemeral", "OpenAutoindex", "SorterOpen", "SorterInsert"}
SOURCE = (
    "oldtoken — точний Cafe\u0301\r\nfirst.",
    "oldtoken second source.",
    "oldtoken third source.",
    "oldtoken fourth source.",
)


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / "library")
    workspace = app.create_workspace("Actual index publication")
    try:
        with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
            yield app, workspace, storage
    finally:
        assert app.close_search()


def _publish(library, identity, *, edition="one", source=SOURCE, extracted=None):
    app, workspace, storage = library
    data = "\f".join(source).encode("utf-8")
    digest = sha256(data).hexdigest()
    app.assets.retain(data, "text/plain", digest)
    record = Registration(
        "work-" + edition, edition,
        dict(sha256=digest, mime="text/plain", size=len(data)),
        "Actual source " + edition, ("Fixture Author",),
    )
    publisher = _CatalogPublisher(storage, record)
    revision = "revision-" + identity
    publisher.publish_revision(
        workspace, edition, revision, digest, "fixture",
        (Page(raw) for raw in (source if extracted is None else extracted)),
    )
    return revision


def _seed(library):
    old = _publish(library, "old")
    foreign = _publish(
        library, "foreign", edition="foreign",
        source=("foreigntoken — retained exact scope.",),
    )
    return old, foreign


def _snapshot(db):
    # Include the genuine FTS shadow tables, catalog metadata and receipts.
    names = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    assert names and all(name.replace("_", "").isalnum() for name in names)
    return {
        name: tuple(sorted(
            (tuple(row) for row in db.execute('SELECT * FROM "' + name + '"')),
            key=repr))
        for name in names
    }


def _hits(db, token):
    return tuple(row[0] for row in db.execute(
        "SELECT rowid FROM page_fts WHERE page_fts MATCH ? ORDER BY rowid",
        (token,),
    ))


class _ObservedCursor:
    def __init__(self, actual, after_fetch):
        self.actual = actual
        self.after_fetch = after_fetch
        self.count = 0

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.actual)
        self.count += 1
        self.after_fetch(self.count)
        return row

    def close(self):
        self.actual.close()


class _ObservedConnection:
    """Forward every actual native call; observe only the two new ID cursors."""
    def __init__(self, actual, *, after_insert=None, after_fetch=None):
        self.actual = actual
        self.after_insert = after_insert
        self.after_fetch = after_fetch
        self.inserts = 0
        self.selected = []

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def execute(self, sql, parameters=()):
        cursor = self.actual.execute(sql, parameters)
        if sql in (SELECT_REVISION, SELECT_ACTIVE):
            self.selected.append(cursor)
            if self.after_fetch is not None:
                return _ObservedCursor(cursor, self.after_fetch)
        if sql == INSERT:
            self.inserts += 1
            if self.after_insert is not None:
                self.after_insert(self.inserts)
        return cursor


def _assert_source_cursors_closed(observed):
    assert observed.selected
    for cursor in observed.selected:
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()


def test_more_than_1024_current_pages_keep_history_scope_fts_and_known_commits(
        library, monkeypatch):
    _, workspace, storage = library
    source = tuple("oldtoken %d — Cafe\u0301\r\nточні дані." % n for n in range(1027))
    revised = tuple("newtoken %d — exact replacement." % n for n in range(1027))
    calls = []
    real_check = storage._check

    def full_check():
        calls.append(storage.db.total_changes)
        return real_check()

    monkeypatch.setattr(storage, "_check", full_check)
    with storage.incremental_publications():
        old = _publish(library, "many-old", source=source)
        foreign = _publish(
            library, "many-foreign", edition="foreign",
            source=("foreigntoken — unchanged scope.",))
        current = _publish(
            library, "many-current", source=source, extracted=revised)
        assert len(calls) == 1
        assert len(_hits(storage.db, "newtoken")) == 1027
        assert not _hits(storage.db, "oldtoken")
        assert len(_hits(storage.db, "foreigntoken")) == 1
        assert storage.get_page(workspace, old, 1027)["raw"] == source[-1]
        assert storage.get_page(workspace, current, 1027)["raw"] == revised[-1]
        assert storage.get_page(workspace, foreign, 1)["raw"].startswith("foreigntoken")
    assert len(calls) == 2
    expected = tuple(tuple(row) for row in storage.db.execute(
        "SELECT id,raw FROM indexed ORDER BY id"))
    storage.rebuild()
    assert tuple(tuple(row) for row in storage.db.execute(
        "SELECT id,raw FROM indexed ORDER BY id")) == expected
    assert len(_hits(storage.db, "newtoken")) == 1027
    assert len(_hits(storage.db, "foreigntoken")) == 1
    assert not _hits(storage.db, "oldtoken")
    storage.check()


@pytest.mark.parametrize("failure", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_third_real_stage_insert_failure_restores_savepoint_and_closes_cursor(
        library, monkeypatch, failure):
    app, workspace, storage = library
    _seed(library)
    native = storage.db
    before = _snapshot(native)

    def fail_after_third(count):
        if count == 3:
            assert native.execute(
                "SELECT count(*) FROM indexed i JOIN page p ON p.id=i.id "
                "WHERE p.revision='stage-rejected'").fetchone()[0] == 3
            with closing(sqlite3.connect(app.database)) as reader:
                assert _snapshot(reader) == before
            raise failure("after third actual index insertion")

    observed = _ObservedConnection(native, after_insert=fail_after_third)
    digest = sha256("\f".join(SOURCE).encode("utf-8")).hexdigest()
    with monkeypatch.context() as change:
        change.setattr(storage, "db", observed)
        with storage.transaction():
            with pytest.raises(failure, match="third actual"):
                storage.stage_revision(
                    workspace, "one", "stage-rejected", digest, "fixture",
                    [Page("newtoken %d" % n) for n in range(5)])
            assert native.in_transaction
            assert observed.inserts == 3
            _assert_source_cursors_closed(observed)
            assert _snapshot(native) == before
            assert _hits(native, "oldtoken") and _hits(native, "foreigntoken")
            assert not _hits(native, "newtoken")
    assert not native.in_transaction
    storage.check()
    assert _publish(library, "after-stage-failure", edition="accepted")


@pytest.mark.parametrize("failure", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_third_real_tracked_insert_failure_rolls_back_book_and_allows_fresh_admission(
        library, monkeypatch, failure):
    _, _, storage = library
    _seed(library)
    native = storage.db
    before = _snapshot(native)

    def fail_after_third(count):
        if count == 3:
            assert native.execute(
                "SELECT count(*) FROM indexed i JOIN page p ON p.id=i.id "
                "WHERE p.revision='revision-tracked-rejected'").fetchone()[0] == 3
            raise failure("after third actual tracked insertion")

    observed = _ObservedConnection(native, after_insert=fail_after_third)
    with storage.incremental_publications() as session:
        with monkeypatch.context() as change:
            change.setattr(storage, "db", observed)
            with pytest.raises(failure, match="third actual"):
                _publish(library, "tracked-rejected", edition="rejected")
            assert observed.inserts == 3
            _assert_source_cursors_closed(observed)
            assert not native.in_transaction
            assert session.trusted is None
            assert _snapshot(native) == before
        assert _publish(library, "after-tracked-failure", edition="accepted")
    storage.check()


@pytest.mark.parametrize("failure", (RuntimeError, KeyboardInterrupt, SystemExit))
def test_third_real_rebuild_insert_failure_restores_fts_and_closes_cursor(
        library, monkeypatch, failure):
    _, _, storage = library
    _seed(library)
    native = storage.db
    before = _snapshot(native)

    def fail_after_third(count):
        if count == 3:
            assert native.execute("SELECT count(*) FROM indexed").fetchone()[0] == 3
            raise failure("after third actual rebuild insertion")

    observed = _ObservedConnection(native, after_insert=fail_after_third)
    with monkeypatch.context() as change:
        change.setattr(storage, "db", observed)
        with pytest.raises(failure, match="third actual"):
            storage.rebuild()
        assert observed.inserts == 3
        _assert_source_cursors_closed(observed)
        assert not native.in_transaction
        assert _snapshot(native) == before
    storage.check()
    storage.rebuild()
    assert len(_hits(native, "oldtoken")) == len(SOURCE)
    assert len(_hits(native, "foreigntoken")) == 1
    storage.check()


def test_untracked_foreign_write_during_actual_id_cursor_iteration_is_not_adopted(
        library, monkeypatch):
    _, _, storage = library
    _, foreign = _seed(library)
    native = storage.db
    before = _snapshot(native)
    changed = []

    def after_real_row(count):
        if count == 2:
            native.execute(
                "DELETE FROM indexed WHERE id IN (SELECT id FROM page WHERE revision=?)",
                (foreign,))
            assert not _hits(native, "foreigntoken")
            changed.append(count)

    observed = _ObservedConnection(native, after_fetch=after_real_row)
    with storage.incremental_publications() as session:
        with monkeypatch.context() as change:
            change.setattr(storage, "db", observed)
            with pytest.raises(ValueError, match="active index content mismatch"):
                _publish(library, "foreign-damage-rejected", edition="rejected")
            assert changed == [2]
            _assert_source_cursors_closed(observed)
            assert session.trusted is None
            assert _snapshot(native) == before
        assert _publish(library, "after-foreign-damage", edition="accepted")
    storage.check()


def test_actual_publication_and_rebuild_statement_has_bounded_native_insert_plan(
        library, capsys):
    app, _, storage = library
    actual = set()

    def trace(sql):
        # The authored source is small. New INSERT parameters are IDs only.
        if sql.startswith("INSERT INTO indexed(id,raw) VALUES("):
            actual.add(sql)

    storage.db.set_trace_callback(trace)
    try:
        _publish(library, "plan", source=SOURCE[:3])
        storage.rebuild()
    finally:
        storage.db.set_trace_callback(None)
    assert len(actual) == 3
    before = sha256(app.database.read_bytes()).hexdigest()
    changes = storage.db.total_changes
    rows = tuple(tuple(row) for row in storage.db.execute(
        "SELECT id,raw FROM indexed ORDER BY id"))
    reports = []
    storage.db.execute("PRAGMA query_only=ON")
    try:
        for sql in sorted(actual):
            opcodes = [tuple(row) for row in storage.db.execute("EXPLAIN " + sql)]
            assert 1 <= len(opcodes) <= 1024
            assert not any(row[1] in TEMP_OPS for row in opcodes)
            plan = [tuple(row) for row in storage.db.execute("EXPLAIN QUERY PLAN " + sql)]
            assert len(plan) <= 128
            reports.append(dict(
                sql_sha256=sha256(sql.encode("utf-8")).hexdigest(),
                opcode_count=len(opcodes), query_plan=plan))
        old = [tuple(row) for row in storage.db.execute(
            "EXPLAIN INSERT INTO indexed SELECT id,raw FROM page WHERE revision=?",
            ("revision-plan",))]
        assert any(row[1] == "OpenEphemeral" for row in old)
        assert storage.db.total_changes == changes
        assert tuple(tuple(row) for row in storage.db.execute(
            "SELECT id,raw FROM indexed ORDER BY id")) == rows
        assert sha256(app.database.read_bytes()).hexdigest() == before
    finally:
        storage.db.execute("PRAGMA query_only=OFF")
    assert storage.db.total_changes == changes
    assert sha256(app.database.read_bytes()).hexdigest() == before
    output = json.dumps(dict(
        schema="carr-indexed-streaming-native-plan/1",
        sqlite=sqlite3.sqlite_version,
        statements=reports,
        old_select_has_ephemeral=True,
        scope="Actual finite native plan and live row/rollback tests; no Windows scratch-file attribution or large-capacity claim.",
    ), sort_keys=True)
    assert len(output.encode("utf-8")) <= 16384
    with capsys.disabled():
        print("CARR_INDEXED_STREAMING_PLAN " + output, flush=True)
