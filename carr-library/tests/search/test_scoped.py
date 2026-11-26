"""Actual WAL snapshot regressions for production scoped retrieval."""
from contextlib import closing
from hashlib import sha256
import sqlite3

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.search.scoped import collection_search, collection_search_file
from carr_library.storage.revisions import Page, Storage


RAW = 'Історія: Straße\nісторії'
ASSET = sha256(RAW.encode()).hexdigest()


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as store:
        assert store.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        with store.transaction():
            for ws in ('a', 'b'):
                store.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                catalog = Catalog(store.db, ws)
                catalog.add_work('w', 'Same title', [ws])
                catalog.add_edition('e', 'w', {'sha256': ASSET, 'mime': 'text/plain', 'size': len(RAW.encode())})
                catalog.create_collection('c', 'Research')
                catalog.create_collection('empty', 'Empty')
                catalog.assign('c', 'e')
                store.stage_revision(ws, 'e', ws+'1', ASSET, 'synthetic', [Page(RAW)])
        yield store, path


def test_exact_source_and_collection_boundaries(library):
    store, path = library
    for ws in ('a', 'b'):
        hit, = collection_search_file(path, ws, 'STRASSE історії', collection='c')
        assert (hit['workspace_id'], hit['edition_id'], hit['revision_id']) == (ws, 'e', ws+'1')
        assert hit['raw_quote'] == RAW[hit['start']:hit['end']] == 'Straße\nісторії'
    assert collection_search_file(path, 'a', 'Straße', collection='empty') == []
    for ws, collection in [('missing', 'c'), ('a','missing')]:
        with pytest.raises(ValueError):
            collection_search_file(path, ws, 'Straße', collection=collection)
    with store.transaction():
        Catalog(store.db, 'a').archive('c')
    with pytest.raises(ValueError):
        collection_search_file(path, 'a', 'Straße', collection='c')
    assert len(collection_search_file(path, 'a', 'Straße')) == 1


def test_removal_and_reextract_between_scope_and_source_do_not_mix(library, monkeypatch):
    store, path = library
    original = Catalog.editions
    changed = False

    def concurrent_update(catalog, collection=None):
        nonlocal changed
        ids = original(catalog, collection)
        if catalog.workspace == 'a' and not changed:
            changed = True
            with store.transaction():
                assert Catalog(store.db, 'a').remove('c', 'e')
                store.stage_revision('a', 'e', 'a2', ASSET, 'new extraction', [Page('new token')])
        return ids

    # Only the interleaving hook is patched. Both Catalog and search execute
    # actual SQL over independently connected disk-backed WAL transactions.
    monkeypatch.setattr(Catalog, 'editions', concurrent_update)
    hit, = collection_search_file(path, 'a', 'Straße', collection='c')
    assert hit['revision_id'] == 'a1' and hit['raw_quote'] == 'Straße'
    assert changed
    assert collection_search_file(path, 'a', 'new token', collection='c') == []
    assert collection_search_file(path, 'a', 'new token')[0]['revision_id'] == 'a2'
    assert collection_search_file(path, 'b', 'Straße', collection='c')[0]['revision_id'] == 'b1'


def test_caller_snapshot_requires_transaction_and_is_left_open(library):
    store, path = library
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(ValueError, match='transaction'):
            collection_search(db, 'a', 'Straße', collection='c')
        db.execute('BEGIN')
        assert collection_search(db, 'a', 'Straße', collection='c')
        assert db.in_transaction
        assert db.execute('SELECT 1').fetchone() == (1,)


def test_one_sticky_budget_includes_catalog_lookup(library, monkeypatch):
    _, path = library
    original = Catalog.editions
    cancelled = False

    def finish_then_cancel(catalog, collection=None):
        nonlocal cancelled
        rows = original(catalog, collection)
        cancelled = True
        return rows

    monkeypatch.setattr(Catalog, 'editions', finish_then_cancel)
    with pytest.raises(TimeoutError):
        collection_search_file(path, 'a', 'Straße', collection='c', cancelled=lambda: cancelled)
    with pytest.raises(TimeoutError):
        collection_search_file(path, 'a', '', collection='c', cancelled=lambda: True)


def test_nonexistent_file_is_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    with pytest.raises(sqlite3.OperationalError):
        collection_search_file(path, 'a', 'text')
    assert not path.exists()


def test_eligibility_filter_only_narrows_current_catalog_scope(library):
    store, _ = library
    with store.transaction():
        assert collection_search(store.db, 'a', 'Straße', collection='c',
                                 eligible_edition_ids=()) == []
        assert collection_search(store.db, 'a', 'Straße', collection='empty',
                                 eligible_edition_ids=('e',)) == []
        hits = collection_search(store.db, 'a', 'Straße', collection='c',
                                  eligible_edition_ids=('e', 'unrelated'))
        assert len(hits) == 1 and hits[0]['workspace_id'] == 'a'
        for invalid in ('e', (str(i) for i in range(501))):
            with pytest.raises(ValueError, match='eligible edition'):
                collection_search(store.db, 'a', 'Straße', eligible_edition_ids=invalid)
