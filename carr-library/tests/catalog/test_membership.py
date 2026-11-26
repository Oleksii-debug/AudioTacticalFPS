import hashlib
from pathlib import Path
import sqlite3
import pytest
from carr_library.catalog.membership import Catalog

SCHEMA = Path(__file__).with_name('catalog_membership_candidate.sql').read_text()


def manifest(data=b'synthetic'):
    return dict(sha256=hashlib.sha256(data).hexdigest(), mime='text/plain', size=len(data))


def connect(path):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA recursive_triggers=ON')
    return db


@pytest.fixture
def db(tmp_path):
    db = connect(tmp_path/'Бібліотека.db')
    db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);' + SCHEMA)
    db.execute("INSERT INTO workspace VALUES('a'),('b')")
    db.execute('BEGIN IMMEDIATE')
    yield db
    db.close()


def seed(db, workspace='a', edition='e', work='w', source=None):
    c = Catalog(db, workspace)
    c.add_work(work, 'Однакова назва', ('Автор', 'Author'))
    c.add_edition(edition, work, source or manifest(), language='uk')
    return c


def test_multiple_memberships_and_dedup(db):
    c = seed(db)
    c.add_edition('translation', 'w', manifest(), language='en')
    for collection in ('one', 'two'):
        c.create_collection(collection, collection)
        c.assign(collection, 'e')
        c.assign(collection, 'e')
    assert db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1
    assert len(c.editions()) == 2
    assert c.remove('one', 'e')
    assert not c.remove('one', 'e')
    assert c.editions('one') == ()
    assert c.editions('two')[0][0] == 'e'
    assert len(c.editions()) == 2


def test_same_title_distinct_author_and_edition(db):
    c = seed(db)
    c.add_work('other', 'Однакова назва', ('Другий',))
    c.add_edition('other-edition', 'other', manifest())
    assert [r[1] for r in c.editions()] == ['w', 'other']
    assert db.execute('SELECT name FROM catalog_author WHERE workspace=? AND work=? ORDER BY position', ('a','w')).fetchall() == [('Автор',), ('Author',)]


def test_workspace_isolation(db):
    a = seed(db)
    b = seed(db, 'b', 'private')
    a.create_collection('one', 'Same')
    b.create_collection('one', 'Same')
    b.assign('one', 'private')
    assert a.editions('one') == ()
    with pytest.raises(ValueError):
        a.assign('one', 'private')
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO catalog_membership VALUES('a','one','private')")
    assert a.editions()[0][0] == 'e'
    assert b.editions()[0][0] == 'private'


def test_archive_preserves_membership(db):
    c = seed(db)
    c.create_collection('old', 'Name')
    c.assign('old', 'e')
    c.archive('old')
    for operation in (lambda: c.assign('old','e'), lambda: c.remove('old','e'), lambda: c.editions('old')):
        with pytest.raises(ValueError): operation()
    c.create_collection('new', 'Name')
    assert db.execute('SELECT count(*) FROM catalog_membership').fetchone()[0] == 1
    assert c.editions()[0][0] == 'e'


def test_immutable_identity_and_source(db):
    seed(db)
    for sql in ("UPDATE catalog_edition SET id='new'", "UPDATE catalog_edition SET work='other'", "DELETE FROM catalog_edition", "DELETE FROM catalog_asset", "UPDATE catalog_asset SET mime='other'"):
        with pytest.raises(sqlite3.IntegrityError): db.execute(sql)


def test_atomic_failure_even_if_caller_commits(db):
    c = seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        c.add_edition('e', 'w', manifest(b'new'))
    assert db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1
    with pytest.raises(ValueError):
        c.add_edition('x', 'w', {**manifest(), 'size': 100})
    db.commit()
    assert len(c.editions()) == 1


def test_reopen_and_rollback(db):
    c = seed(db)
    c.create_collection('one', 'Name')
    c.assign('one','e')
    db.commit()
    path = db.execute('PRAGMA database_list').fetchone()[2]
    other = connect(path)
    try:
        reloaded = Catalog(other,'a')
        assert reloaded.editions('one') == c.editions('one')
        other.execute('BEGIN IMMEDIATE')
        reloaded.remove('one','e')
        other.rollback()
        assert reloaded.editions('one') == c.editions('one')
    finally:
        other.close()


def test_transaction_required(db):
    c = seed(db)
    db.commit()
    with pytest.raises(ValueError): c.create_collection('one','Name')


def test_unknown_metadata_not_inferred(db):
    c = Catalog(db,'a')
    c.add_work('w')
    c.add_edition('e','w',manifest())
    assert c.editions()[0][3:] == (None,None,None,None)
    assert db.execute('SELECT count(*) FROM catalog_author').fetchone()[0] == 0
