"""Synthetic backend results; not a real model inference benchmark."""
from dataclasses import replace
from hashlib import sha256

import pytest

from carr_library.search.thematic import Passage, search, search_hybrid
from carr_library.search import thematic


def passage(pid, text, workspace='A', revision='r1', physical=1):
    raw = 'Before. ' + text + ' After.'
    return Passage(workspace, pid, 'ed-'+pid, revision, pid, physical,
                   8, 8+len(text), raw, sha256(raw.encode()).hexdigest(),
                   1 if physical is None else None)


P = passage('one', 'Railway transport.')
Q = passage('two', 'Залізниця та Cafe\u0301.', physical=None)
R = passage('three', 'A garden with flowers.')
K = ('A', 'ed-two', 'r1', 'two')


def test_semantic_only_and_exact_slice():
    def rank(query, items):
        assert query == 'railway'
        assert dict(items)[K] == 'Залізниця та Cafe\u0301.'
        return [K, ('A', 'ed-one', 'r1', 'one')]
    hits = search_hybrid('A', [P, Q, R], 'railway', semantic_ranker=rank)
    assert [h['passage_id'] for h in hits] == ['one', 'two']
    hit = hits[1]
    assert hit['raw_quote'] == Q.raw_page[hit['start']:hit['end']]
    assert hit['page_sha256'] == Q.page_sha256
    assert hit['physical_index'] is None and hit['segment_index'] == 1
    assert hit['matched_terms'] == () and 'printed_label' not in hit


@pytest.mark.parametrize('ranker', [None, lambda q, p: []])
@pytest.mark.parametrize('clock', [None, 4.001])
def test_fallback_is_exact_baseline(ranker, clock, monkeypatch):
    if clock is not None:
        monkeypatch.setattr(thematic, 'monotonic', lambda: clock)
    assert search_hybrid('A', [P, Q], 'railway', semantic_ranker=ranker,
                         timeout=60.0) == search('A', [P, Q], 'railway')


@pytest.mark.parametrize('bad', [('B', 'ed-two', 'r1', 'two'),
    ('A', 'ed-two', 'old', 'two'), ('A', 'ed-other', 'r1', 'two'),
    ('A', 'ed-two', 'r1', 'missing')])
def test_backend_key_must_be_authorized(bad):
    with pytest.raises(ValueError):
        search_hybrid('A', [P, Q], 'railway', semantic_ranker=lambda q,p:[bad])


@pytest.mark.parametrize('bad', [replace(Q, workspace='B'),
    replace(Q, page_sha256='0'*64), replace(Q, start=-1),
    replace(Q, revision_id=''), replace(Q, physical_index=0)])
def test_validate_before_model(bad):
    def forbidden(*args):
        pytest.fail('unverified source reached backend')
    with pytest.raises(ValueError):
        search_hybrid('A', [P, bad], 'railway', semantic_ranker=forbidden)


def test_no_match_can_return_empty():
    assert search_hybrid('A', [Q], 'railway', semantic_ranker=lambda q,p:[]) == []
    assert search_hybrid('A', [], 'railway', semantic_ranker=lambda q,p:pytest.fail()) == []
    assert search_hybrid('A', [P], ' ', semantic_ranker=lambda q,p:pytest.fail()) == []


def test_cancel_after_model():
    cancelled = False
    def rank(q, p):
        nonlocal cancelled
        cancelled = True
        return [K]
    with pytest.raises(TimeoutError):
        search_hybrid('A', [Q], 'railway', semantic_ranker=rank, cancelled=lambda:cancelled)


def test_budget_before_model():
    with pytest.raises(ValueError, match='budget'):
        search_hybrid('A', (P for _ in range(501)), 'railway',
                      semantic_ranker=lambda q,p:pytest.fail())
