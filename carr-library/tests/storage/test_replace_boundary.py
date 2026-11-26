import hashlib
import sqlite3
import pytest
from carr_library.storage.revisions import Storage, Page


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / 'library.db')
    s.publish_revision('a', 'book', 'old', 'a'*64, 'native', [Page('original', 1)])
    s.publish_revision('a', 'book', 'new', 'a'*64, 'native', [Page('current', 1)])
    yield s
    s.close()


def test_replace_revision(store):
    with pytest.raises(sqlite3.IntegrityError, match='immutable revision'):
        store.db.execute("INSERT OR REPLACE INTO revision VALUES('old','a','book',?,'changed')", ('b'*64,))
    assert store.db.execute("SELECT asset FROM revision WHERE id='old'").fetchone()[0] == 'a'*64
    store.check()


def test_replace_seal(store):
    with pytest.raises(sqlite3.IntegrityError, match='immutable seal'):
        store.db.execute("INSERT OR REPLACE INTO sealed VALUES('old')")
    store.check()


def test_replace_page_via_unsealed_revision(store):
    original = store.get_page('a', 'old', 1)
    with store.transaction():
        store.db.execute("INSERT INTO revision VALUES('pending','a','book',?,'native')", ('a'*64,))
        with pytest.raises(sqlite3.IntegrityError, match='immutable page'):
            store.db.execute('INSERT OR REPLACE INTO page VALUES(?,?,?,?,?,?,?,?)',
                             (original['id'], 'pending', 1, 1, None, 0, 'different', hashlib.sha256(b'different').hexdigest()))
        # Roll back the deliberately incomplete revision before leaving the test.
        store.db.rollback()
    assert store.get_page('a', 'old', 1)['raw'] == 'original'
    store.check()


def test_reopen_enables_guard(store, tmp_path):
    reopened = Storage(tmp_path / 'library.db')
    try:
        with pytest.raises(sqlite3.IntegrityError, match='immutable revision'):
            reopened.db.execute("INSERT OR REPLACE INTO revision SELECT * FROM revision WHERE id='old'")
        reopened.check()
    finally:
        reopened.close()


def test_normal_publish_and_rebuild(store, tmp_path):
    store.publish_revision('a', 'book', 'next', 'a'*64, 'native', [Page('next text', 1)])
    store.rebuild()
    store.backup(tmp_path / 'backup.db')
    backup = Storage(tmp_path / 'backup.db')
    try:
        assert backup.get_page('a', 'old', 1)['raw'] == 'original'
        assert backup.db.execute('SELECT raw FROM active_page').fetchone()[0] == 'next text'
        backup.check()
    finally:
        backup.close()
