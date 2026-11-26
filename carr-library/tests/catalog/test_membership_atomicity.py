"""QA-006 and SEC-CATALOG-001 regressions using the existing test-only schema."""
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.catalog.membership import Catalog

SCHEMA = Path(__file__).with_name('catalog_membership_candidate.sql').read_text()


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(tmp_path / 'Дослідження.db', isolation_level=None)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA recursive_triggers=ON')
    con.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);' + SCHEMA)
    con.execute("INSERT INTO workspace VALUES('a'),('b')")
    con.execute('BEGIN IMMEDIATE')
    yield con
    con.close()


def test_failed_second_author_is_atomic(db):
    c = Catalog(db, 'a')
    c.add_work('good', 'Title', ('Автор',))
    db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON catalog_author WHEN new.position=1 BEGIN SELECT RAISE(ABORT,'second author'); END")
    with pytest.raises(sqlite3.IntegrityError, match='second author'):
        c.add_work('bad', 'Title', ('First', 'Second'))
    assert db.in_transaction
    db.commit()
    assert db.execute('SELECT id FROM catalog_work').fetchall() == [('good',)]
    assert db.execute('SELECT name FROM catalog_author').fetchall() == [('Автор',)]


def test_surrogate_author_does_not_leave_partial_work(db):
    c = Catalog(db, 'a')
    with pytest.raises(ValueError):
        c.add_work('bad', 'Title', ('First', '\ud800'))
    db.commit()
    assert db.execute('SELECT count(*) FROM catalog_work').fetchone() == (0,)
    assert db.execute('SELECT count(*) FROM catalog_author').fetchone() == (0,)


def test_transaction_wide_failure_preserves_original_error(db):
    c = Catalog(db, 'a')
    db.execute("CREATE TRIGGER rollback_author BEFORE INSERT ON catalog_author BEGIN SELECT RAISE(ROLLBACK,'author rollback'); END")
    with pytest.raises(sqlite3.IntegrityError, match='author rollback'):
        c.add_work('bad', 'Title', ('First',))
    assert not db.in_transaction
    assert db.execute('SELECT count(*) FROM catalog_work').fetchone() == (0,)


def test_reject_disabled_recursive_triggers_before_write(db):
    c = Catalog(db, 'a')
    db.execute('PRAGMA recursive_triggers=OFF')
    with pytest.raises(ValueError, match='recursive triggers'):
        c.add_work('bad', 'Title', ('First',))
    assert db.execute('SELECT count(*) FROM catalog_work').fetchone() == (0,)


def test_replace_cannot_retarget_catalog_on_configured_connection(db):
    c = Catalog(db, 'a')
    c.add_work('w', 'Title', ('First',))
    c.add_work('other', 'Title', ('Other',))
    digest = sha256(b'x').hexdigest()
    c.add_edition('e', 'w', dict(sha256=digest, mime='text/plain', size=1))
    c.create_collection('c', 'Collection')
    c.assign('c', 'e')
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT OR REPLACE INTO catalog_edition VALUES('a','e','other',?,NULL,NULL,NULL)", (digest,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT OR REPLACE INTO catalog_asset VALUES(?,'text/plain',99)", (digest,))
    db.commit()
    assert c.editions('c')[0][:3] == ('e', 'w', digest)
    assert db.execute('SELECT size FROM catalog_asset').fetchone() == (1,)


def test_surrogate_identity_and_metadata_rejected_before_sql(db):
    c = Catalog(db, 'a')
    for identity, title in [('bad\ud800', 'Title'), ('bad', 'Title\ud800')]:
        with pytest.raises(ValueError):
            c.add_work(identity, title)
    assert db.execute('SELECT count(*) FROM catalog_work').fetchone() == (0,)
