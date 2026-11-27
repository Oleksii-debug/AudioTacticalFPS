"""Cache/ranker integration with explicit deterministic unit-test vectors.

These tests exercise the real runtime control flow, not neural model quality.
Actual MiniLM persistence is tested separately in e2e/test_semantic_runtime.py.
"""
from hashlib import sha256
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from carr_library.semantic import APPROVED_MANIFEST, OfflineSemanticRanker
from carr_library.semantic_cache import CacheBudget, EmbeddingCache, content_identity, model_identity


class CoverageTokenizer:
    truncation = None
    padding = None

    def encode(self, text, add_special_tokens=True):
        spans = [match.span() for match in re.finditer(r'\S+', text)]
        ids = [1, *range(3, len(spans) + 3), 2]
        return SimpleNamespace(ids=ids, overflowing=[], attention_mask=[1] * len(ids),
                               offsets=[(0, 0), *spans, (0, 0)],
                               special_tokens_mask=[1, *([0] * len(spans)), 1])


class DeterministicTestEngine:
    """Counts actual embed calls; values deliberately are not language embeddings."""
    def __init__(self):
        self.model = SimpleNamespace(tokenizer=CoverageTokenizer())
        self.calls = []

    def embed(self, texts, batch_size):
        self.calls.append(tuple(texts))
        for text in texts:
            digest = sha256(text.encode()).digest()
            yield tuple(float(digest[index % len(digest)] + 1) for index in range(384))


def fixture_ranker(root, budget=None):
    # Do not falsely label this fixture as actual MiniLM initialization/inference.
    ranker = object.__new__(OfflineSemanticRanker)
    ranker.tokenizer = CoverageTokenizer()
    ranker.engine = DeterministicTestEngine()
    ranker.minimum_similarity = 0.0
    ranker.bind_cache(root, budget=budget)
    return ranker


@pytest.fixture
def candidates():
    return ((('workspace', 'edition-1', 'revision', 'window-1'), 'Railways connected towns.'),
            (('workspace', 'edition-2', 'revision', 'window-1'), 'Botanists studied flowers.'))


def test_exact_reopened_cache_avoids_all_repeated_inference(tmp_path, candidates):
    cold = fixture_ranker(tmp_path)
    expected = cold.rank('Industrial transport', candidates)
    assert cold.cache_statistics == {'hits': 0, 'misses': 3, 'embedded_texts': 3, 'available': True}
    reopened = fixture_ranker(tmp_path)
    with patch.object(reopened.engine, 'embed', side_effect=AssertionError('unexpected repeated inference')):
        assert reopened.rank('Industrial transport', candidates) == expected
    assert reopened.cache_statistics == {'hits': 3, 'misses': 0, 'embedded_texts': 0, 'available': True}
    raw = reopened.cache.path.read_bytes()
    assert b'Railways connected towns.' not in raw
    assert b'Industrial transport' not in raw
    assert b'edition-1' not in raw


@pytest.mark.parametrize('change', ['text', 'workspace', 'edition', 'revision', 'window'])
def test_changed_source_identity_recomputes_only_changed_vector(tmp_path, candidates, change):
    ranker = fixture_ranker(tmp_path)
    ranker.rank('Industrial transport', candidates)
    key, text = candidates[0]
    if change == 'text':
        text += ' Updated source.'
    else:
        position = {'workspace': 0, 'edition': 1, 'revision': 2, 'window': 3}[change]
        key = tuple(value + '-new' if index == position else value for index, value in enumerate(key))
    changed = ((key, text), candidates[1])
    ranker.engine.calls.clear()
    result = ranker.rank('Industrial transport', changed)
    assert set(result) == {row[0] for row in changed}
    assert ranker.engine.calls == [(text,)]
    assert ranker.cache_statistics['hits'] == 2


def test_new_query_reuses_only_current_authorized_candidates(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path)
    ranker.rank('Industrial transport', candidates)
    ranker.engine.calls.clear()
    assert ranker.rank('Gardens', candidates[1:]) == [candidates[1][0]]
    assert ranker.engine.calls == [('Gardens',)]
    assert ranker.cache_statistics['hits'] == 1


def test_complete_token_coverage_is_checked_even_for_cached_text(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path)
    ranker.rank('Industrial transport', candidates)
    ranker.tokenizer.truncation = {'max_length': 1}
    with pytest.raises(ValueError, match='Untruncated'):
        ranker.rank('Industrial transport', candidates)


def test_model_runtime_and_preprocessing_contracts_are_separate(tmp_path):
    base = model_identity(APPROVED_MANIFEST)
    assert base != model_identity(APPROVED_MANIFEST, runtime='another-runtime')
    assert base != model_identity(APPROVED_MANIFEST, contract='another-normalization')
    cache = EmbeddingCache(tmp_path / 'cache', base)
    key = content_identity(base, 'Source', ('workspace', 'edition', 'revision', 'window'))
    assert cache.put_many({key: (1.0,) + (0.0,) * 383})
    different = EmbeddingCache(tmp_path / 'cache', model_identity(APPROVED_MANIFEST, runtime='other'))
    assert different.get_many([key]) == {}


def test_corrupt_vector_is_recomputed_without_accepting_its_rank(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path)
    expected = ranker.rank('Industrial transport', candidates)
    key = content_identity(ranker.cache.model, candidates[0][1], candidates[0][0])
    with sqlite3.connect(ranker.cache.path) as db:
        db.execute('UPDATE embedding SET vector=? WHERE identity=?', (b'\x00' * 3072, key))
    ranker.engine.calls.clear()
    assert ranker.rank('Industrial transport', candidates) == expected
    assert ranker.engine.calls == [(candidates[0][1],)]
    assert ranker.cache_statistics['hits'] == 2
    assert ranker.cache.available


def test_bad_sqlite_file_degrades_to_real_inference_flow(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path)
    expected = ranker.rank('Industrial transport', candidates)
    ranker.cache.path.write_bytes(b'not an SQLite database')
    ranker.engine.calls.clear()
    assert ranker.rank('Industrial transport', candidates) == expected
    assert len(ranker.engine.calls[0]) == 3
    assert not ranker.cache.available
    assert ranker.cache.last_error == 'DatabaseError'


def test_cancellation_after_cache_read_does_not_start_inference(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path)
    ranker.rank('Industrial transport', candidates)
    stopped = [False]
    original = ranker.cache.get_many

    def read_then_cancel(keys):
        result = original(keys)
        stopped[0] = True
        return result

    with patch.object(ranker.cache, 'get_many', side_effect=read_then_cancel), \
            patch.object(ranker.engine, 'embed', side_effect=AssertionError('must not infer after cancel')):
        with pytest.raises(TimeoutError):
            ranker.rank('Industrial transport', candidates, cancelled=lambda: stopped[0])


def test_small_entry_budget_is_honored_without_dropping_search_candidates(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path, CacheBudget(max_entries=1))
    assert len(ranker.rank('Industrial transport', candidates)) == 2
    with sqlite3.connect(ranker.cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0] == 1


def test_byte_budget_evicts_and_keeps_cache_available(tmp_path):
    cache = EmbeddingCache(tmp_path / 'cache', 'a' * 64,
                           budget=CacheBudget(max_bytes=1024 * 1024))
    vector = (1.0,) + (0.0,) * 383
    for batch in range(3):
        values = {sha256(f'{batch}:{i}'.encode()).hexdigest(): vector for i in range(511)}
        assert cache.put_many(values)
        assert cache.available
        assert cache.path.stat().st_size <= cache.budget.max_bytes
        assert cache.get_many([next(reversed(values))])


def test_eviction_order_survives_coarse_or_backward_wall_clock(tmp_path):
    cache = EmbeddingCache(tmp_path / 'cache', 'e' * 64, budget=CacheBudget(max_entries=2))
    vector = (1.0,) + (0.0,) * 383
    with patch('carr_library.semantic_cache.time_ns', return_value=100):
        assert cache.put_many({'a' * 64: vector, 'b' * 64: vector})
        assert cache.get_many(['a' * 64])
    with patch('carr_library.semantic_cache.time_ns', return_value=50):
        assert cache.put_many({'c' * 64: vector})
        assert cache.get_many(['a' * 64, 'b' * 64, 'c' * 64]) == {
            'a' * 64: vector, 'c' * 64: vector}


def test_cache_batch_failure_never_publishes_partial_embeddings(tmp_path, candidates):
    ranker = fixture_ranker(tmp_path)
    with patch.object(ranker.engine, 'embed', return_value=[(1.0,) + (0.0,) * 383]):
        with pytest.raises(ValueError, match='count mismatch'):
            ranker.rank('Industrial transport', candidates)
    with sqlite3.connect(ranker.cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0] == 0


def test_library_automatically_binds_derived_cache_after_validating_storage(tmp_path):
    from carr_library.application import LibraryApp
    ranker = fixture_ranker(tmp_path)
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    assert app.semantic_cache.directory == app.root / 'semantic-cache'
    assert not app.semantic_cache.directory.exists()
    assert ranker.cache.directory == tmp_path / 'semantic-cache'


def test_shared_model_keeps_two_live_libraries_caches_separate(tmp_path):
    from carr_library.application import LibraryApp
    ranker = fixture_ranker(tmp_path)
    first = LibraryApp(tmp_path / 'first', semantic_ranker=ranker)
    second = LibraryApp(tmp_path / 'second', semantic_ranker=ranker)
    workspace = first.create_workspace('First private library')
    first.add_text(workspace, 'First source', 'Railway transport connected cities.')
    hits = first.search(workspace, 'transport', mode='semantic')
    assert len(hits) == 1 and hits[0].verified
    assert first.semantic_cache.path.is_file()
    assert not second.semantic_cache.directory.exists()
    assert not ranker.cache.directory.exists()
    other = second.create_workspace('Second private library')
    second.add_text(other, 'Second source', 'Botanists studied flowers.')
    second.search(other, 'flowers', mode='semantic')
    assert second.semantic_cache.path.is_file()
    with sqlite3.connect(first.semantic_cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0] == 2
    with sqlite3.connect(second.semantic_cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0] == 2
    ranker.engine.calls.clear()
    assert first.search(workspace, 'transport', mode='semantic')[0].quote == hits[0].quote
    assert ranker.engine.calls == []
