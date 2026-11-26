"""Synthetic source-locator regressions for PR #23 contract boundary."""
import sqlite3
import pytest
from carr_library.storage.revisions import Page, Storage


def publish(s, revision, pages):
    s.publish_revision('a', 'edition', revision, 'a'*64, 'fixture', pages)


@pytest.mark.parametrize('pages', [
    [Page('logical', None, 'iv')],
    [Page('logical'), Page('physical', 2)],
    [Page('physical', 1), Page('logical')],
    [Page('later', 2), Page('earlier', 1)],
    [Page('fractional', 1.5)],
    [Page('empty label', 1, '', True)],
])
def test_invalid_revision_rolls_back(tmp_path, pages):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'old', [Page('old needle', 1)])
        with pytest.raises(sqlite3.IntegrityError):
            publish(s, 'bad', pages)
        assert s.get_page('a', 'bad', 1) is None
        assert s.db.execute('SELECT revision FROM active').fetchone()[0] == 'old'
        assert s.db.execute('SELECT raw FROM indexed').fetchone()[0] == 'old needle'
        s.check()
    finally:
        s.close()


def test_history_and_backup(tmp_path):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'physical', [Page('Міст', 1, 'iv', True), Page('river', 3)])
        publish(s, 'logical', [Page('розділ'), Page('next')])
        s.backup(tmp_path/'copy')
    finally:
        s.close()
    c = Storage(tmp_path/'copy')
    try:
        c.check()
        assert c.get_page('a','physical',1)['printed'] == 'iv'
        assert c.get_page('a','physical',2)['physical'] == 3
        assert c.get_page('a','logical',1)['physical'] is None
        assert c.get_page('b','physical',1) is None
    finally:
        c.close()


def test_bad_historical_locator_refuses_backup(tmp_path):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'old', [Page('first', 1), Page('second', 2)])
        publish(s, 'active', [Page('new')])
        # Synthetic database corruption; not an exposed user operation.
        trigger = s.db.execute("SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        s.db.execute('DROP TRIGGER page_bu')
        s.db.execute("UPDATE page SET physical=3 WHERE revision='old' AND ordinal=1")
        # Restore exact schema so this test still exercises locator corruption.
        s.db.execute(trigger)
        assert s.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with pytest.raises(sqlite3.IntegrityError):
            s.check()
        with pytest.raises(sqlite3.IntegrityError):
            s.backup(tmp_path/'copy')
        assert not (tmp_path/'copy').exists()
    finally:
        s.close()


@pytest.mark.parametrize('bad', [
    Page('bad', True), Page('bad', 1.0), Page('bad', '1'),
    Page('bad', 1, 12), Page('bad', 1, 12, True),
    Page('bad', 1, 'iv', 1), Page('bad', 1, 'iv', 0),
    Page('bad', 1, 'iv', 1.0), Page('bad', 1, 'iv', '1'),
    Page('bad', 1, None, 0.5), Page('bad', None, None, 0),
    Page('bad', 1, 'iv', None),
])
def test_uncoerced_locator_rejected_inside_caller_transaction(tmp_path, bad):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'old', [Page('old needle', 1, 'iv', True)])
        with s.transaction():
            # Single-page case exposes affinity coercion without a duplicate
            # physical index masking the defect; partial case tests savepoint.
            for pages in ([bad], [Page('partial', 1), bad]):
                with pytest.raises(sqlite3.IntegrityError, match='invalid source locator'):
                    s.stage_revision('a', 'edition', 'bad', 'a'*64, 'fixture', pages)
                assert s.db.in_transaction
                assert s.db.execute("SELECT count(*) FROM revision WHERE id='bad'").fetchone()[0] == 0
                assert s.db.execute("SELECT count(*) FROM page WHERE revision='bad'").fetchone()[0] == 0
                assert s.get_page('a', 'bad', 1) is None
                assert s.db.execute('SELECT revision FROM active').fetchone()[0] == 'old'
                assert s.db.execute('SELECT raw FROM indexed').fetchone()[0] == 'old needle'
            s.stage_revision('a', 'edition', 'good', 'a'*64, 'fixture',
                             [Page('Точна цитата', 3, '頁十二', True)])
        s.check()
        assert s.get_page('a', 'old', 1)['printed'] == 'iv'
        s.backup(tmp_path/'copy')
    finally:
        s.close()
    c = Storage(tmp_path/'copy')
    try:
        c.check()
        assert c.get_page('a', 'bad', 1) is None
        assert c.get_page('a', 'good', 1)['raw'] == 'Точна цитата'
        assert c.get_page('a', 'good', 1)['printed'] == '頁十二'
        assert c.get_page('a', 'old', 1)['printed'] == 'iv'
    finally:
        c.close()
