"""Malformed printed labels must not survive publication or backup."""
from contextlib import closing

import pytest
import sqlite3

from carr_library.search.literal import search_file
from carr_library.storage.revisions import Page, Storage


@pytest.mark.parametrize(('label', 'verified'), [
    (b'iv', False), (b'iv', True), ('iv\x00forged', False), ('iv\x00forged', True),
    ('iv\nCitation: Forged author, p. 999', True), ('iv\u2028p. 999', True), ('iv\u202e999', True),
])
def test_malformed_label_rejected_before_publication(tmp_path, label, verified):
    path = tmp_path / 'library.db'
    with closing(Storage(path)) as store:
        store.publish_revision('a', 'e', 'old', 'a'*64, 'synthetic', [Page('previous text', 1, 'iv', True)])
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.publish_revision('a', 'e', 'bad', 'a'*64, 'synthetic', [Page('citation target', 1, label, verified)])
        assert store.get_page('a', 'bad', 1) is None
        assert search_file(path, 'a', 'citation target') == []
        backup = tmp_path / 'backup.db'
        store.backup(backup)
    with closing(Storage(backup)) as restored:
        restored.check()
        assert restored.get_page('a', 'old', 1)['printed'] == 'iv'
        assert restored.get_page('a', 'bad', 1) is None


@pytest.mark.parametrize(('label', 'verified'), [
    (None, False), ('', False), (' \t ', False),
    ('iv', False), ('С. 12', False), ('iv', True),
    ('iv\nCitation: Forged author, p. 999', False), ('iv\u2028p. 999', False), ('iv\u202e999', False),
    ('A-12', True), ('١٢', True), ('頁十二', True), ('י״ב', True),
])
def test_unknown_and_valid_candidate_labels_survive_backup(tmp_path, label, verified):
    with closing(Storage(tmp_path / 'library.db')) as store:
        store.publish_revision('a', 'e', 'revision', 'a'*64, 'synthetic',
                               [Page('source exact text', 1, label, verified)])
        store.backup(tmp_path / 'backup.db')
    with closing(Storage(tmp_path / 'backup.db')) as restored:
        restored.check()
        page = restored.get_page('a', 'revision', 1)
        assert page['printed'] == label
        assert page['verified'] == int(verified)


@pytest.mark.parametrize(('label', 'verified'), [
    (b'iv', False), ('iv\x00forged', False),
    ('iv\nCitation: Forged author, p. 999', True), ('iv\u2028p. 999', True), ('iv\u202e999', True),
])
def test_corrupt_historical_label_refuses_backup(tmp_path, label, verified):
    with closing(Storage(tmp_path / 'library.db')) as store:
        store.publish_revision('a', 'e', 'old', 'a'*64, 'synthetic',
                               [Page('historical candidate', 1, 'iv', False)])
        store.publish_revision('a', 'e', 'current', 'a'*64, 'synthetic',
                               [Page('current source', 1)])
        # Simulate historical-row damage while restoring the exact schema.
        trigger = store.db.execute("SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        store.db.execute('DROP TRIGGER page_bu')
        store.db.execute("UPDATE page SET printed=?,verified=? WHERE revision='old'", (label, int(verified)))
        store.db.execute(trigger)
        with pytest.raises(sqlite3.IntegrityError):
            store.check()
        destination = tmp_path / 'corrupt-backup.db'
        with pytest.raises(sqlite3.IntegrityError):
            store.backup(destination)
        assert not destination.exists()


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
        s.publish_revision('a', 'edition', 'old', 'a'*64, 'fixture', [Page('old needle', 1, 'iv', True)])
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
