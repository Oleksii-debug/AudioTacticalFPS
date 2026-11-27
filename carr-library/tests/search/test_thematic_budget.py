"""Cancellation must not be reported as a successful search."""
from dataclasses import replace
from hashlib import sha256

import pytest
from carr_library.search import thematic


def page(raw='railway bridge'):
    return thematic.Passage('A', 'work', 'edition', 'rev', 'p', 2,
                            0, len(raw), raw, sha256(raw.encode()).hexdigest())


@pytest.mark.parametrize('limit', [True, 1.5, 0, 101])
def test_invalid_limit(limit):
    with pytest.raises(ValueError):
        thematic.search('A', [page()], 'railway', limit=limit)


@pytest.mark.parametrize('timeout', [True, 0, -1, float('nan'), float('inf'), 86401])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        thematic.search('A', [page()], 'railway', timeout=timeout)


@pytest.mark.parametrize('query', ['railway\x00bridge', 'railway\ud800'])
def test_invalid_query(query):
    with pytest.raises(ValueError):
        thematic.search('A', [page()], query)


def test_cancel_before_empty_scope():
    with pytest.raises(TimeoutError):
        thematic.search('A', [], 'railway', cancelled=lambda: True)


def test_cancel_while_consuming_scope():
    stop = False

    def passages():
        nonlocal stop
        yield page()
        stop = True

    with pytest.raises(TimeoutError):
        thematic.search('A', passages(), 'absent', cancelled=lambda: stop)


def test_timeout_after_last_input(monkeypatch):
    now = 0.0
    monkeypatch.setattr(thematic, 'monotonic', lambda: now, raising=False)

    def passages():
        nonlocal now
        yield page()
        now = 10.0

    with pytest.raises(TimeoutError):
        thematic.search('A', passages(), 'railway', timeout=1)


def test_native_progress_interrupt(monkeypatch):
    connect = thematic.sqlite3.connect
    calls = []

    class ObservedConnection(thematic.sqlite3.Connection):
        def set_progress_handler(self, callback, n):
            def wrapped():
                calls.append(True)
                return callback()
            return super().set_progress_handler(wrapped, n)

    monkeypatch.setattr(thematic.sqlite3, 'connect',
                        lambda *a, **kw: connect(*a, factory=ObservedConnection, **kw))
    with pytest.raises(TimeoutError):
        thematic.search('A', (replace(page(), passage_id=str(i)) for i in range(1000)), 'railway',
                        cancelled=lambda: bool(calls))
    assert calls


def test_success_preserves_raw_quote_and_score():
    p = page('Café. Railway bridge.')
    expected = thematic.search('A', [p], 'railway')
    assert thematic.search('A', [p], 'railway', timeout=60) == expected
    assert expected[0]['raw_quote'] == p.raw_page


@pytest.mark.parametrize('timeout', [61, 300, 86400])
def test_configured_longer_budget_preserves_scores_and_its_exact_deadline(monkeypatch, timeout):
    expected = thematic.search('A', [page()], 'railway')
    assert thematic.search('A', [page()], 'railway', timeout=timeout) == expected
    now = 0.0
    monkeypatch.setattr(thematic, 'monotonic', lambda: now)
    def passages():
        nonlocal now
        yield page()
        now = timeout + 1.0
    with pytest.raises(TimeoutError):
        thematic.search('A', passages(), 'railway', timeout=timeout)
