"""Synthetic corruption boundaries; no real source material."""
import hashlib
import pytest
from carr_library.storage.revisions import Storage, Page


def test_unsealed_page_is_not_readable(tmp_path):
    s = Storage(tmp_path / 'db')
    try:
        s.publish_revision('A', 'book', 'good', 'a'*64, 'fixture', [Page('old', 1)])
        with s.transaction():
            s.db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', ('bad', 'A', 'book', 'b'*64, 'fixture'))
            s.db.execute('INSERT INTO page(revision,ordinal,physical,verified,raw,digest) VALUES(?,?,?,?,?,?)',
                         ('bad', 1, 1, 0, 'partial', hashlib.sha256(b'partial').hexdigest()))
        assert s.get_page('A', 'bad', 1) is None
        assert s.get_page('A', 'good', 1)['raw'] == 'old'
    finally:
        s.close()


def test_digest_mismatch_on_historical_read(tmp_path):
    s = Storage(tmp_path / 'db')
    try:
        s.publish_revision('A', 'book', 'old', 'a'*64, 'fixture', [Page('old', 1)])
        s.publish_revision('A', 'book', 'new', 'b'*64, 'fixture', [Page('new', 1)])
        s.db.execute('DROP TRIGGER page_bu')
        s.db.execute("UPDATE page SET raw='tampered' WHERE revision='old'")
        with pytest.raises(ValueError, match='raw digest mismatch'):
            s.get_page('A', 'old', 1)
        assert s.get_page('B', 'old', 1) is None
        assert s.get_page('A', 'new', 1)['raw'] == 'new'
    finally:
        s.close()


@pytest.mark.parametrize('operation', ['check', 'rebuild', 'backup'])
def test_missing_active_pointer(tmp_path, operation):
    s = Storage(tmp_path / 'db')
    try:
        s.publish_revision('A', 'book', 'old', 'a'*64, 'fixture', [Page('old', 1)])
        s.publish_revision('A', 'book', 'new', 'b'*64, 'fixture', [Page('new', 1)])
        s.publish_revision('B', 'book', 'other', 'c'*64, 'fixture', [Page('other', 1)])
        with s.transaction():
            s.db.execute("DELETE FROM indexed WHERE id IN (SELECT id FROM active_page WHERE workspace='A')")
            s.db.execute("DELETE FROM active WHERE workspace='A'")
        assert s.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not s.db.execute('PRAGMA foreign_key_check').fetchall()
        with pytest.raises(ValueError, match='missing active edition'):
            if operation == 'backup':
                s.backup(tmp_path / 'copy')
            else:
                getattr(s, operation)()
        assert not (tmp_path / 'copy').exists()
        assert s.get_page('A', 'old', 1)['raw'] == 'old'
        assert s.get_page('B', 'other', 1)['raw'] == 'other'
    finally:
        s.close()
