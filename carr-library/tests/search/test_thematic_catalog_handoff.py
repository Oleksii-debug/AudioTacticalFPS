"""Cross-PR candidate tests. Requires real PR53 catalog and PR21 storage.

A standalone PR25 checkout cannot satisfy this integration suite; skips are
not an acceptance result. Assemble the pinned candidate files before running.
"""
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from carr_library.search.thematic import search_active, search_catalog


@pytest.fixture
def library(tmp_path):
    catalog = pytest.importorskip('carr_library.catalog.membership')
    revisions = pytest.importorskip('carr_library.storage.revisions')
    storage = revisions.Storage(tmp_path / 'library.db', schema_version=2)
    db = storage.db
    try:
        with storage.transaction():
            for ws in ('A', 'B'):
                db.execute('INSERT INTO workspace VALUES (?)', (ws,))
                c = catalog.Catalog(db, ws)
                c.add_work('work', 'Same title', ('Author ' + ws,))
                c.add_edition('e', 'work', {'sha256': ws.lower()*64, 'mime': 'text/plain', 'size': 5})
                c.create_collection('c', 'First')
                c.create_collection('d', 'Second')
                c.assign('c', 'e')
                c.assign('d', 'e')
            c = catalog.Catalog(db, 'A')
            c.add_work('other', 'Same title', ('Third author',))
            c.add_edition('other', 'other', {'sha256': 'c'*64, 'mime': 'text/plain', 'size': 5})
            c.create_collection('empty', 'Empty')
        storage.publish_revision('A', 'e', 'r1', 'a'*64, 'cfg', [revisions.Page('Railway old.')])
        storage.publish_revision('A', 'e', 'r2', 'a'*64, 'cfg', [revisions.Page('Railway Cafe\u0301. Залізниця.')])
        storage.publish_revision('B', 'e', 'rB', 'b'*64, 'cfg', [revisions.Page('Railway '*100)])
        storage.publish_revision('A', 'other', 'rOther', 'c'*64, 'cfg', [revisions.Page('Railway other.', 1)])
        yield storage, catalog.Catalog, revisions.Page
    finally:
        storage.close()


def test_collection_scope_and_exact_revision(library):
    storage, _, _ = library
    db = storage.db
    with storage.transaction():
        hit, = search_catalog(db, 'A', 'railway', collection='c')
        assert hit['workspace_id'] == 'A' and hit['revision_id'] == 'r2'
        assert hit['asset_sha256'] == 'a'*64 and hit['segment_index'] == 1
        assert hit['physical_index'] is None and hit['printed_label'] is None
        assert hit['raw_quote'] == 'Railway Cafe\u0301. Залізниця.'
        assert hit['raw_text_sha256'] == sha256(hit['raw_quote'].encode()).hexdigest()
        assert search_catalog(db, 'A', 'railway', collection='d') == [hit]
        assert search_catalog(db, 'A', 'railway', collection='empty') == []
        assert {h['edition_id'] for h in search_catalog(db, 'A', 'railway')} == {'e', 'other'}
        assert search_catalog(db, 'B', 'railway', collection='c')[0]['revision_id'] == 'rB'


def test_membership_remove_and_archive(library):
    storage, Catalog, _ = library
    db = storage.db
    with storage.transaction():
        c = Catalog(db, 'A')
        assert c.remove('c', 'e')
        assert search_catalog(db, 'A', 'railway', collection='c') == []
        assert search_catalog(db, 'A', 'railway', collection='d')
        c.archive('d')
        with pytest.raises(ValueError, match='archived'):
            search_catalog(db, 'A', 'railway', collection='d')
        assert db.in_transaction
    assert storage.get_page('A', 'r2', 1) is not None


def test_asset_mismatch_fails_even_for_nonmatching_text(library):
    storage, _, Page = library
    db = storage.db
    # The shared schema rejects a mismatched asset at the publication boundary.
    with pytest.raises(sqlite3.IntegrityError):
        storage.publish_revision('A', 'e', 'wrong', 'f'*64, 'cfg', [Page('Unrelated topic.')])
    assert db.execute("SELECT 1 FROM revision WHERE id='wrong'").fetchone() is None
    # Fault injection models an externally corrupted file, not an authorized
    # publication. The read adapter still must reject it before ranking.
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN')
    raw = 'Unrelated topic.'
    db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', ('wrong', 'A', 'e', 'f'*64, 'cfg'))
    db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) '
               'VALUES(?,1,NULL,NULL,0,?,?)', ('wrong', raw, sha256(raw.encode()).hexdigest()))
    db.execute('INSERT INTO sealed VALUES(?)', ('wrong',))
    db.execute("UPDATE active SET revision='wrong' WHERE workspace='A' AND edition='e'")
    db.commit()
    db.execute('PRAGMA foreign_keys=ON')
    with storage.transaction():
        # The previous adapter accepts the wrong asset for the authorized ID.
        hit, = search_active(db, 'A', {'e': 'work'}, 'unrelated')
        assert hit['asset_sha256'] == 'f'*64
        with pytest.raises(ValueError, match='asset differs'):
            search_catalog(db, 'A', 'railway', collection='c')
        with pytest.raises(ValueError, match='asset differs'):
            search_catalog(db, 'A', 'unrelated', collection='c')


def test_snapshot_and_cancellation(library):
    storage, Catalog, _ = library
    db = storage.db
    with pytest.raises(ValueError, match='snapshot'):
        search_catalog(db, 'A', 'railway')
    db.execute('BEGIN')
    try:
        Catalog(db, 'A').remove('c', 'e')
        assert search_catalog(db, 'A', 'railway', collection='c') == []
        with pytest.raises(TimeoutError):
            search_catalog(db, 'A', 'railway', cancelled=lambda: True)
        assert db.in_transaction
    finally:
        db.rollback()
    with storage.transaction():
        assert search_catalog(db, 'A', 'railway', collection='c')
        with pytest.raises(ValueError, match='unknown workspace'):
            search_catalog(db, 'missing', 'railway')
        with pytest.raises(ValueError, match='unknown or archived collection'):
            search_catalog(db, 'A', 'railway', collection='missing')
