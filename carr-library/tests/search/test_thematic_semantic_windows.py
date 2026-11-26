"""Synthetic tokenizer boundary tests; not real MiniLM inference."""
from types import SimpleNamespace
import pytest
from carr_library.search.semantic_windows import source_windows, TOKEN_LIMIT, _encoding


class TokenizerDouble:
    truncation = padding = None

    def encode(self, text, add_special_tokens=True):
        assert add_special_tokens
        # One token per non-whitespace codepoint, deliberately not a real model.
        offsets = [(i, i+1) for i, c in enumerate(text) if not c.isspace()]
        return SimpleNamespace(ids=list(range(len(offsets)+2)),
            offsets=[(0, 0), *offsets, (0, 0)],
            special_tokens_mask=[1, *([0]*len(offsets)), 1], overflowing=[])


@pytest.mark.parametrize('raw', [
    'railway ' * 80 + 'TAIL',
    'Ж' * 400 + 'остання частина',
    '中文。' * 130 + '末尾',
    'عربي ' * 100 + 'نهاية',
    'Cafe\u0301.\n\t' * 100 + 'koniec',
    '  ' + 'a' * 350 + '  ',
    '  \t\n',
])
def test_exact_partition_and_tail(raw):
    t = TokenizerDouble()
    spans = source_windows(t, raw)
    assert spans[0][0] == 0 and spans[-1][1] == len(raw)
    assert all(a < b for a, b in spans)
    assert all(b == c for (_, b), (c, _) in zip(spans, spans[1:]))
    assert ''.join(raw[a:b] for a, b in spans) == raw
    assert all(_encoding(t, raw[a:b])[0] <= TOKEN_LIMIT for a, b in spans)
    assert source_windows(t, raw) == spans


def test_special_tokens_count():
    t = TokenizerDouble()
    assert source_windows(t, 'a'*126) == ((0, 126),)
    assert len(source_windows(t, 'a'*127)) > 1


def test_enabled_truncation_rejected():
    t = TokenizerDouble()
    t.truncation = {'max_length': 128}
    with pytest.raises(ValueError, match='untruncated'):
        source_windows(t, 'a'*300)


def test_window_budget_rejected():
    with pytest.raises(ValueError, match='budget'):
        source_windows(TokenizerDouble(), 'a'*300, max_windows=1)


def test_bad_offsets_rejected():
    class Bad(TokenizerDouble):
        def encode(self, text, add_special_tokens=True):
            enc = super().encode(text, add_special_tokens)
            enc.offsets[1] = (0, len(text)+1)
            return enc
    with pytest.raises(ValueError, match='offsets'):
        source_windows(Bad(), 'č')


def test_unsplittable_rejected():
    class Bad(TokenizerDouble):
        def encode(self, text, add_special_tokens=True):
            return SimpleNamespace(ids=list(range(129)), offsets=[(0,1)]*129,
                                   special_tokens_mask=[0]*129, overflowing=[])
    with pytest.raises(ValueError, match='budget'):
        source_windows(Bad(), 'abc')


def test_cancelled_before_encoding():
    def check():
        raise TimeoutError('cancelled')
    with pytest.raises(TimeoutError):
        source_windows(TokenizerDouble(), 'abc', check=check)


def _passage(raw, workspace='A'):
    from hashlib import sha256
    thematic = pytest.importorskip('carr_library.search.thematic')
    return thematic.Passage(workspace, 'work', 'edition', 'revision', 'p1', 3,
                            0, len(raw), raw, sha256(raw.encode()).hexdigest())


def test_real_hybrid_handoff_with_synthetic_ranker():
    from carr_library.search.semantic_windows import search_windowed
    raw = 'ordinary '*300 + 'Залізниця сполучала міста.'
    p = _passage(raw)
    def rank(query, candidates):
        assert all(_encoding(TokenizerDouble(), text)[0] <= TOKEN_LIMIT
                   for _, text in candidates)
        return [key for key, text in candidates if 'Залізниця' in text]
    hits = search_windowed('A', [p], 'railway', tokenizer=TokenizerDouble(),
                           semantic_ranker=rank)
    assert len(hits) == 1
    h = hits[0]
    assert 'Залізниця' in h['raw_quote']
    assert h['raw_quote'] == raw[h['start']:h['end']]
    assert h['parent_passage_id'] == 'p1' and h['page_sha256'] == p.page_sha256
    assert h['physical_index'] == 3


def test_foreign_scope_before_encoding():
    from carr_library.search.semantic_windows import search_windowed
    p = _passage('private', 'B')
    class Never(TokenizerDouble):
        def encode(self, *args, **kwargs):
            pytest.fail('foreign scope reached tokenizer')
    with pytest.raises(ValueError, match='foreign'):
        search_windowed('A', [p], 'railway', tokenizer=Never(), semantic_ranker=lambda *_: [])


def test_overlong_query_before_ranking():
    from carr_library.search.semantic_windows import search_windowed
    p = _passage('railway')
    def never(*args):
        pytest.fail('overlong query reached ranker')
    with pytest.raises(ValueError, match='query exceeds'):
        search_windowed('A', [p], 'a'*127, tokenizer=TokenizerDouble(), semantic_ranker=never)
