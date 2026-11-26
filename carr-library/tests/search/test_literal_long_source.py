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


@pytest.mark.parametrize('sample', ['Українська історія. Straße\t  архів.\n',
    'e\u0301lan İSTANBUL Σίσυφος ς Життя\n', '한글 தமிழ் 中文 研究\t\t'])
def test_compact_unicode_matches_cluster_reference_and_cancels(sample, monkeypatch):
    raw = sample * 3000
    with monkeypatch.context() as patch:
        patch.setattr(literal, '_LONG_ASCII', len(raw) + 1)
        folded, starts, ends = literal._mapped(raw)
    compact, actual_starts, actual_ends = literal._mapped(raw)
    assert compact == folded
    assert dict(actual_starts.items()) == starts
    assert dict(actual_ends.items()) == ends
    calls = []
    def cancel():
        calls.append(True)
        if len(calls) > 4:
            raise TimeoutError('cancelled')
    with pytest.raises(TimeoutError):
        literal._mapped(raw, cancel)


def test_long_unicode_native_trigram_source_exact(source_db):
    raw = 'Українська історія. Straße\t  архів.\n' * 3000 + 'Київський архів.'
    add_source(source_db, raw)
    hits = literal.search(source_db, 'library', 'КИЇВСЬКИЙ АРХІВ')
    assert len(hits) == 1 and hits[0]['raw_quote'] == 'Київський архів'
    assert raw[hits[0]['start']:hits[0]['end']] == 'Київський архів'
    hits = literal.search(source_db, 'library', 'STRASSE архів', limit=100)
    assert len(hits) == 100 and all(hit['raw_quote'] == 'Straße\t  архів' for hit in hits)
