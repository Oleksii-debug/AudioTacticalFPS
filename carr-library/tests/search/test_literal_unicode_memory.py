"""Long multilingual sources retain exact cluster boundaries without dict fanout."""
from hashlib import sha256
import random
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.search import literal
from carr_library.search.presentation import _reverse_offsets


def test_long_unicode_native_trigram_preserves_source_exact_spans():
    raw = 'Українська історія. Straße\t  архів.\n' * 3000 + 'Київський архів.'
    folded, starts, ends = literal._mapped(raw)
    assert isinstance(starts, literal._CompactOffsets)
    assert isinstance(ends, literal._CompactOffsets)
    # Bound offset storage itself independently of allocator/runtime RSS.
    assert starts.values.itemsize * len(starts.values) <= 4 * (len(folded) + 1)
    db = sqlite3.connect(':memory:')
    try:
        db.execute("CREATE VIRTUAL TABLE source USING fts5(text, tokenize='trigram case_sensitive 1')")
        db.execute('INSERT INTO source VALUES (?)', (folded,))
        for query, expected, count in [('STRASSE архів', 'Straße\t  архів', 3000),
                                       ('КИЇВСЬКИЙ АРХІВ', 'Київський архів', 1)]:
            needle = literal._fold(query)
            assert db.execute('SELECT rowid FROM source WHERE source MATCH ?',
                              ('"' + needle + '"',)).fetchall() == [(1,)]
            spans = list(literal._spans(folded, starts, ends, needle))
            assert len(spans) == count
            assert all(raw[a:b] == expected for a, b in spans)
    finally:
        db.close()


def test_compact_unicode_mapping_matches_existing_cluster_oracle(monkeypatch):
    rng = random.Random(762413)
    pieces = ['ї', 'і\u0308', 'é', 'e\u0301', 'ß', 'İ', '\r\n\t ', 'Ж',
              '中', '한', 'a\u0301\u0323', 'word', ' ', '👩\u200d🔬', '.']
    for _ in range(250):
        raw = 'Ж' + ''.join(rng.choice(pieces) for _ in range(rng.randrange(1, 90)))
        monkeypatch.setattr(literal, '_LONG_ASCII', len(raw) + 1)
        expected, starts, ends = literal._mapped(raw)
        monkeypatch.setattr(literal, '_LONG_ASCII', 1)
        actual, compact_starts, compact_ends = literal._mapped(raw)
        assert actual == expected
        assert dict(compact_starts.items()) == starts
        assert dict(compact_ends.items()) == ends
        for boundary in range(len(actual) + 1):
            assert (boundary in compact_starts) == (boundary in starts)
            assert (boundary in compact_ends) == (boundary in ends)


@pytest.mark.parametrize('query', ['s', 'i', '\u0308'])
def test_compact_mapping_does_not_accept_partial_casefold_or_combining_cluster(query):
    raw = 'Українська. ' * 6000 + 'ß İ ї.'
    folded, starts, ends = literal._mapped(raw)
    assert not list(literal._spans(folded, starts, ends, literal._fold(query)))


def test_long_unicode_mapping_cancels_during_cluster_scan():
    count = 0

    def check():
        nonlocal count
        count += 1
        if count == 8:
            raise TimeoutError('cancelled')

    with pytest.raises(TimeoutError, match='cancelled'):
        literal._mapped('Українська історія. ' * 10000, check)
    assert count == 8


def test_excerpt_reverse_map_keeps_compact_boundaries_and_cancellation():
    raw = 'Українська e\u0301 Straße. ' * 4000
    _, starts, ends = literal._mapped(raw)
    for offsets in (starts, ends):
        reverse = _reverse_offsets(offsets, len(raw), lambda: None)
        assert isinstance(reverse, literal._CompactOffsets)
        assert dict(reverse.items()) == {value: key for key, value in offsets.items()}
    with pytest.raises(TimeoutError, match='cancelled'):
        _reverse_offsets(starts, len(raw), lambda: (_ for _ in ()).throw(TimeoutError('cancelled')))


@pytest.mark.parametrize('result_mode', ['short', 'thematic'])
def test_real_app_long_unicode_copy_context_and_scope(tmp_path, result_mode):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Українські джерела')
    other = app.create_workspace('Окрема бібліотека')
    raw = 'Давній архів містив важливі документи.\n' * 2000 + 'Унікальний Київський запис.'
    imported = app.add_text(workspace, 'Український архів', raw, authors=('Тестовий автор',))
    assert imported['state'] == 'complete'
    hits = app.search(workspace, 'Унікальний Київський запис', result_mode=result_mode)
    assert len(hits) == 1
    hit = hits[0]
    assert 'Унікальний Київський запис' in hit.quote
    assert hit.quote in raw
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    context = app.context(workspace, hit.ref, hit.revision)
    assert context.page(0).quote == hit.quote
    assert context.page(0).raw_text == raw
    assert not app.search(other, 'Унікальний Київський запис')
    with pytest.raises(ValueError):
        app.copy(other, hit.ref, hit.revision, 'quote')
    # Searching and presenting a derivative must not rewrite retained originals.
    source = next((app.root / 'originals').iterdir())
    assert sha256(source.read_bytes()).digest() == sha256(raw.encode('utf-8')).digest()
