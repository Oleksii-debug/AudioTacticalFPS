"""Synthetic rank lists; no model inference or production authorization claim."""
import sqlite3

import pytest

from carr_library.search.thematic import fuse_scoped_rankings
from carr_library.search._vendor.rifflux.rrf import rrf_fuse

A = ('w', 'ed', 'r1', 'a')
B = ('w', 'ed', 'r1', 'b')
C = ('w', 'other', 'r1', 'c')


def test_upstream_formula_and_overlap():
    assert rrf_fuse({'a': ['1', '2'], 'b': ['2']}) == {
        '2': 1 / 62 + 1 / 61, '1': 1 / 61}
    ranked = fuse_scoped_rankings('w', [A, B, C], [A, B], [C, B])
    assert ranked[0] == (B, 2 / 62)
    assert [key for key, _ in ranked] == [B, A, C]


def test_fallback_and_empty():
    assert [k for k, _ in fuse_scoped_rankings('w', [A, B], [B, A], [])] == [B, A]
    assert fuse_scoped_rankings('w', [A], [], []) == []
    assert fuse_scoped_rankings('w', [], [], []) == []


def test_ties_and_limit():
    first = fuse_scoped_rankings('w', [A, B], [A], [B])
    assert first == fuse_scoped_rankings('w', [B, A], [B], [A])
    assert first[0][0] == A
    assert fuse_scoped_rankings('w', [A, B], [A], [B], limit=1) == first[:1]


@pytest.mark.parametrize('bad', [('other', 'ed', 'r1', 'a'),
    ('w', 'ed', 'old', 'a'), ('w', 'foreign-ed', 'r1', 'a'),
    ('w', 'ed', 'r1', 'unknown'), ('w', 'ed', 'r1', ''), 'a'])
def test_reject_unapproved_semantic_keys(bad):
    with pytest.raises(ValueError):
        fuse_scoped_rankings('w', [A], [A], [bad])


@pytest.mark.parametrize('allowed,lexical,semantic', [([A, A], [], []),
    ([A], [A, A], []), ([A], [], [A, A]), ([A], [B], []),
    ([('other', 'ed', 'r1', 'a')], [], [])])
def test_reject_duplicate_and_foreign_inputs(allowed, lexical, semantic):
    with pytest.raises(ValueError):
        fuse_scoped_rankings('w', allowed, lexical, semantic)


@pytest.mark.parametrize('limit', [0, 101, True, 1.5])
def test_limit(limit):
    with pytest.raises(ValueError):
        fuse_scoped_rankings('w', [A], [A], [], limit=limit)


def test_native_fts_rank_handoff():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE VIRTUAL TABLE hits USING fts5(raw)')
        raw = ['railway railway transport', 'railway transport', 'залізничний транспорт']
        db.executemany('INSERT INTO hits(raw) VALUES(?)', [(v,) for v in raw])
        keys = [A, B, C]
        lexical = [keys[rowid-1] for rowid, in db.execute(
            'SELECT rowid FROM hits WHERE hits MATCH ? ORDER BY bm25(hits),rowid', ('railway',))]
        # A synthetic semantic ranking, NOT actual embeddings.
        ranked = fuse_scoped_rankings('w', keys, lexical, [B, C])
        assert ranked[0][0] == B
        assert {key for key, _ in ranked} == set(keys)
        assert db.execute('SELECT raw FROM hits ORDER BY rowid').fetchall() == [(v,) for v in raw]
