"""Disk-backed composition of existing catalog and research candidate DDL.

Synthetic workspace table only; not production migration, authorization, source
attestation, or a substitute for the application-level reader test suite.
Requires CARR-02 PR53 catalog fixture in the assembled checkout.
"""
import hashlib
import sqlite3
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def connect(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA recursive_triggers=ON')
    return db


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'research.sqlite'
    db = connect(path)
    db.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
    for script in (HERE.parent / 'catalog/catalog_membership_candidate.sql',
                   HERE / 'topic_tree_candidate.sql',
                   HERE / 'saved_evidence_candidate.sql',
                   HERE / 'saved_topic_query_candidate.sql'):
        db.executescript(script.read_text(encoding='utf-8'))
    digest = hashlib.sha256(b'synthetic source').hexdigest()
    db.execute('INSERT INTO catalog_asset VALUES(?,?,?)', (digest, 'text/plain', 16))
    for workspace in ('a', 'b'):
        db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
        db.execute('INSERT INTO catalog_work VALUES(?,?,?)', (workspace, 'work', 'Same title'))
        db.execute('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)',
                   (workspace, 'edition', 'work', digest, 'uk', None, None))
        db.execute('INSERT INTO catalog_collection VALUES(?,?,?,0)', (workspace, 'collection', 'Books'))
        db.execute('INSERT INTO catalog_membership VALUES(?,?,?)', (workspace, 'collection', 'edition'))
        db.execute('INSERT INTO research_topic VALUES(?,?,NULL,?,0,0)', (workspace, 'topic', 'Research'))
        db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                   (workspace, 'topic', 'evidence', 'Цитата', 'fixture-checksum', workspace))
        db.execute('INSERT INTO research_saved_query VALUES(?,?,?,?,?,?,?,0)',
                   (workspace, 'topic', 'query', 'Query', 'Цитата', 'quote', 10))
    db.commit()
    try:
        yield db, path
    finally:
        db.close()


def test_membership_removal_and_note_change_survive_reopen(database):
    db, path = database
    db.execute("DELETE FROM catalog_membership WHERE workspace='a'")
    db.execute("UPDATE research_evidence SET note='Нова примітка' WHERE workspace='a'")
    db.commit()
    with connect(path) as reopened:
        assert reopened.execute('SELECT workspace FROM catalog_membership').fetchall() == [('b',)]
        assert reopened.execute('SELECT workspace,payload,checksum,note FROM research_evidence ORDER BY workspace').fetchall() == [
            ('a', 'Цитата', 'fixture-checksum', 'Нова примітка'),
            ('b', 'Цитата', 'fixture-checksum', 'b')]
        assert reopened.execute('SELECT count(*) FROM catalog_edition').fetchone() == (2,)
        assert reopened.execute('SELECT count(*) FROM research_saved_query').fetchone() == (2,)


@pytest.mark.parametrize('statement', [
    "UPDATE research_evidence SET payload='changed' WHERE workspace='a'",
    "DELETE FROM research_evidence WHERE workspace='a'",
    "INSERT OR REPLACE INTO research_evidence VALUES('a','topic','evidence','changed','x','x')",
    "UPDATE research_saved_query SET workspace='b' WHERE workspace='a'",
    "DELETE FROM research_saved_query WHERE workspace='a'",
    "INSERT OR REPLACE INTO research_saved_query VALUES('a','topic','query','x','x','note',1,0)",
    "DELETE FROM catalog_edition WHERE workspace='a'",
])
def test_immutable_and_identity_guards_compose(database, statement):
    db, _ = database
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement)
    db.rollback()
    assert db.execute('SELECT count(*) FROM research_evidence').fetchone() == (2,)
    assert db.execute('SELECT count(*) FROM research_saved_query').fetchone() == (2,)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_foreign_topic_references_rejected(database):
    db, _ = database
    db.execute("INSERT INTO research_topic VALUES('b','private',NULL,'Private',1,0)")
    for statement in (
        "INSERT INTO research_evidence VALUES('a','private','new','x','x','x')",
        "INSERT INTO research_saved_query VALUES('a','private','new','x','x','quote',1,0)",
        "INSERT INTO research_topic VALUES('a','child','private','x',0,0)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_native_backup_preserves_all_candidate_rows(database, tmp_path):
    db, _ = database
    backup_path = tmp_path / 'backup.sqlite'
    with connect(backup_path) as backup:
        db.backup(backup)
        assert backup.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert backup.execute('PRAGMA foreign_key_check').fetchall() == []
        for table in ('catalog_asset', 'catalog_edition', 'catalog_membership',
                      'research_topic', 'research_evidence', 'research_saved_query'):
            query = 'SELECT * FROM ' + table + ' ORDER BY 1,2'
            assert backup.execute(query).fetchall() == db.execute(query).fetchall()
