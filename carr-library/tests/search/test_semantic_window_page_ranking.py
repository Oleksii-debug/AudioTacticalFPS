"""Page retrieval must survive token window cuts and window-level truncation.

Most cases use deterministic tokenizer/ranker doubles, not model benchmarks.
The opt-in pinned-tokenizer case exercises real publisher token boundaries;
actual CPU inference is covered separately by tests/e2e/test_semantic_runtime.py.
"""
from hashlib import sha256
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from carr_library.search.semantic_windows import TOKEN_LIMIT, search_windowed, source_windows
from carr_library.search.thematic import Passage, search


class WordTokenizerDouble:
    truncation = padding = None

    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens
        offsets = [match.span() for match in re.finditer(r'\S+', text)]
        return SimpleNamespace(ids=list(range(len(offsets) + 2)),
            offsets=[(0, 0), *offsets, (0, 0)],
            special_tokens_mask=[1, *([0] * len(offsets)), 1], overflowing=[])


def passage(identity, body, *, prefix='', suffix=''):
    raw = prefix + body + suffix
    return Passage('A', 'work-' + identity, 'edition-' + identity,
        'revision-' + identity, identity, 1, len(prefix), len(prefix) + len(body),
        raw, sha256(raw.encode()).hexdigest())


def empty_ranker(tokenizer):
    def rank(query, candidates):
        assert all(len(tokenizer.encode(text).ids) <= TOKEN_LIMIT
                   for _, text in candidates)
        return []
    return rank


@pytest.mark.parametrize('query', ['railway bridge', '"railway bridge"'])
def test_lexical_match_crossing_windows_keeps_original_parent_span(query):
    tokenizer = WordTokenizerDouble()
    body = 'ordinary ' * 70 + 'railway bridge ' + 'ordinary ' * 70
    p = passage('boundary', body, prefix='Excluded preface. ', suffix=' Excluded afterword.')
    spans = source_windows(tokenizer, body)
    start, end = body.index('railway'), body.index('railway') + len('railway bridge')
    assert not any(a <= start and b >= end for a, b in spans)
    assert len(search('A', [p], query)) == 1

    hit, = search_windowed('A', [p], query, tokenizer=tokenizer,
                          semantic_ranker=empty_ranker(tokenizer))
    assert hit['parent_passage_id'] == p.passage_id
    assert (hit['start'], hit['end'], hit['raw_quote']) == (p.start, p.end, body)
    assert hit['page_sha256'] == p.page_sha256
    assert hit['raw_quote'] == p.raw_page[hit['start']:hit['end']]


def test_matching_bounded_tail_is_preferred_to_the_whole_parent():
    tokenizer = WordTokenizerDouble()
    body = 'ordinary ' * 300 + 'The distinct railway archive.'
    p = passage('tail', body)
    hit, = search_windowed('A', [p], 'distinct railway archive', tokenizer=tokenizer,
                          semantic_ranker=empty_ranker(tokenizer))
    assert hit['start'] > 2000
    assert 'distinct railway archive' in hit['raw_quote']
    assert hit['raw_quote'] == body[hit['start']:hit['end']]
    assert len(tokenizer.encode(hit['raw_quote']).ids) <= TOKEN_LIMIT


def test_semantic_windows_are_deduplicated_before_limiting_parent_pages():
    tokenizer = WordTokenizerDouble()
    first = passage('first', 'ordinary ' * 8200)
    second = passage('second', 'Another independently relevant page.')
    assert len(source_windows(tokenizer, first.raw_page)) > 100

    def rank(query, candidates):
        # Intentionally ranks all first-page windows before the second page.
        # This is a deterministic ranking regression, not real inference.
        assert all(len(tokenizer.encode(text).ids) <= TOKEN_LIMIT
                   for _, text in candidates)
        return [key for key, _ in candidates]

    hits = search_windowed('A', [first, second], 'trains', tokenizer=tokenizer,
                           semantic_ranker=rank, limit=2)
    assert [hit['parent_passage_id'] for hit in hits] == ['first', 'second']
    assert all(hit['raw_quote'] in {'first': first, 'second': second}[
        hit['parent_passage_id']].raw_page for hit in hits)


@pytest.mark.parametrize('kind', ['foreign', 'revision', 'duplicate'])
def test_model_window_keys_remain_authorized(kind):
    tokenizer = WordTokenizerDouble()
    p = passage('source', 'A railway archive.')

    def invalid_rank(query, candidates):
        key = candidates[0][0]
        if kind == 'duplicate':
            return [key, key]
        return [('foreign', *key[1:])] if kind == 'foreign' else [(*key[:2], 'old', key[3])]

    with pytest.raises(ValueError):
        search_windowed('A', [p], 'railway', tokenizer=tokenizer, semantic_ranker=invalid_rank)


def test_cancel_after_model_never_returns_lexical_fallback():
    tokenizer = WordTokenizerDouble()
    stopped = False

    def rank(query, candidates):
        nonlocal stopped
        stopped = True
        return []

    with pytest.raises(TimeoutError):
        search_windowed('A', [passage('source', 'A railway archive.')], 'railway',
                        tokenizer=tokenizer, semantic_ranker=rank, cancelled=lambda: stopped)


def test_actual_pinned_tokenizer_boundary_preserves_lexical_match():
    directory = os.environ.get('CARR_TEST_MODEL_DIR')
    if not directory:
        pytest.skip('Set CARR_TEST_MODEL_DIR for the real approved tokenizer regression.')
    from carr_library.semantic import verify_model_bundle
    from tokenizers import Tokenizer

    root = verify_model_bundle(Path(directory))
    tokenizer = Tokenizer.from_file(str(root / 'tokenizer.json'))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    raw = 'ordinary ' * 70 + 'railway bridge ' + 'ordinary ' * 70
    spans = source_windows(tokenizer, raw)
    assert spans == ((0, 321), (321, 638), (638, 957), (957, 1275))
    p = passage('real-boundary', raw)
    assert len(search('A', [p], 'railway bridge')) == 1
    hit, = search_windowed('A', [p], 'railway bridge', tokenizer=tokenizer,
                          semantic_ranker=empty_ranker(tokenizer))
    assert (hit['start'], hit['end'], hit['raw_quote']) == (0, len(raw), raw)
    assert hit['parent_passage_id'] == p.passage_id
