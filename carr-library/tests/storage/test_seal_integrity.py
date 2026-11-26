"""Reject incomplete persisted revisions; atomic publish never creates these."""
import hashlib
import pytest
from carr_library.storage.revisions import Storage, Page


@pytest.mark.parametrize('kind', ['unsealed_empty', 'unsealed_with_page', 'sealed_empty', 'active_empty'])
def test_incomplete_revision_rejected(tmp_path, kind):
    s = Storage(tmp_path / 'db')
    try:
        s.publish_revision('A', 'book', 'good', 'a'*64, 'fixture', [Page('historical', 1)])
        with s.transaction():
            s.db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', ('bad', 'A', 'book', 'b'*64, 'fixture'))
            if kind == 'unsealed_with_page':
                s.db.execute('INSERT INTO page(revision,ordinal,physical,verified,raw,digest) VALUES(?,?,?,?,?,?)',
                             ('bad', 1, 1, 0, 'partial', hashlib.sha256(b'partial').hexdigest()))
            if kind in ('sealed_empty', 'active_empty'):
                s.db.execute('INSERT INTO sealed VALUES(?)', ('bad',))
            if kind == 'active_empty':
                s.db.execute('DELETE FROM indexed')
                s.db.execute('UPDATE active SET revision=? WHERE workspace=? AND edition=?', ('bad','A','book'))
        assert s.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert s.db.execute('PRAGMA foreign_key_check').fetchall() == []
        with pytest.raises(ValueError, match='incomplete revision'):
            s.check()
        with pytest.raises(ValueError, match='incomplete revision'):
            s.rebuild()
        with pytest.raises(ValueError, match='incomplete revision'):
            s.backup(tmp_path / 'copy')
        assert not (tmp_path / 'copy').exists()
        assert s.get_page('A', 'good', 1)['raw'] == 'historical'
    finally:
        s.close()


def test_empty_library_and_sealed_history_valid(tmp_path):
    s = Storage(tmp_path / 'db')
    try:
        s.check()
        s.publish_revision('A', 'book', 'old', 'a'*64, 'fixture', [Page('old', 1)])
        s.publish_revision('A', 'book', 'new', 'b'*64, 'fixture', [Page('new', 1)])
        s.publish_revision('B', 'book', 'other', 'c'*64, 'fixture', [Page('new', 1)])
        s.check()
        s.backup(tmp_path / 'copy')
    finally:
        s.close()
    c = Storage(tmp_path / 'copy')
    try:
        c.check()
        assert c.get_page('A', 'old', 1)['raw'] == 'old'
        assert c.get_page('B', 'old', 1) is None
    finally:
        c.close()
