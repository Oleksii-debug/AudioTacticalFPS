"""Actual SQLite parity and rejection controls for streaming index validation.

The old EXCEPT expressions are independent read-only oracles. No generated
catalog, native process, receipt, or successful validation is substituted.
"""
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.catalog.registration import Registration
from carr_library.storage.publication import PublicationTransaction
from carr_library.storage.revisions import CURRENT_SCHEMA, Page, Storage


ASSET = "a" * 64
RAW = "Needle — точний Cafe\u0301\r\nsource."


def _old_full_difference(db):
    expected = "SELECT id,raw FROM active_page"
    actual = "SELECT id,raw FROM indexed"
    return bool(db.execute(expected + " EXCEPT " + actual).fetchone()
                or db.execute(actual + " EXCEPT " + expected).fetchone())


def _old_revision_difference(db, revision):
    expected = "SELECT id,raw FROM page WHERE revision=?"
    actual = ("SELECT i.id,i.raw FROM indexed i JOIN page p ON p.id=i.id "
              "WHERE p.revision=?")
    parameters = (revision, revision)
    return bool(db.execute(expected + " EXCEPT " + actual, parameters).fetchone()
                or db.execute(actual + " EXCEPT " + expected, parameters).fetchone())


def _capture_membership_queries(db, validate):
    observed = []
    db.set_trace_callback(observed.append)
    try:
        validate()
    finally:
        db.set_trace_callback(None)
    queries = []
    for sql in observed:
        normalized = " ".join(sql.upper().split())
        if (normalized.startswith("SELECT ") and "INDEXED" in normalized
                and "RAW" in normalized
                and ("NOT EXISTS" in normalized or " EXCEPT " in normalized)):
            queries.append(sql)
    assert queries, "The genuine checker did not execute its index-content validation."
    return queries


def _read_only_parity(db, path, queries, old_difference):
    """Execute actual traced product SELECTs under native query_only."""
    assert not db.in_transaction
    before = sha256(path.read_bytes()).hexdigest()
    changes = db.total_changes
    db.execute("PRAGMA query_only=ON")
    try:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        expected = old_difference()
        actual = any(db.execute(sql).fetchone() is not None for sql in queries)
        assert actual == expected
    finally:
        db.execute("PRAGMA query_only=OFF")
    assert db.total_changes == changes
    assert sha256(path.read_bytes()).hexdigest() == before
    return expected


def _id(db, revision, ordinal=1):
    row = db.execute("SELECT id FROM page WHERE revision=? AND ordinal=?",
                     (revision, ordinal)).fetchone()
    assert row is not None
    return row[0]


def _replace_index(db, identity, raw):
    db.execute("DELETE FROM indexed WHERE id=?", (identity,))
    db.execute("INSERT INTO indexed(id,raw) VALUES(?,?)", (identity, raw))


@contextmanager
def _full_library(path, raw=RAW):
    with closing(Storage(path)) as storage:
        # Same visible edition ID in two workspaces; equal text has distinct IDs.
        storage.publish_revision("alpha", "shared", "old", ASSET, "fixture",
                                 [Page(raw)])
        storage.publish_revision("alpha", "shared", "current", ASSET, "fixture",
                                 [Page(raw), Page("different second page"), Page(raw)])
        storage.publish_revision("beta", "shared", "foreign", ASSET, "fixture",
                                 [Page(raw)])
        queries = _capture_membership_queries(storage.db, storage.check)
        yield storage, queries


@pytest.mark.parametrize("damage", (
    "none", "missing_first", "missing_last", "changed", "swapped",
    "stale_same_text", "foreign_missing", "same_count_historical_substitution",
))
def test_full_index_matches_old_symmetric_difference_with_real_scope_rows(tmp_path, damage):
    path = tmp_path / "full.sqlite"
    with _full_library(path) as (storage, queries):
        db = storage.db
        first, second, last = (_id(db, "current", value) for value in (1, 2, 3))
        old, foreign = _id(db, "old"), _id(db, "foreign")
        with storage.transaction():
            if damage == "missing_first":
                db.execute("DELETE FROM indexed WHERE id=?", (first,))
            elif damage == "missing_last":
                db.execute("DELETE FROM indexed WHERE id=?", (last,))
            elif damage == "changed":
                _replace_index(db, first, RAW + " changed")
            elif damage == "swapped":
                _replace_index(db, first, "different second page")
                _replace_index(db, second, RAW)
            elif damage == "stale_same_text":
                db.execute("INSERT INTO indexed(id,raw) VALUES(?,?)", (old, RAW))
            elif damage == "foreign_missing":
                db.execute("DELETE FROM indexed WHERE id=?", (foreign,))
            elif damage == "same_count_historical_substitution":
                db.execute("DELETE FROM indexed WHERE id=?", (first,))
                db.execute("INSERT INTO indexed(id,raw) VALUES(?,?)", (old, RAW))
        different = _read_only_parity(
            db, path, queries, lambda: _old_full_difference(db))
        assert different is (damage != "none")
        if different:
            with pytest.raises(ValueError, match="^active index content mismatch$"):
                storage.check()
        else:
            storage.check()


@pytest.mark.parametrize("original,replacement", (
    ("Needle", "needle"),
    ("ending", "ending "),
    ("Cafe\u0301", "Café"),
    ("first\r\nsecond", "first\nsecond"),
    ("prefix", "prefix\x00tail"),
    ("123", "123.0"),
    ("Blob text", sqlite3.Binary(b"Blob text")),
))
def test_raw_equality_preserves_exact_bytes_and_storage_class(
        tmp_path, original, replacement):
    path = tmp_path / "raw.sqlite"
    with _full_library(path, original) as (storage, queries):
        identity = _id(storage.db, "current")
        with storage.transaction():
            _replace_index(storage.db, identity, replacement)
        assert _read_only_parity(
            storage.db, path, queries, lambda: _old_full_difference(storage.db))
        with pytest.raises(ValueError, match="^active index content mismatch$"):
            storage.check()


@contextmanager
def _current_library(directory):
    app = LibraryApp(directory)
    workspace = app.create_workspace("Index equality")
    foreign_workspace = app.create_workspace("Independent scope")
    data = (RAW + "\fSecond actual segment.").encode("utf-8")
    digest = sha256(data).hexdigest()
    app.assets.retain(data, "text/plain", digest)
    record = Registration(
        "shared-work", "shared-edition",
        dict(sha256=digest, mime="text/plain", size=len(data)),
        "Exact retained source", ("Fixture Author",),
    )
    try:
        with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
            with storage.incremental_publications():
                publisher = _CatalogPublisher(storage, record)
                publisher.publish_revision(workspace, "shared-edition", "old",
                                           digest, "fixture", [Page(RAW)])
                publisher.publish_revision(workspace, "shared-edition", "current",
                                           digest, "fixture",
                                           [Page(RAW), Page("Second actual segment.")])
                publisher.publish_revision(foreign_workspace, "shared-edition", "foreign",
                                           digest, "fixture", [Page(RAW)])
            transaction = PublicationTransaction(
                storage, workspace, "shared-edition", "current", digest, "fixture")
            queries = _capture_membership_queries(
                storage.db, transaction._check_revision)
            yield app, storage, transaction, queries
    finally:
        assert app.close_search()


@pytest.mark.parametrize("damage,expected_error", (
    ("none", None),
    ("missing", "active index content mismatch"),
    ("changed", "active index content mismatch"),
    ("blob", "active index content mismatch"),
    ("stale", "Previous active pages remain indexed."),
    ("foreign_missing", None),
))
def test_revision_subset_parity_keeps_stale_edition_and_scope_boundaries(
        tmp_path, damage, expected_error):
    with _current_library(tmp_path / "current") as (app, storage, transaction, queries):
        db = storage.db
        first, old, foreign = (_id(db, revision)
                               for revision in ("current", "old", "foreign"))
        with storage.transaction():
            if damage == "missing":
                db.execute("DELETE FROM indexed WHERE id=?", (first,))
            elif damage == "changed":
                _replace_index(db, first, RAW + " changed")
            elif damage == "blob":
                _replace_index(db, first, sqlite3.Binary(RAW.encode("utf-8")))
            elif damage == "stale":
                db.execute("INSERT INTO indexed(id,raw) VALUES(?,?)", (old, RAW))
            elif damage == "foreign_missing":
                db.execute("DELETE FROM indexed WHERE id=?", (foreign,))
        different = _read_only_parity(
            db, app.database, queries,
            lambda: _old_revision_difference(db, "current"))
        assert different is (damage in ("missing", "changed", "blob"))
        if expected_error is None:
            transaction._check_revision()
        else:
            with pytest.raises(ValueError) as failure:
                transaction._check_revision()
            assert str(failure.value) == expected_error
        if damage == "foreign_missing":
            # The bounded target check does not adopt another workspace's damage.
            # The unchanged whole-storage gate still rejects that separate row.
            with pytest.raises(ValueError, match="^active index content mismatch$"):
                storage.check()


@pytest.mark.parametrize("damage", ("schema", "foreign_key", "raw_digest", "fts"))
def test_full_checker_preserves_earlier_and_later_independent_guards(tmp_path, damage):
    path = tmp_path / "guards.sqlite"
    with _full_library(path) as (storage, _):
        db = storage.db
        if damage == "schema":
            db.execute("DROP TRIGGER indexed_au")
            expected, message = ValueError, "revision storage schema mismatch"
        elif damage == "foreign_key":
            db.execute("PRAGMA foreign_keys=OFF")
            try:
                db.execute("INSERT INTO indexed(id,raw) VALUES(?,?)", (1000000, RAW))
            finally:
                db.execute("PRAGMA foreign_keys=ON")
            expected, message = ValueError, "database integrity failure"
        elif damage == "raw_digest":
            original = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
            with storage.transaction():
                db.execute("DROP TRIGGER page_bu")
                db.execute("UPDATE page SET raw=? WHERE id=?",
                           ("changed authoritative bytes", _id(db, "current")))
                db.execute(original)
            expected, message = ValueError, "raw digest mismatch"
        else:
            db.execute("INSERT INTO page_fts(page_fts) VALUES('delete-all')")
            assert not _old_full_difference(db)
            expected, message = sqlite3.DatabaseError, None
        with pytest.raises(expected) as failure:
            storage.check()
        if message is not None:
            assert str(failure.value) == message


def _native_read_only_plans(db, path, queries, old_sql, old_parameters=()):
    before = sha256(path.read_bytes()).hexdigest()
    changes = db.total_changes
    records = []
    db.execute("PRAGMA query_only=ON")
    try:
        for sql in queries:
            assert db.execute(sql).fetchone() is None
            plan = [tuple(row) for row in db.execute("EXPLAIN QUERY PLAN " + sql)]
            opcodes = [row[1] for row in db.execute("EXPLAIN " + sql)]
            assert not set(opcodes) & {
                "OpenEphemeral", "OpenAutoindex", "SorterOpen", "SorterInsert",
            }, (sql, plan, opcodes)
            records.append(dict(sql_sha256=sha256(sql.encode()).hexdigest(),
                                plan=plan, opcode_count=len(opcodes)))
        old_opcodes = [row[1] for row in db.execute(
            "EXPLAIN " + old_sql, old_parameters)]
        assert "OpenEphemeral" in old_opcodes, old_opcodes
    finally:
        db.execute("PRAGMA query_only=OFF")
    assert db.total_changes == changes
    assert sha256(path.read_bytes()).hexdigest() == before
    return records


def test_native_sqlite_read_only_parity_has_no_raw_text_materialization(tmp_path):
    # A real multi-MiB page makes this a content-sized regression. The proof
    # concerns actual SQLite query instructions, not a predicted RSS ceiling.
    path = tmp_path / "large.sqlite"
    raw = "A" * (2 * 1024**2) + " — точний Cafe\u0301\r\n"
    with _full_library(path, raw) as (storage, queries):
        assert not _read_only_parity(
            storage.db, path, queries, lambda: _old_full_difference(storage.db))
        full = _native_read_only_plans(
            storage.db, path, queries,
            "SELECT id,raw FROM active_page EXCEPT SELECT id,raw FROM indexed")
    with _current_library(tmp_path / "delta") as (app, storage, _, queries):
        assert not _read_only_parity(
            storage.db, app.database, queries,
            lambda: _old_revision_difference(storage.db, "current"))
        delta = _native_read_only_plans(
            storage.db, app.database, queries,
            "SELECT id,raw FROM page WHERE revision=? EXCEPT "
            "SELECT i.id,i.raw FROM indexed i JOIN page p ON p.id=i.id "
            "WHERE p.revision=?", ("current", "current"))
    report = dict(schema="carr-index-validation-read-only-parity/1",
                  sqlite=sqlite3.sqlite_version, full=full, revision=delta,
                  scope="Actual finite parity and opcode proof; no Windows overflow cause or corpus capacity claim.")
    encoded = json.dumps(report, sort_keys=True)
    assert len(encoded.encode("utf-8")) <= 16384
    print("CARR_INDEX_VALIDATION_PARITY " + encoded)
