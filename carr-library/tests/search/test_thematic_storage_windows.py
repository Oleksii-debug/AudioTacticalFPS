"""Cross-PR V2 storage/catalog and windowed search; synthetic model boundary.

Requires PR21 54497fb20487850939d6d3eb49c8758a7fc047b8,
PR53 9c56900cf1b932428ad3427c74fc7a4b34cfc221 and PR25.
Use real SQLite on disk; no model weights or native tokenizer are exercised.
"""
from hashlib import sha256
from types import SimpleNamespace

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.storage.revisions import Storage, Page
from carr_library.search.semantic_windows import source_windows
from carr_library.search.thematic import search_catalog


class TokenizerDouble:
    truncation = padding = None

    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens
        offsets = [(i, i + 1) for i, c in enumerate(text) if not c.isspace()]
        return SimpleNamespace(ids=list(range(len(offsets) + 2)),
            offsets=[(0, 0), *offsets, (0, 0)],
            special_tokens_mask=[1, *([0] * len(offsets)), 1], overflowing=[])


RAW = 'x ' * 70 + 'railway industry' + ' x' * 70
TAIL = 'ordinary ' * 150 + 'Залізниця сполучала міста.'


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'library.db'
    storage = Storage(path, schema_version=2)
    try:
        db = storage.db
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        with storage.transaction():
            for ws in ('A', 'B'):
                db.execute('INSERT INTO workspace VALUES (?)', (ws,))
                cat = Catalog(db, ws)
                cat.add_work('w', 'Same title', ('Author ' + ws,))
                cat.add_edition('e', 'w', {'sha256': ws.lower() * 64,
                    'mime': 'text/plain', 'size': 10})
                cat.create_collection('c', 'Collection')
                cat.create_collection('empty', 'Empty')
                cat.assign('c', 'e')
            cat = Catalog(db, 'A')
            cat.add_work('other', 'Another work', ('Other author',))
            cat.add_edition('other', 'other', {'sha256': 'c' * 64,
                'mime': 'text/plain', 'size': 10})
        storage.publish_revision('A', 'e', 'r0', 'a' * 64, 'cfg', [Page('OLD PRIVATE')])
        storage.publish_revision('A', 'e', 'r1', 'a' * 64, 'cfg', [Page(RAW), Page(TAIL)])
        storage.publish_revision('B', 'e', 'rB', 'b' * 64, 'cfg', [Page('FOREIGN PRIVATE')])
        storage.publish_revision('A', 'other', 'rOther', 'c' * 64, 'cfg',
                                 [Page('Залізниця.', 7, 'iv', True)])
        storage.check()
        yield storage, path
    finally:
        storage.close()


def test_empty_semantic_preserves_whole_phrase(library):
    storage, _ = library
    t = TokenizerDouble()
    assert not any('railway industry' in RAW[a:b] for a, b in source_windows(t, RAW))
    with storage.transaction():
        expected = search_catalog(storage.db, 'A', '"railway industry"', collection='c')
        assert len(expected) == 1 and expected[0]['raw_quote'] == RAW
        actual = search_catalog(storage.db, 'A', '"railway industry"', collection='c',
                                tokenizer=t, semantic_ranker=lambda *_: [])
        assert actual == expected


def test_semantic_tail_retains_source_identity(library):
    storage, _ = library
    def rank(query, candidates):
        assert query == 'trains'
        assert all(k[:3] == ('A', 'e', 'r1') for k, _ in candidates)
        assert all('PRIVATE' not in text for _, text in candidates)
        return [k for k, text in candidates if 'Залізниця' in text]
    with storage.transaction():
        hit, = search_catalog(storage.db, 'A', 'trains', collection='c',
                              tokenizer=TokenizerDouble(), semantic_ranker=rank)
        page = storage.get_page('A', 'r1', 2)
        assert hit['page_id'] == page['id'] and hit['parent_passage_id'] == str(page['id'])
        assert hit['revision_id'] == 'r1' and hit['asset_sha256'] == 'a' * 64
        assert hit['locator_kind'] == 'logical_segment' and hit['segment_index'] == 2
        assert hit['physical_index'] is None and hit['printed_label'] is None
        assert not hit['printed_label_verified']
        assert hit['raw_quote'] == TAIL[hit['start']:hit['end']]
        assert 'Залізниця' in hit['raw_quote']
        assert hit['raw_text_sha256'] == sha256(TAIL.encode()).hexdigest()


def test_verified_physical_metadata(library):
    storage, _ = library
    with storage.transaction():
        hits = search_catalog(storage.db, 'A', 'trains', tokenizer=TokenizerDouble(),
            semantic_ranker=lambda q, rows: [k for k, _ in rows if k[1] == 'other'])
        hit, = hits
        assert hit['edition_id'] == 'other' and hit['physical_index'] == 7
        assert hit['locator_kind'] == 'physical_page' and hit['segment_index'] is None
        assert hit['printed_label'] == 'iv' and hit['printed_label_verified']
        assert hit['raw_quote'] == 'Залізниця.'


def test_same_snapshot_during_membership_and_revision_change(library):
    storage, path = library
    writer = Storage(path, schema_version=2)
    db = storage.db
    def rank(query, candidates):
        with writer.transaction():
            assert Catalog(writer.db, 'A').remove('c', 'e')
            writer.stage_revision('A', 'e', 'r2', 'a' * 64, 'cfg', [Page('NEW PRIVATE')])
        assert all(k[2] == 'r1' and 'NEW PRIVATE' not in text for k, text in candidates)
        return [k for k, text in candidates if 'Залізниця' in text]
    try:
        db.execute('BEGIN')
        hit, = search_catalog(db, 'A', 'trains', collection='c',
                              tokenizer=TokenizerDouble(), semantic_ranker=rank)
        assert db.in_transaction and hit['revision_id'] == 'r1'
        assert hit['raw_quote'] == TAIL[hit['start']:hit['end']]
        db.rollback()
        db.execute('BEGIN')
        assert search_catalog(db, 'A', 'trains', collection='c',
                              tokenizer=TokenizerDouble(), semantic_ranker=rank) == []
        assert storage.get_page('A', 'r1', 2)['raw'] == TAIL
    finally:
        if db.in_transaction:
            db.rollback()
        writer.close()


def test_invalid_rank_and_cancellation_fail_closed(library):
    storage, _ = library
    with storage.transaction():
        with pytest.raises(ValueError, match='foreign|unauthorized'):
            search_catalog(storage.db, 'A', 'trains', collection='c',
                tokenizer=TokenizerDouble(), semantic_ranker=lambda *_: [('B', 'e', 'rB', '1')])
        cancelled = False
        def rank(*_):
            nonlocal cancelled
            cancelled = True
            return []
        with pytest.raises(TimeoutError):
            search_catalog(storage.db, 'A', 'railway', collection='c', tokenizer=TokenizerDouble(),
                           semantic_ranker=rank, cancelled=lambda: cancelled)
        assert storage.db.in_transaction


def test_no_ranker_preserves_lexical_and_empty_scope(library):
    storage, _ = library
    class Never(TokenizerDouble):
        def encode(self, *args, **kwargs):
            pytest.fail('tokenizer called without an eligible model or source')
    with storage.transaction():
        expected = search_catalog(storage.db, 'A', 'railway', collection='c')
        assert expected == search_catalog(storage.db, 'A', 'railway', collection='c', tokenizer=Never())
        assert search_catalog(storage.db, 'A', 'railway', collection='empty',
            tokenizer=Never(), semantic_ranker=lambda *_: pytest.fail('empty scope reached ranker')) == []


def test_direct_window_fallback_preserves_phrase():
    from carr_library.search.thematic import Passage, search
    from carr_library.search.semantic_windows import search_windowed
    p = Passage('A', 'w', 'e', 'r', 'p', 1, 0, len(RAW), RAW,
                sha256(RAW.encode()).hexdigest())
    expected = search('A', [p], '"railway industry"')
    assert expected and search_windowed('A', [p], '"railway industry"',
        tokenizer=TokenizerDouble(), semantic_ranker=lambda *_: []) == expected


def test_ranker_must_not_end_snapshot(library):
    storage, _ = library
    db = storage.db
    db.execute('BEGIN')
    def rank(*_):
        db.rollback()
        return []
    try:
        with pytest.raises(ValueError, match='snapshot ended'):
            search_catalog(db, 'A', 'railway', collection='c',
                           tokenizer=TokenizerDouble(), semantic_ranker=rank)
    finally:
        if db.in_transaction:
            db.rollback()
