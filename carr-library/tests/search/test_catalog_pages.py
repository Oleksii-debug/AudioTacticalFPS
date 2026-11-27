"""Real catalog continuations retain source identity and fresh authorization."""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp


@pytest.fixture
def corpus(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected library')
    collection = app.create_collection(workspace, 'Selected collection')
    for number in range(5):
        app.add_text(workspace, f'Railway source {number}',
            f'Railway history and archives number {number}.',
            authors=('Historian',), language='en',
            edition_id=f'edition-{number}', work_id=f'work-{number}',
            collections=(collection,))
    return app, workspace, collection


@pytest.mark.parametrize('mode', ('word', 'phrase', 'terms', 'advanced', 'thematic', 'semantic'))
def test_every_source_window_and_result_page_uses_verified_originals(corpus, mode):
    app, workspace, collection = corpus
    cursor = None
    found, ranges = [], []
    for _ in range(10):
        page = app.search_page(workspace, 'Railway', mode=mode,
            collection=collection, edition_limit=2, limit=1, cursor=cursor)
        ranges.append((page.edition_start, page.edition_end))
        assert len(page.results) <= 1
        for hit in page.results:
            assert hit.verified and hit.workspace == workspace
            assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
            found.append(hit.edition_id)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(found) == len(set(found)) == 5
    assert set(ranges) == {(1, 2), (3, 4), (5, 5)}


@pytest.mark.parametrize('change', ('query', 'mode', 'collection', 'limit', 'edition_limit'))
def test_cursor_cannot_be_reused_with_other_search_settings(corpus, change):
    app, workspace, collection = corpus
    first = app.search_page(workspace, 'Railway', edition_limit=2, limit=1)
    options = dict(edition_limit=2, limit=1)
    query = 'Railway'
    if change == 'query':
        query = 'archives'
    else:
        options[change] = dict(mode='terms', collection=collection, limit=2, edition_limit=3)[change]
    with pytest.raises(ValueError, match='different options'):
        app.search_page(workspace, query, cursor=first.next_cursor, **options)


@pytest.mark.parametrize('change', ('metadata', 'membership', 'source', 'new_edition'))
def test_catalog_revision_change_invalidates_continuation(corpus, change):
    app, workspace, collection = corpus
    first = app.search_page(workspace, 'Railway', edition_limit=2, limit=1)
    if change == 'metadata':
        app.update_metadata(workspace, 'edition-0', title='Changed title', authors=('Historian',), language='en')
    elif change == 'membership':
        app.create_collection(workspace, 'New catalog grouping')
    elif change == 'source':
        app.add_text(workspace, 'Replacement', 'Railway revised.', edition_id='replacement', work_id='replacement-work')
    else:
        app.add_text(workspace, 'New source', 'Railway new edition.')
    with pytest.raises(ValueError, match='catalog changed'):
        app.search_page(workspace, 'Railway', edition_limit=2, limit=1, cursor=first.next_cursor)


def test_empty_source_window_still_advances(corpus):
    app, workspace, _ = corpus
    cursor = None
    seen = []
    for _ in range(4):
        page = app.search_page(workspace, 'absenttoken', edition_limit=2, cursor=cursor)
        assert page.results == ()
        seen.append((page.edition_start, page.edition_end))
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [(1, 2), (3, 4), (5, 5)]


def test_cancelled_continuation_does_not_publish_new_handles(corpus):
    app, workspace, _ = corpus
    first = app.search_page(workspace, 'Railway', edition_limit=2, limit=1)
    before = dict(app._selections)
    with pytest.raises((TimeoutError, InterruptedError)):
        app.search_page(workspace, 'Railway', edition_limit=2, limit=1,
            cursor=first.next_cursor, cancelled=lambda: True)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Railway'


def test_more_than_five_hundred_editions_remain_reachable(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large catalog')
    for number in range(501):
        app.add_text(workspace, f'Source {number}', 'Railway history.',
            edition_id=f'edition-{number:04d}', work_id=f'work-{number:04d}')
    with pytest.raises(ValueError, match='500 editions'):
        app.search(workspace, 'Railway')
    cursor, found = None, []
    for _ in range(8):
        page = app.search_page(workspace, 'Railway', limit=100, cursor=cursor)
        found.extend(hit.edition_id for hit in page.results)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(found) == len(set(found)) == 501
    assert found[-1] == 'edition-0500'


def test_forged_cursor_is_not_source_authority(corpus):
    app, workspace, _ = corpus
    first = app.search_page(workspace, 'Railway', edition_limit=2, limit=1)
    with pytest.raises(ValueError, match='different options'):
        app.search_page(workspace, 'Railway', edition_limit=2, limit=1,
            cursor=replace(first.next_cursor, criteria='0' * 64))
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        app.search_page(workspace, 'Railway', edition_limit=2, limit=1, cursor=first.next_cursor)
