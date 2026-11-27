"""Real scoped SQLite fixtures for Boolean/proximity source search."""
from hashlib import sha256
from itertools import product
import sqlite3

import pytest

from carr_library.search import advanced, literal


@pytest.fixture
def pages():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE active_page(id INTEGER PRIMARY KEY,workspace,edition,'
               'revision,asset,ordinal,physical,printed,verified,raw,digest)')

    def add(edition, raw, *, workspace='library', ordinal=1, physical=None):
        return db.execute('INSERT INTO active_page VALUES(NULL,?,?,?,?,?,?,?,?,?,?)',
                          (workspace, edition, 'revision', 'a' * 64, ordinal,
                           physical, 'iv' if physical else None, bool(physical),
                           raw, sha256(raw.encode()).hexdigest())).lastrowid

    yield db, add
    db.close()


def run(db, query, **options):
    return literal.search(db, 'library', query, mode='advanced', **options)


@pytest.mark.parametrize('query,expected', [
    ('alpha OR beta AND gamma', ['a', 'b', 'e', 'f']),
    ('(alpha OR beta) AND gamma', ['b', 'e', 'f']),
    ('alpha beta', ['a', 'f']),
    ('alpha NOT gamma', ['a']),
    ('alpha AND NOT (beta OR gamma)', []),
    ('NOT (NOT alpha OR beta)', ['b']),
    ('NOT NOT alpha', ['a', 'b', 'f']),
    ('(NOT alpha OR beta) AND gamma', ['d', 'e', 'f']),
    ('alpha AND NOT NOT beta', ['a', 'f']),
    ('alpha OR NOT NOT beta', ['a', 'b', 'c', 'e', 'f']),
])
def test_boolean_truth_and_precedence(pages, query, expected):
    db, add = pages
    for edition, raw in [('a', 'alpha beta'), ('b', 'alpha gamma'),
                         ('c', 'beta'), ('d', 'gamma delta'),
                         ('e', 'beta gamma'), ('f', 'alpha beta gamma')]:
        add(edition, raw)
    assert [hit['edition_id'] for hit in run(db, query)] == expected


@pytest.mark.parametrize('query', [
    'railway OR ox', 'NOT NOT (railway OR ox)',
    'NOT (NOT railway AND NOT ox)', '(railway OR ox) AND NOT closed',
])
def test_short_or_branch_is_not_lost_to_trigram_filter(pages, query):
    db, add = pages
    for edition, raw in [('a', 'ox'), ('b', 'railway'), ('c', 'railway ox'),
                         ('d', 'unrelated')]:
        add(edition, raw)
    assert advanced.compile_query(query).candidate is None
    assert [hit['edition_id'] for hit in run(db, query)] == ['a', 'b', 'c']


def test_nested_or_partial_positive_filters_are_safe(pages):
    db, add = pages
    for edition, raw in [('a', 'railway'), ('b', 'ox history'),
                         ('c', 'ox'), ('d', 'history'), ('e', 'railway closed')]:
        add(edition, raw)
    hits = run(db, '(railway OR (ox AND history)) AND NOT closed')
    assert [hit['edition_id'] for hit in hits] == ['a', 'b']
    assert all('excluded "closed"' in hit['reason'] for hit in hits)


def test_operator_like_literals_and_double_quote_escape(pages):
    db, add = pages
    raw = 'A "careful" account AND OR NOT NEAR/3 C:\\study *'
    add('a', raw)
    add('b', 'unrelated AND OR text')
    query = ' AND '.join(advanced.quote_atom(value) for value in
                         ('A "careful" account', 'AND', 'OR', 'NOT', 'NEAR/3', 'C:\\study', '*'))
    hit, = run(db, query)
    assert hit['raw_quote'] == 'A "careful" account'
    assert [raw[start:end] for start, end in hit['match_spans']] == [
        'A "careful" account', 'AND', 'OR', 'NOT', 'NEAR/3', 'C:\\study', '*']
    # Entire strings that resemble FTS syntax are still just quoted atoms.
    injected = 'x" OR "unrelated'
    add('c', injected)
    assert [h['edition_id'] for h in run(db, advanced.quote_atom(injected))] == ['c']


def test_lowercase_boolean_words_remain_literal_words(pages):
    db, add = pages
    add('a', 'alpha and beta')
    add('b', 'alpha beta')
    assert [h['edition_id'] for h in run(db, 'alpha and beta')] == ['a']
    assert [h['edition_id'] for h in run(db, 'alpha AND beta')] == ['a', 'b']


def test_unicode_casefold_canonical_clusters_and_raw_offsets(pages):
    db, add = pages
    raw = 'Straße Cafe\u0301; ІСТОРІЯ. A source with CAFÉ and café.'
    add('a', raw, physical=3)
    hit, = run(db, 'STRASSE AND CAFÉ AND історія')
    assert [raw[start:end] for start, end in hit['match_spans']] == ['Straße', 'Cafe\u0301', 'ІСТОРІЯ']
    assert hit['raw_quote'] == raw[hit['start']:hit['end']] == 'Straße'
    assert hit['raw_text_sha256'] == sha256(raw.encode()).hexdigest()
    assert (hit['physical_index'], hit['printed_label'], hit['printed_label_verified']) == (3, 'iv', True)
    assert not run(db, 'STRASSE AND cafe')  # Accent-sensitive, like existing literal modes.
    assert not run(db, 's AND STRASSE')  # Cannot split the full casefold of ß.
    add('b', 'CAFÉ one cafe\u0301')
    near = run(db, 'café NEAR/1 café', edition_ids=['b'])[0]
    assert near['match_spans'] == ((0, 4), (9, 14))


def test_long_multilingual_near_search_preserves_compact_source_boundaries(pages):
    db, add = pages
    prefix = 'Незмінний оригінальний текст. ' * 2500
    left, right = 'Cafe\u0301\t  archive', 'ІСТОРІЯ'
    raw = prefix + left + ' правдива ' + right + '.'
    assert len(raw) > literal._LONG_ASCII and not raw.isascii()
    add('a', raw)
    hit, = run(db, '"café archive" NEAR/1 історія')
    assert hit['match_spans'] == ((len(prefix), len(prefix) + len(left)),
                                  (raw.index(right), raw.index(right) + len(right)))
    assert hit['raw_quote'] == left
    assert '1 intervening words' in hit['reason']


@pytest.mark.parametrize('raw,query,distance', [
    ('red blue', 'red NEAR/0 blue', 0),
    ('blue,\r\nred', 'red NEAR/0 blue', 0),
    ('red one blue', 'red NEAR/0 blue', None),
    ('red one blue', 'red NEAR/1 blue', 1),
    ('red one-two blue', 'red NEAR/1 blue', None),
    ('red one-two blue', 'red NEAR/2 blue', 2),
    ('red один\u0301 中 ३ blue', 'red NEAR/2 blue', None),
    ('red один\u0301 中 ३ blue', 'red NEAR/3 blue', 3),
    ('red a_b blue', 'red NEAR/1 blue', 1),
    ('red', 'red NEAR/100 red', None),
    ('red red', 'red NEAR/0 red', 0),
    ('red red red', '"red red" NEAR/0 red', 0),
    ('red blue', '"red blue" NEAR/0 blue', None),
    ('red blue blue', '"red blue" NEAR/0 blue', 0),
    ('a a a a', '"a a" NEAR/0 "a a"', 0),
    ('red ' + 'x ' * 100 + 'blue', 'red NEAR/100 blue', 100),
    ('red ' + 'x ' * 101 + 'blue', 'red NEAR/100 blue', None),
])
def test_near_counts_intervening_unicode_words_and_distinct_occurrences(pages, raw, query, distance):
    db, add = pages
    add('a', raw)
    hits = run(db, query)
    if distance is None:
        assert hits == []
    else:
        hit, = hits
        first, second = hit['match_spans']
        assert first[0] < first[1] <= second[0] < second[1]
        assert hit['raw_quote'] == raw[first[0]:first[1]]
        assert f'{distance} intervening words' in hit['reason']


@pytest.mark.parametrize('distance', [0, 1, 2])
def test_proximity_against_independent_small_word_sequence_oracle(pages, distance):
    db, add = pages
    expected = []
    for number, words in enumerate(product(('red', 'blue', 'x'), repeat=4)):
        edition = f'{number:03d}'
        add(edition, ', '.join(words))
        if any(abs(i - j) - 1 <= distance for i, left in enumerate(words)
               for j, right in enumerate(words) if left == 'red' and right == 'blue'):
            expected.append(edition)
    assert [h['edition_id'] for h in run(db, f'red NEAR/{distance} blue', limit=100)] == expected


def test_near_not_and_boolean_groups(pages):
    db, add = pages
    add('a', 'archive red blue')
    add('b', 'archive red one blue')
    add('c', 'archive red')
    assert [h['edition_id'] for h in run(db, 'archive AND NOT red NEAR/0 blue')] == ['b', 'c']
    assert [h['edition_id'] for h in run(db, 'NOT NOT (red NEAR/0 blue)')] == ['a']


def test_boolean_and_proximity_never_stitch_pages(pages):
    db, add = pages
    add('a', 'red', ordinal=1, physical=1)
    add('a', 'blue', ordinal=2, physical=2)
    assert run(db, 'red AND blue') == []
    assert run(db, 'red NEAR/100 blue') == []


def test_one_hit_per_page_exact_spans_and_stable_pagination(pages):
    db, add = pages
    raw = 'alpha ' + 'middle text ' * 40 + 'beta alpha beta'
    for edition in ('a', 'b', 'c'):
        add(edition, raw)
    all_hits = run(db, 'beta AND alpha')
    assert [hit['edition_id'] for hit in all_hits] == ['a', 'b', 'c']
    assert run(db, 'beta AND alpha', offset=1, limit=1) == all_hits[1:2]
    assert all(hit['raw_quote'] == 'alpha' for hit in all_hits)
    assert all([raw[start:end] for start, end in hit['match_spans']] == ['alpha', 'beta'] for hit in all_hits)


def test_same_scope_filters_and_all_row_digest_validation(pages):
    db, add = pages
    add('a', 'alpha beta')
    damaged = add('b', 'unrelated')
    foreign = add('c', 'alpha beta', workspace='foreign')
    db.execute("UPDATE active_page SET digest='wrong' WHERE id IN (?,?)", (damaged, foreign))
    with pytest.raises(ValueError, match='digest'):
        run(db, 'alpha AND beta', limit=1)
    hit, = run(db, 'alpha AND beta', edition_ids=['a'])
    assert hit['workspace_id'] == 'library' and hit['edition_id'] == 'a'
    assert run(db, 'alpha AND beta', edition_ids=[]) == []
    assert run(db, 'alpha AND beta', edition_ids=["' OR 1=1 --"]) == []


def test_long_source_uses_streamed_occurrences_and_preserves_crlf_offsets(pages):
    db, add = pages
    raw = 'ordinary\r\n\t ' * 6000 + 'red x ' * 30_000 + 'blue'
    add('a', raw)
    hit, = run(db, 'red NEAR/1 blue', timeout=15)
    assert hit['match_spans'] == ((raw.rfind('red'), raw.rfind('red') + 3),
                                  (len(raw) - 4, len(raw)))
    assert hit['raw_quote'] == 'red'
    assert hit['raw_text_sha256'] == sha256(raw.encode()).hexdigest()


def test_occurrence_budget_fails_explicitly_instead_of_returning_partial_hits(pages, monkeypatch):
    db, add = pages
    add('a', 'red blue')
    add('b', 'red ' * 50 + 'x ' * 101 + 'blue')
    monkeypatch.setattr(advanced, 'MAX_OCCURRENCES', 10)
    with pytest.raises(ValueError, match='occurrence budget'):
        run(db, 'red NEAR/0 blue')


def test_cancel_during_near_word_count_discards_earlier_results(pages, monkeypatch):
    db, add = pages
    add('a', 'red blue')
    add('b', 'red ' + 'ordinary ' * 2000 + 'blue')
    count = 0
    real_word = advanced._word

    def word(char):
        nonlocal count
        count += 1
        return real_word(char)

    monkeypatch.setattr(advanced, '_word', word)
    with pytest.raises(TimeoutError):
        run(db, 'red NEAR/100 blue', cancelled=lambda: count >= 32)
    assert 32 <= count <= 1030


@pytest.mark.parametrize('mode', ['word', 'phrase', 'terms'])
def test_existing_modes_keep_operator_text_literal(pages, mode):
    db, add = pages
    add('a', 'AND OR NOT')
    add('b', 'an unrelated source')
    query = 'AND' if mode == 'word' else 'AND OR NOT'
    hit, = literal.search(db, 'library', query, mode=mode)
    assert 'reason' not in hit
    assert hit['raw_quote'] == ('AND' if mode in ('word', 'terms') else 'AND OR NOT')


@pytest.mark.parametrize('query', [
    '(', ')', '""', '" \t "', 'a OR', 'AND a', 'a AND OR b', 'a NOT',
    'NEAR/2 a', 'a NEAR/-1 b', 'a NEAR/101 b', 'a NEAR/1.0 b',
    'a NEAR/ b', 'a NEAR/2 NOT b', '(a OR b) NEAR/2 c',
    'a NEAR/1 b NEAR/2 c', '"unclosed', 'NOT a', 'a OR NOT b',
    'NOT (a AND b)', 'NOT (a OR b)', '\0a', '\ud800', 'x' * 501,
    ' '.join('a' for _ in range(33)), '(' * 17 + 'a' + ')' * 17,
    'NOT ' * 17 + 'a',
])
def test_invalid_or_unanchored_queries_have_clear_errors(query):
    with pytest.raises(ValueError) as error:
        advanced.compile_query(query)
    assert str(error.value)


@pytest.mark.parametrize('query', [
    'a AND NOT b', 'NOT NOT a', 'NOT (NOT a OR b)',
    '(a OR NOT b) AND c', 'a AND NOT (b AND c)',
    'a NEAR/0 b', '"AND" OR "OR"', '(' * 16 + 'a' + ')' * 16,
])
def test_nested_negative_clauses_are_allowed_when_final_branches_are_anchored(query):
    assert advanced.compile_query(query).query == query


def test_compilation_checks_cancellation():
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise TimeoutError('cancelled')

    with pytest.raises(TimeoutError, match='cancelled'):
        advanced.compile_query('alpha OR beta OR gamma', check=check)
    assert calls == 4


def test_oversized_query_is_rejected_before_character_processing(monkeypatch):
    def unexpected_scan(_text):
        raise AssertionError('oversized input must not reach Unicode scanning')

    monkeypatch.setattr(advanced, '_valid_text', unexpected_scan)
    with pytest.raises(ValueError, match='at most 500'):
        advanced.compile_query('x' * 1_000_000)
