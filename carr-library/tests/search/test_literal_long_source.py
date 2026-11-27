"""Unpaginated TXT source slices and real SQLite cancellation regressions."""
from hashlib import sha256
import sqlite3

import pytest

from carr_library.search import literal


@pytest.fixture
def source_db():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE active_page(id,workspace,edition,revision,asset,'
               'ordinal,physical,printed,verified,raw,digest)')
    try:
        yield db
    finally:
        db.close()


def add_source(db, raw):
    db.execute('INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (1, 'library', 'edition', 'revision', 'a' * 64, 1, None, None,
                0, raw, sha256(raw.encode('utf-8')).hexdigest()))


def test_long_ascii_book_keeps_exact_raw_slices_and_pagination(source_db):
    first = 'Unique\r\n\t target'
    second = 'UNIQUE  target'
    raw = ('A complete source line.\r\n\r\n' * 4000 + first + '\n' +
           'Ordinary source text.\t ' * 4000 + second + '\r\n')
    add_source(source_db, raw)
    hits = literal.search(source_db, 'library', 'unique target')
    assert [hit['raw_quote'] for hit in hits] == [first, second]
    for hit, expected in zip(hits, (first, second)):
        assert hit['start'] == raw.index(expected)
        assert hit['end'] == raw.index(expected) + len(expected)
        assert raw[hit['start']:hit['end']] == expected
        assert hit['raw_text_sha256'] == sha256(raw.encode()).hexdigest()
        assert hit['locator_kind'] == 'logical_segment'
    assert literal.search(source_db, 'library', 'unique target', limit=1,
                          offset=1) == hits[1:]
    terms = literal.search(source_db, 'library', 'target unique', mode='terms')
    assert len(terms) == 1
    assert [raw[start:end] for start, end in terms[0]['match_spans']] == ['Unique', 'target']


@pytest.mark.parametrize('padding', [' ', '\r\n\r\n', '\t\t', '\x1c\x1d'])
def test_long_ascii_boundaries_after_whitespace_runs(source_db, padding):
    raw = padding + ('ordinary' + padding) * 8000 + 'target' + padding
    add_source(source_db, raw)
    hits = literal.search(source_db, 'library', 'target', mode='word')
    assert len(hits) == 1
    assert hits[0]['start'] == raw.index('target')
    assert hits[0]['end'] == raw.index('target') + len('target')
    assert hits[0]['raw_quote'] == 'target'


def test_one_shot_cancel_during_fts_sql_stays_cancelled(source_db, monkeypatch):
    add_source(source_db, 'A literal source quotation.')
    state = {'in_sql': False, 'pulses': 0}
    real_connect = sqlite3.connect

    class InterruptibleIndex(sqlite3.Connection):
        def set_progress_handler(self, handler, steps):
            def progress():
                state['in_sql'] = True
                try:
                    return handler()
                finally:
                    state['in_sql'] = False
            # Exercise an actual SQLite interrupt, even for a small fixture.
            super().set_progress_handler(progress, 1)

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=InterruptibleIndex)

    def cancel_once():
        if state['in_sql'] and not state['pulses']:
            state['pulses'] += 1
            return True
        return False

    with monkeypatch.context() as patch:
        patch.setattr(literal.sqlite3, 'connect', connect)
        with pytest.raises(TimeoutError) as error:
            literal.search(source_db, 'library', 'quotation', cancelled=cancel_once)
    assert state['pulses'] == 1
    assert isinstance(error.value.__cause__, sqlite3.OperationalError)
    assert error.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
    assert literal.search(source_db, 'library', 'quotation')[0]['raw_quote'] == 'quotation'


@pytest.mark.parametrize('timeout', [True, '5', None, float('nan')])
def test_direct_search_rejects_invalid_timeout(source_db, timeout):
    with pytest.raises(ValueError, match='budget'):
        literal.search(source_db, 'library', 'quotation', timeout=timeout)


def test_long_unicode_source_keeps_exact_clusters_casefold_and_page_offsets(source_db):
    phrase = 'Straße\t  архів'
    tail = 'Київський архів'
    raw = ('Українська історія. ' + phrase + '.\n') * 3000 + tail + '.'
    assert len(raw) > 65536 and not raw.isascii()
    add_source(source_db, raw)
    first, = literal.search(source_db, 'library', 'STRASSE архів', limit=1)
    last, = literal.search(source_db, 'library', 'STRASSE архів', limit=1, offset=2999)
    unique, = literal.search(source_db, 'library', 'КИЇВСЬКИЙ АРХІВ')
    for hit, expected, at in ((first, phrase, raw.index(phrase)),
                              (last, phrase, raw.rindex(phrase)),
                              (unique, tail, raw.index(tail))):
        assert hit['raw_quote'] == raw[hit['start']:hit['end']] == expected
        assert (hit['start'], hit['end']) == (at, at + len(expected))
        assert hit['locator_kind'] == 'logical_segment'
        assert hit['raw_text_sha256'] == sha256(raw.encode()).hexdigest()
    assert literal.search(source_db, 'library', 'ss', mode='word') == []
    assert literal.search(source_db, 'library', 'Киівський', mode='word') == []


def test_unicode_mapping_peak_allocation_is_bounded_for_large_source():
    import tracemalloc

    raw = 'Українська історія. Straße\t  архів.\n' * 2200
    assert len(raw) > 65536
    tracemalloc.start()
    try:
        folded, starts, ends = literal._mapped(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # This covers Python allocations in offset mapping, not SQLite or total RSS.
    # Two per-character dictionaries exceed this budget by several multiples.
    assert peak < len(raw) * 32 + 2 * 1024 * 1024
    match, = tuple(literal._spans(folded, starts, ends, literal._fold('STRASSE архів')))[:1]
    assert raw[match[0]:match[1]] == 'Straße\t  архів'


def test_compact_unicode_boundaries_match_existing_small_source_contract(monkeypatch):
    from random import Random

    random = Random(20261126)
    clusters = ('a', 'Ж', 'ї', 'é', 'e\u0301', 'ß', 'İ', 'ﬃ', 'Σ', 'ς', '가',
                '\u0301\u0327', '🧑\u200d🔬', ' ', '\r\n\t', '\u2003', '\x1c')
    for _ in range(250):
        raw = 'Ж ' + ''.join(random.choice(clusters) for _ in range(random.randrange(1, 120)))
        monkeypatch.setattr(literal, '_LONG_ASCII', 65536)
        expected, expected_starts, expected_ends = literal._mapped(raw)
        monkeypatch.setattr(literal, '_LONG_ASCII', 1)
        folded, starts, ends = literal._mapped(raw)
        assert folded == expected
        assert dict(starts.items()) == expected_starts
        assert dict(ends.items()) == expected_ends


def test_cancel_during_large_unicode_mapping_remains_cooperative(source_db, monkeypatch):
    raw = 'Українська історія. Straße\t  архів.\n' * 2200
    add_source(source_db, raw)
    original = literal._fold
    calls = 0

    def fold(value):
        nonlocal calls
        if len(value) == 1:
            calls += 1
        return original(value)

    monkeypatch.setattr(literal, '_fold', fold)
    with pytest.raises(TimeoutError):
        literal.search(source_db, 'library', 'STRASSE архів', cancelled=lambda: calls >= 1)
    assert 1 <= calls <= 1024
