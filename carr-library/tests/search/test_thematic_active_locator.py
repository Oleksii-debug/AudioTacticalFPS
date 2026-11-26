"""Candidate locator handoff tests; active_page is the PR21 read interface."""
from dataclasses import replace
from hashlib import sha256
import sqlite3

import pytest
from carr_library.search.thematic import Passage, search, search_active


def digest(text):
    return sha256(text.encode()).hexdigest()


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.execute('''CREATE TABLE active_page(id, workspace, edition, revision,
        asset, ordinal, physical, printed, verified, raw, digest)''')
    try:
        yield connection
    finally:
        connection.close()


def add(db, page_id=1, workspace='A', edition='e', revision='r', ordinal=1,
        physical=None, printed=None, verified=0, raw='Railway bridge.', asset='a'*64,
        raw_digest=None):
    db.execute('INSERT INTO active_page VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (page_id, workspace, edition, revision, asset, ordinal, physical,
                printed, verified, raw, raw_digest if raw_digest is not None else digest(raw)))


def test_logical_and_exact_source(db):
    raw = 'Преамбула. Railway bridge. Cafe\u0301.'
    add(db, ordinal=2, raw=raw)
    hit, = search_active(db, 'A', {'e': 'work'}, 'railway')
    assert (hit['locator_kind'], hit['physical_index'], hit['segment_index']) == ('logical_segment', None, 2)
    assert hit['printed_label'] is None and not hit['printed_label_verified']
    assert (hit['workspace_id'], hit['edition_id'], hit['revision_id'], hit['asset_sha256']) == ('A', 'e', 'r', 'a'*64)
    assert hit['raw_quote'] == raw[hit['start']:hit['end']] == raw
    assert hit['raw_text_sha256'] == digest(raw)


@pytest.mark.parametrize('verified,label', [(0, None), (1, 'iv')])
def test_physical_printed_distinction(db, verified, label):
    add(db, physical=7, printed='iv', verified=verified)
    hit, = search_active(db, 'A', {'e': 'work'}, 'railway')
    assert (hit['locator_kind'], hit['physical_index'], hit['segment_index']) == ('physical_page', 7, None)
    assert hit['printed_label'] == label and hit['printed_label_verified'] is bool(verified)


def test_scope_scores_and_explicit_empty(db):
    add(db, physical=1)
    expected = search_active(db, 'A', {'e': 'work'}, 'railway')
    add(db, page_id=2, workspace='B', physical=1, raw='Railway '*100)
    add(db, page_id=3, edition='other', physical=1, raw='Railway '*100)
    assert search_active(db, 'A', {'e': 'work'}, 'railway') == expected
    assert search_active(db, 'A', {}, 'railway') == []
    with pytest.raises(ValueError):
        search_active(db, 'A', None, 'railway')


@pytest.mark.parametrize('bad', [dict(physical=0), dict(physical=1.5),
    dict(ordinal=0), dict(printed='1'), dict(physical=1, verified=1, printed=' '),
    dict(asset='bad'), dict(raw_digest='0'*64), dict(raw='Railway\x00bridge')])
def test_bad_active_anchor(db, bad):
    add(db, **bad)
    with pytest.raises(ValueError):
        search_active(db, 'A', {'e': 'work'}, 'railway')


def test_legacy_shape_and_invalid_index():
    raw = 'Railway bridge'
    page = Passage('A', 'work', 'e', 'r', 'p', 1, 0, len(raw), raw, digest(raw))
    hit, = search('A', [page], 'railway')
    assert set(hit) == {'passage_id', 'work_id', 'edition_id', 'revision_id',
        'physical_index', 'start', 'end', 'page_sha256', 'raw_quote', 'score', 'matched_terms', 'reason'}
    for index in [True, 1.5, None]:
        with pytest.raises(ValueError):
            search('A', [replace(page, physical_index=index)], 'railway')


def test_no_stopped_success(db):
    add(db, physical=1)
    with pytest.raises(TimeoutError):
        search_active(db, 'A', {'e': 'work'}, 'railway', cancelled=lambda: True)
