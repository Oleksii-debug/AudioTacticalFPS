"""CARR-SEC-SCHEMA-001: fail closed on V1 schema drift."""
from contextlib import closing
import sqlite3

import pytest

from carr_library.storage.revisions import Page, Storage


def seed(path):
    with closing(Storage(path)) as s:
        s.publish_revision('A', 'edition', 'old', 'a'*64, 'v1', [Page('Міст old', 1, 'iv', True)])
        s.publish_revision('A', 'edition', 'new', 'b'*64, 'v1', [Page('new', 1)])


@pytest.mark.parametrize('mutation', [
    'DROP TRIGGER revision_bu',
    "DROP TRIGGER indexed_ai",
    'CREATE TABLE unknown_extension(id INTEGER)',
])
def test_reopen_rejects_drift(tmp_path, mutation):
    path = tmp_path / 'library.db'
    seed(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(mutation)
    with pytest.raises(ValueError, match='schema mismatch'):
        with closing(Storage(path)):
            pass


def test_same_name_changed_trigger(tmp_path):
    path = tmp_path / 'library.db'
    seed(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('DROP TRIGGER revision_bu')
        db.execute('CREATE TRIGGER revision_bu BEFORE UPDATE ON revision BEGIN SELECT 1; END')
    with pytest.raises(ValueError, match='schema mismatch'):
        with closing(Storage(path)):
            pass


@pytest.mark.parametrize('operation', ['check', 'rebuild', 'backup', 'publish'])
def test_live_drift_rejected(tmp_path, operation):
    path, target = tmp_path / 'library.db', tmp_path / 'copy.db'
    seed(path)
    with closing(Storage(path)) as s:
        s.db.execute('DROP TRIGGER revision_bu')
        with pytest.raises(ValueError, match='schema mismatch'):
            if operation == 'backup':
                s.backup(target)
            elif operation == 'publish':
                s.publish_revision('A', 'edition', 'third', 'c'*64, 'v1', [Page('third', 1)])
            else:
                getattr(s, operation)()
    assert not target.exists()


def test_clean_snapshot_and_historical_quote(tmp_path):
    path, target = tmp_path / 'library.db', tmp_path / 'copy.db'
    seed(path)
    with closing(Storage(path)) as s:
        s.check()
        s.rebuild()
        s.backup(target)
    with closing(Storage(target)) as s:
        s.check()
        assert s.get_page('A', 'old', 1)['raw'] == 'Міст old'
        assert s.get_page('B', 'old', 1) is None
