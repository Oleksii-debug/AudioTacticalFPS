"""PR66 integration regression: real V2 storage and catalog, no parallel schema.

Run only on assembled candidate containing PR21/53/57/66 and PR33 runner.
Synthetic data; authorization remains the host's responsibility.
"""
from contextlib import closing, contextmanager
import sqlite3

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.catalog.read_views import active_collections, browse_editions
from carr_library.catalog.registration import Registration, register_batch
from carr_library.storage.revisions import Page, Storage


def record(workspace, eid, *, collections=('c',)):
    return Registration('w' + eid, eid,
                        dict(sha256=workspace * 64, mime='text/plain', size=8),
                        'Історія 100%_test', ('Олена', 'Žan') if workspace == 'a' else ('Private',),
                        'uk', '2', 'iv', collections)


def publish(s, workspace, eid, revision, text):
    s.stage_revision(workspace, eid, revision, workspace * 64, 'synthetic',
                     [Page(text, 3, 'iv', True)])


@contextmanager
def snapshot(path, workspace):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        try:
            yield Catalog(db, workspace)
        finally:
            db.rollback()


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as s:
        assert s.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        with s.transaction():
            for ws in ('a', 'b'):
                s.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                catalog = Catalog(s.db, ws)
                catalog.create_collection('c', 'Same collection')
                catalog.create_collection(ws + '-only', 'Other')
                register_batch(catalog, (record(ws, 'e1'), record(ws, 'e2')))
                for eid in ('e1', 'e2'):
                    publish(s, ws, eid, ws + eid, 'same original passage')
        yield s, path


def ids(rows):
    return [r.edition_id for r in rows]


def test_read_views_real_row_factory_and_scoped_filters(library):
    s, path = library
    with snapshot(path, 'a') as a:
        assert active_collections(a) == (('a-only', 'Other'), ('c', 'Same collection'))
        rows = browse_editions(a, collection='c', title='%_', author='Žan', language='uk', edition='2', limit=1)
        assert ids(rows) == ['e1']
        assert rows[0].authors == ('Олена', 'Žan')
        assert rows[0].asset == 'a' * 64 and rows[0].volume == 'iv'
        assert ids(browse_editions(a, collection='c', after=rows[0].edition_id)) == ['e2']
        assert browse_editions(a, author='Private') == ()
        with pytest.raises(ValueError, match='unknown or archived'):
            browse_editions(a, collection='b-only')
    with snapshot(path, 'b') as b:
        assert ids(browse_editions(b, collection='c', author='Private')) == ['e1', 'e2']
        assert browse_editions(b, author='Олена') == ()
    s.check()


def test_pagination_and_collection_callback_share_wal_snapshot(library):
    s, path = library
    with snapshot(path, 'a') as a:
        first = browse_editions(a, collection='c', limit=1)[0]
        assert first.edition_id == 'e1'
        with s.transaction():
            writer = Catalog(s.db, 'a')
            writer.remove('c', 'e2')
            register_batch(writer, (record('a', 'e3'),))
            publish(s, 'a', 'e3', 'ae3', 'new source')
            writer.archive('c')
        # This already-open read snapshot must not mix old and new memberships.
        assert ('c', 'Same collection') in active_collections(a)
        assert ids(browse_editions(a, collection='c', after=first.edition_id)) == ['e2']
        assert ids(browse_editions(a)) == ['e1', 'e2']
    with snapshot(path, 'a') as a:
        assert active_collections(a) == (('a-only', 'Other'),)
        with pytest.raises(ValueError, match='unknown or archived'):
            browse_editions(a, collection='c')
        assert ids(browse_editions(a)) == ['e1', 'e2', 'e3']
    with snapshot(path, 'b') as b:
        assert ids(browse_editions(b, collection='c')) == ['e1', 'e2']
    s.check()


def test_atomic_register_stage_visibility_and_failed_import(library):
    s, path = library
    with pytest.raises(ValueError, match='invalid raw page'):
        with s.transaction():
            catalog = Catalog(s.db, 'a')
            register_batch(catalog, (record('a', 'e3'),))
            assert ids(browse_editions(catalog, collection='c')) == ['e1', 'e2', 'e3']
            with snapshot(path, 'a') as a:
                assert ids(browse_editions(a, collection='c')) == ['e1', 'e2']
            publish(s, 'a', 'e3', 'bad', 'bad\x00text')
    with snapshot(path, 'a') as a:
        assert ids(browse_editions(a, collection='c')) == ['e1', 'e2']
    assert s.get_page('a', 'bad', 1) is None
    with s.transaction():
        catalog = Catalog(s.db, 'a')
        register_batch(catalog, (record('a', 'e3'),))
        publish(s, 'a', 'e3', 'good', 'new source')
        with snapshot(path, 'a') as a:
            assert ids(browse_editions(a, collection='c')) == ['e1', 'e2']
    with snapshot(path, 'a') as a:
        assert ids(browse_editions(a, collection='c')) == ['e1', 'e2', 'e3']
    assert s.get_page('a', 'good', 1)['raw'] == 'new source'
    s.check()


def test_catalog_backup_preserves_author_views_and_historical_page(library):
    s, path = library
    with s.transaction():
        c = Catalog(s.db, 'a')
        register_batch(c, (record('a', 'e1', collections=('a-only',)),))
        c.remove('c', 'e1')
        publish(s, 'a', 'e1', 'ae1-new', 'changed extraction')
    target = path.with_name('backup.db')
    s.backup(target)
    with snapshot(target, 'a') as a:
        assert ids(browse_editions(a, collection='c')) == ['e2']
        assert browse_editions(a, collection='a-only')[0].authors == ('Олена', 'Žan')
    with closing(Storage(target, schema_version=2)) as restored:
        restored.check()
        historical = restored.get_page('a', 'ae1', 1)
        assert (historical['raw'], historical['physical'], historical['printed']) == ('same original passage', 3, 'iv')
        assert restored.get_page('b', 'ae1', 1) is None
        assert restored.get_page('a', 'ae1-new', 1)['raw'] == 'changed extraction'
        assert restored.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 2


def test_views_reject_closed_snapshot(library):
    s, _ = library
    c = Catalog(s.db, 'a')
    with pytest.raises(ValueError, match='caller snapshot'):
        active_collections(c)
    with pytest.raises(ValueError, match='caller snapshot'):
        browse_editions(c)
