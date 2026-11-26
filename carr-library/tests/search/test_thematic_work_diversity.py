"""Hybrid ranking must preserve work/edition diversity before the result cap."""
from hashlib import sha256
import re
from types import SimpleNamespace

import pytest

from carr_library.search.semantic_windows import search_windowed
from carr_library.search.thematic import Passage, search_hybrid


class WordTokenizer:
    truncation = padding = None

    def encode(self, text, add_special_tokens=True):
        offsets = [m.span() for m in re.finditer(r'\S+', text)]
        return SimpleNamespace(ids=list(range(len(offsets) + 2)),
            offsets=[(0, 0), *offsets, (0, 0)],
            special_tokens_mask=[1, *([0] * len(offsets)), 1], overflowing=[])


def p(work, edition, page, raw='Railway archive.'):
    return Passage('w', work, edition, 'r-' + edition, str(page), page,
                   0, len(raw), raw, sha256(raw.encode()).hexdigest())


def run(mode, rows, query, ranker, limit):
    if mode == 'windowed':
        return search_windowed('w', rows, query, tokenizer=WordTokenizer(),
                               semantic_ranker=ranker, limit=limit)
    return search_hybrid('w', rows, query, semantic_ranker=ranker, limit=limit)


def ranked_all(query, candidates):
    return [key for key, _ in candidates]


@pytest.mark.parametrize('mode', ['hybrid', 'windowed'])
@pytest.mark.parametrize('query', ['trains', 'railway'])
def test_work_diversity_before_limit(mode, query):
    rows = [p('A', 'a', 1), p('A', 'a', 2), p('B', 'b', 1)]
    hits = run(mode, rows, query, ranked_all, 2)
    assert [h['work_id'] for h in hits] == ['A', 'B']
    for hit in hits:
        source = next(row for row in rows if row.edition_id == hit['edition_id']
                      and row.physical_index == hit['physical_index'])
        assert hit['raw_quote'] == source.raw_page[hit['start']:hit['end']]
        assert hit['page_sha256'] == source.page_sha256


@pytest.mark.parametrize('mode', ['hybrid', 'windowed'])
def test_edition_diversity_when_only_one_work(mode):
    rows = [p('A', 'a', 1), p('A', 'a', 2), p('A', 'b', 1)]
    hits = run(mode, rows, 'trains', ranked_all, 2)
    assert [h['edition_id'] for h in hits] == ['a', 'b']


@pytest.mark.parametrize('mode', ['hybrid', 'windowed'])
def test_diversity_sees_beyond_first_100_ranked_pages(mode):
    rows = [p('A', 'a', i) for i in range(1, 111)] + [p('B', 'b', 1)]
    hits = run(mode, rows, 'trains', ranked_all, 2)
    assert [h['work_id'] for h in hits] == ['A', 'B']


@pytest.mark.parametrize('mode', ['hybrid', 'windowed'])
def test_empty_ranker_keeps_lexical_order_and_text(mode):
    rows = [p('A', 'a', 1), p('A', 'a', 2), p('B', 'b', 1)]
    hits = run(mode, rows, 'railway', lambda q, c: [], 2)
    assert [h['work_id'] for h in hits] == ['A', 'B']
    assert all(h['raw_quote'] == 'Railway archive.' for h in hits)


@pytest.mark.parametrize('mode', ['hybrid', 'windowed'])
def test_foreign_rank_key_still_rejected(mode):
    rows = [p('A', 'a', 1), p('B', 'b', 1)]
    with pytest.raises(ValueError, match='foreign'):
        run(mode, rows, 'trains', lambda q, c: [('foreign', *c[0][0][1:])], 2)
