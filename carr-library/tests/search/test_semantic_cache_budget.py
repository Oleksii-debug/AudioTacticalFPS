"""Real SQLite width/deadline boundaries; synthetic vectors are not inference."""
from hashlib import sha256
import sqlite3
from time import monotonic
import tracemalloc

import pytest

from carr_library import semantic_cache as cache_module
from carr_library.semantic import OfflineSemanticRanker, _unit
from carr_library.semantic_cache import CacheBudget, EmbeddingCache


KEY = sha256(b'bounded synthetic source vector').hexdigest()
VECTOR = (1.0,) + (0.0,) * 383


def populated(tmp_path):
    cache = EmbeddingCache(tmp_path / 'cache', 'a' * 64,
        budget=CacheBudget(max_bytes=64 * 1024**2))
    assert cache.put_many({KEY: VECTOR})
    return cache


@pytest.mark.parametrize('column', ['vector', 'checksum'])
def test_wide_corrupt_cell_cannot_become_a_large_python_allocation(tmp_path, column):
    cache = populated(tmp_path)
    with sqlite3.connect(cache.path) as db:
        db.execute(f'UPDATE embedding SET {column}=zeroblob(?) WHERE identity=?', (16 * 1024**2, KEY))
    tracemalloc.start()
    try:
        assert cache.get_many([KEY]) == {}
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024**2, f'A corrupt cached vector allocated {peak} Python bytes.'


@pytest.mark.parametrize('operation', ['read', 'write'])
def test_actual_sqlite_execution_honors_search_deadline_without_disabling_cache(tmp_path, monkeypatch, operation):
    cache = populated(tmp_path)
    before = cache.path.read_bytes()
    original = sqlite3.connect
    entered = []

    class DeliberatelySlowSql(sqlite3.Connection):
        """Lengthen the selected read to exercise the real SQLite VM interrupt."""
        def execute(self, sql, parameters=()):
            selected = (sql.startswith('SELECT identity,vector,checksum') if operation == 'read'
                        else sql == 'SELECT COUNT(*) FROM embedding')
            if selected:
                entered.append(True)
                return super().execute('WITH RECURSIVE busy(x) AS '
                    '(SELECT 1 UNION ALL SELECT x+1 FROM busy WHERE x<100000000) '
                    'SELECT SUM(x) FROM busy')
            return super().execute(sql, parameters)

    monkeypatch.setattr(cache_module.sqlite3, 'connect',
        lambda *args, **kwargs: original(*args, **kwargs, factory=DeliberatelySlowSql))
    started, deadline = monotonic(), monotonic() + .03

    def check():
        if monotonic() >= deadline:
            raise TimeoutError('Selected search deadline expired.')

    with pytest.raises(TimeoutError, match='Selected search deadline'):
        if operation == 'read':
            cache.get_many([KEY], check=check)
        else:
            cache.put_many({KEY: VECTOR}, check=check)
    assert entered and monotonic() - started < 2
    assert cache.available and cache.path.read_bytes() == before
    monkeypatch.setattr(cache_module.sqlite3, 'connect', original)
    assert cache.get_many([KEY]) == {KEY: VECTOR}


@pytest.mark.parametrize('operation', ['read', 'write'])
def test_pre_cancel_creates_no_disposable_cache_file(tmp_path, operation):
    cache = EmbeddingCache(tmp_path / 'cache', 'a' * 64)

    def check():
        raise TimeoutError('Cancelled before cache access.')

    with pytest.raises(TimeoutError):
        if operation == 'read':
            cache.get_many([KEY], check=check)
        else:
            cache.put_many({KEY: VECTOR}, check=check)
    assert not cache.directory.exists() and cache.available


def test_ranker_bounds_a_lazy_candidate_stream_before_materialization():
    ranker, consumed = object.__new__(OfflineSemanticRanker), []

    def candidates():
        for index in range(1000):
            assert index <= 500, 'The ranker consumed past its one-excess candidate budget.'
            consumed.append(index)
            yield (('workspace', 'edition', 'revision', str(index)), 'Source')

    with pytest.raises(ValueError, match='candidate budget'):
        ranker.rank('Query', candidates())
    assert len(consumed) == 501


def test_runtime_vector_width_is_bounded_before_materialization():
    consumed = []

    def vector():
        for index in range(1000):
            assert index <= 384, 'The vector reader consumed beyond one excess dimension.'
            consumed.append(index)
            yield 1.0

    with pytest.raises(ValueError, match='Invalid MiniLM embedding'):
        _unit(vector())
    assert len(consumed) == 385
