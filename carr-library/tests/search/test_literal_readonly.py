"""Native SQLite source-connection regression tests, synthetic only."""
from hashlib import sha256
import sqlite3

import pytest

from carr_library.search import literal


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'Книги # 100%.db'
    writer = sqlite3.connect(path)
    writer.execute('CREATE TABLE active_page(id INTEGER PRIMARY KEY,workspace,edition,revision,asset,ordinal,physical,printed,verified,raw,digest)')
    def add(ws, raw):
        writer.execute('INSERT INTO active_page VALUES(NULL,?,?,?,?,?,?,?,?,?,?)',
                       (ws, 'e', ws+'1', 'a'*64, 1, 3, 'iv', 1, raw,
                        sha256(raw.encode()).hexdigest()))
    add('A', 'Straße. Революція.')
    add('B', 'Straße. Революція.')
    writer.commit()
    try:
        yield path, writer, add
    finally:
        writer.close()


def test_exact_scope_and_unicode_filename(library):
    path, writer, _ = library
    before = path.read_bytes()
    hits = literal.search_file(path, 'A', 'STRASSE')
    assert hits == literal.search(writer, 'A', 'STRASSE')
    assert len(hits) == 1 and hits[0]['raw_quote'] == 'Straße'
    assert hits[0]['workspace_id'] == 'A' and hits[0]['physical_index'] == 3
    assert path.read_bytes() == before


def test_committed_only_and_writer_handler(library):
    path, writer, add = library
    calls = []
    writer.set_progress_handler(lambda: calls.append(1) or 0, 1)
    add('A', 'Pending needle')
    calls.clear()
    assert literal.search_file(path, 'A', 'needle') == []
    assert not calls
    writer.execute('SELECT count(*) FROM active_page').fetchone()
    assert calls
    writer.commit()
    assert literal.search_file(path, 'A', 'needle')[0]['raw_quote'] == 'needle'


def test_missing_file_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        literal.search_file(path, 'A', 'needle')
    assert not path.exists()


def test_exclusive_writer_fails_explicitly(library):
    path, writer, _ = library
    writer.execute('BEGIN EXCLUSIVE')
    try:
        with pytest.raises(sqlite3.OperationalError) as err:
            literal.search_file(path, 'A', 'needle')
        assert err.value.sqlite_errorcode & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    finally:
        writer.rollback()
    assert literal.search_file(path, 'A', 'Straße')


def test_native_source_interrupt(tmp_path):
    path = tmp_path / 'slow.db'
    db = sqlite3.connect(path)
    db.execute('''CREATE VIEW active_page AS
        WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<250000)
        SELECT n id, printf('B%d', n) workspace, 'e' edition, 'r' revision, 'a' asset,
        1 ordinal, 1 physical, NULL printed, 0 verified, 'other' raw, 'x' digest
        FROM seq''')
    db.commit()
    calls = 0
    def cancel_once():
        nonlocal calls
        calls += 1
        return calls == 20
    try:
        with pytest.raises(TimeoutError) as err:
            literal.search_file(path, 'A', 'needle', cancelled=cancel_once)
        cause = err.value.__cause__
        assert isinstance(cause, sqlite3.OperationalError)
        assert cause.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        assert calls == 20
        assert db.execute('SELECT 1').fetchone() == (1,)
    finally:
        db.close()


def test_precancel_and_invalid_budget(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(TimeoutError):
        literal.search_file(path, 'A', 'needle', cancelled=lambda: True)
    for timeout in (0, 61, float('nan'), True):
        with pytest.raises(ValueError):
            literal.search_file(path, 'A', 'needle', timeout=timeout)
    assert not path.exists()
