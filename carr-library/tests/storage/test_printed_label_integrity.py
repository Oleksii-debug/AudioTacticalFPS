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
