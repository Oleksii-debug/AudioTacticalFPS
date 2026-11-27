"""Real SQLite keyset paging retains complete-scope integrity and raw offsets."""
from hashlib import sha256
import sqlite3

import pytest

from carr_library.search.literal import search, search_page
from carr_library.search.presentation import presentation_span


@pytest.fixture
def db():
    value = sqlite3.connect(':memory:')
    value.execute('CREATE TABLE active_page(id INTEGER PRIMARY KEY, workspace TEXT, '
        'edition TEXT, revision TEXT, asset TEXT, ordinal INTEGER, physical INTEGER, '
        'printed TEXT, verified INTEGER, raw TEXT, digest TEXT)')
    try:
        yield value
    finally:
        value.close()


def add(db, identity, raw, *, workspace='w', edition='a', expected=None):
    digest = sha256((raw if expected is None else expected).encode('utf-8')).hexdigest()
    db.execute('INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (identity, workspace, edition, 'r' + str(identity), 'a' * 64,
         identity, None, None, 0, raw, digest))


def collect(db, query, *, limit=7, **options):
    result, after, snapshot = [], None, None
    for _ in range(200):
        page = search_page(db, 'w', query, limit=limit,
            after=after, expected_snapshot=snapshot, **options)
        assert len(page.hits) <= limit
        result.extend(page.hits)
        if page.after is None:
            return result
        assert page.hits
        after, snapshot = page.after, page.snapshot
    pytest.fail('A finite fixture did not reach its final page.')


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'advanced'])
def test_paging_matches_existing_complete_order_and_exact_unicode(db, mode):
    for index in range(1, 54):
        add(db, index, 'Київ: Cafe\u0301 and Straße. Original source ' + str(index),
            edition='a' if index % 2 else 'b')
    add(db, 90, 'Cafe\u0301 and Straße private text', workspace='foreign')
    query = 'café AND strasse' if mode == 'advanced' else (
        'café strasse' if mode == 'terms' else 'STRASSE')
    expected = search(db, 'w', query, mode=mode, limit=100)
    assert len(expected) == 53
    actual = collect(db, query, mode=mode)
    assert actual == expected
    assert len({(h['page_id'], h['start'], h['end']) for h in actual}) == len(actual)
    assert all(h['workspace_id'] == 'w' and h['raw_quote'] in (
        'Straße', 'Cafe\u0301') for h in actual)


def test_keyset_has_no_legacy_ten_thousand_offset_ceiling(db):
    add(db, 1, 'needle ' * 10030)
    first = search_page(db, 'w', 'needle', limit=3)
    after = ('a', 1, 1, 10001 * 7, 10001 * 7 + 6)
    later = search_page(db, 'w', 'needle', limit=3, after=after,
                        expected_snapshot=first.snapshot)
    assert [hit['start'] for hit in later.hits] == [index * 7 for index in (10002, 10003, 10004)]
    assert later.after == ('a', 1, 1, 10004 * 7, 10004 * 7 + 6)
    with pytest.raises(ValueError, match='budget'):
        search(db, 'w', 'needle', offset=10001)


@pytest.mark.parametrize('result_mode', ['short', 'thematic'])
def test_expanded_excerpts_do_not_repeat_across_page_boundaries(db, result_mode):
    raw = ' '.join(f'Part {i}: needle needle and the original context.' for i in range(35))
    add(db, 1, raw)
    hits = search(db, 'w', 'needle', limit=100)
    expected = list(dict.fromkeys(presentation_span(raw, h, 'needle', result_mode) for h in hits))
    pages = collect(db, 'needle', result_mode=result_mode, limit=3)
    actual = [presentation_span(raw, h, 'needle', result_mode) for h in pages]
    assert len(actual) > 3 and actual == expected
    assert len(actual) == len(set(actual))


@pytest.mark.parametrize('change', ['source', 'printed-label', 'new-page', 'query', 'mode', 'limit', 'scope'])
def test_continuation_is_bound_to_exact_scoped_snapshot_and_query(db, change):
    add(db, 1, 'needle needle needle needle')
    page = search_page(db, 'w', 'needle', limit=1)
    options = dict(limit=1, after=page.after, expected_snapshot=page.snapshot)
    query = 'needle'
    if change == 'source':
        raw = 'needle changed needle needle'
        db.execute('UPDATE active_page SET raw=?,digest=? WHERE id=1',
                   (raw, sha256(raw.encode()).hexdigest()))
    elif change == 'printed-label':
        db.execute("UPDATE active_page SET printed='new label' WHERE id=1")
    elif change == 'new-page':
        add(db, 2, 'No matching token but part of this scope.')
    elif change == 'query':
        query = 'changed'
    elif change == 'mode':
        options['mode'] = 'word'
    elif change == 'limit':
        options['limit'] = 2
    else:
        options['scope_key'] = 'new-catalog-metadata'
    with pytest.raises(ValueError, match='changed'):
        search_page(db, 'w', query, **options)


def test_integrity_before_cursor_and_candidate_pruning_is_not_relaxed(db):
    add(db, 1, 'needle needle')
    add(db, 2, 'ordinary unrelated source')
    page = search_page(db, 'w', 'needle', limit=1)
    db.execute("UPDATE active_page SET raw='tampered unrelated source' WHERE id=2")
    with pytest.raises(ValueError, match='raw page digest mismatch'):
        search_page(db, 'w', 'needle', limit=1, after=page.after,
                    expected_snapshot=page.snapshot)


def test_foreign_workspace_and_filtered_out_edition_do_not_affect_snapshot(db):
    add(db, 1, 'needle needle')
    page = search_page(db, 'w', 'needle', edition_ids=['a'], limit=1)
    add(db, 2, 'corrupt foreign', workspace='other', expected='original')
    add(db, 3, 'corrupt excluded', edition='b', expected='original')
    last = search_page(db, 'w', 'needle', edition_ids=['a'], limit=1,
                       after=page.after, expected_snapshot=page.snapshot)
    assert len(last.hits) == 1 and last.hits[0]['start'] == 7
    assert last.after is None and last.snapshot == page.snapshot


@pytest.mark.parametrize('after', [
    ('a', 1, 1, True, 2), ('a', 1, 1, -1, 2), ('a', 1, 1, 2, 2),
    ('a', 1, 1, 0), ['a', 1, 1, 0, 2], ('', 1, 1, 0, 2),
])
def test_invalid_keyset_is_rejected_before_database_access(after):
    with pytest.raises(ValueError, match='position'):
        search_page(None, 'w', 'needle', after=after, expected_snapshot='a' * 64)


def test_empty_selection_and_exact_last_page_have_no_false_continuation(db):
    add(db, 1, 'needle needle')
    empty = search_page(db, 'w', 'needle', edition_ids=[])
    assert empty.hits == () and empty.after is None
    full = search_page(db, 'w', 'needle', limit=2)
    assert len(full.hits) == 2 and full.after is None
