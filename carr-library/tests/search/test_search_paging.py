"""Actual source/database paging; no corpus-throughput or native qualification."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch
from carr_library.search.paging import MAX_SOURCE_PAGES, MAX_TIMEOUT_SECONDS


@pytest.fixture(scope='module')
def catalog(tmp_path_factory):
    app = LibraryApp(tmp_path_factory.mktemp('search-pages') / 'library')
    workspace = app.create_workspace('More than 500 retained editions')
    for number in range(501):
        result = app.add_text(workspace, f'Source {number:04d}',
                              f'Needle unique source {number:04d}.',
                              edition_id=f'edition-{number:04d}', work_id=f'work-{number:04d}')
        assert result['state'] == 'complete'
    other = app.create_workspace('Excluded library')
    app.add_text(other, 'Excluded', 'Needle outside the selected library.')
    return app, workspace, other


def test_actual_501_editions_are_visited_with_bounded_source_windows(catalog):
    app, workspace, other = catalog
    before = app.database.read_bytes()
    cursor, found, windows = None, [], []
    for _ in range(8):
        page = app.search_page(workspace, 'Needle', mode='terms', cursor=cursor, timeout=300)
        assert page.source_pages <= MAX_SOURCE_PAGES
        assert page.source_characters <= 5_000_000
        assert page.source_editions <= 500 and not page.has_more_results
        assert all(row.workspace == workspace and row.verified for row in page.results)
        assert all(app.copy(workspace, row.ref, row.revision, 'quote') == 'Needle' for row in page.results)
        found.extend(row.edition_id for row in page.results)
        windows.append(page)
        cursor = page.next_source_cursor
        if cursor is None:
            break
    assert len(windows) == 6
    assert found == [f'edition-{number:04d}' for number in range(501)]
    assert app.database.read_bytes() == before
    assert len(app._selections) <= 2000


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'advanced', 'thematic', 'semantic'])
def test_one_long_edition_continues_source_and_result_pages_without_gaps(tmp_path, mode):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('One many-page source')
    original = '\f'.join(f'Needle source page {number:04d}.' for number in range(205))
    result = app.add_text(workspace, 'Long source', original, edition_id='long')
    seen, cursor = [], None
    notices = []
    for _ in range(4):
        offset = 0
        while True:
            page = app.search_page(workspace, 'Needle', mode=mode, cursor=cursor,
                                   limit=37, offset=offset, timeout=300, on_status=notices.append)
            seen.extend(row.page for row in page.results)
            assert all(row.edition_id == 'long' for row in page.results)
            if not page.has_more_results:
                break
            offset += 37
        cursor = page.next_source_cursor
        if cursor is None:
            break
    assert len(seen) == 205 and len(set(seen)) == 205
    assert app.assets.resolve(result['asset']).read_bytes() == original.encode('utf-8')
    if mode == 'semantic':
        assert any('unavailable' in value.lower() for value in notices)


def test_literal_many_matches_on_one_page_keep_result_continuation_after_presentation_dedup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Repeated matching text')
    app.add_text(workspace, 'Repeated', ' '.join('Needle.' for _ in range(205)))
    first = app.search_page(workspace, 'Needle', mode='word', result_mode='thematic', limit=100)
    assert first.results and first.has_more_results
    second = app.search_page(workspace, 'Needle', mode='word', result_mode='thematic', limit=100, offset=100)
    final = app.search_page(workspace, 'Needle', mode='word', result_mode='thematic', limit=100, offset=200)
    assert second.has_more_results and final.results and not final.has_more_results
    assert final.next_source_cursor is None


@pytest.mark.parametrize('change', ['workspace', 'query', 'mode', 'filter', 'root'])
def test_source_cursor_is_bound_to_exact_requested_scope(catalog, tmp_path, change):
    app, workspace, other = catalog
    first = app.search_page(workspace, 'Needle', mode='terms')
    cursor = json.loads(json.dumps(first.next_source_cursor))
    host, scope, query, options = app, workspace, 'Needle', {'mode': 'terms'}
    if change == 'workspace':
        scope = other
    elif change == 'query':
        query = 'unique'
    elif change == 'mode':
        options['mode'] = 'word'
    elif change == 'filter':
        options['title'] = 'Source'
    else:
        host = LibraryApp(tmp_path / 'different-root')
        scope = host.create_workspace('Different root')
    before = dict(host._selections)
    with pytest.raises(ValueError, match='cursor|scope'):
        host.search_page(scope, query, cursor=cursor, **options)
    assert host._selections == before


def test_cursor_with_unmatched_active_revision_rejects_resume_before_publishing_new_handles(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Changed cursor source')
    app.add_text(workspace, 'Long source', '\f'.join('Needle.' for _ in range(102)), edition_id='long')
    first = app.search_page(workspace, 'Needle', mode='terms')
    cursor = json.loads(json.dumps(first.next_source_cursor))
    # An inactive/forged revision cannot describe the current continuation.
    cursor['after_page'][1] = 'different-active-revision'
    before = dict(app._selections)
    with pytest.raises(ValueError, match='source changed'):
        app.search_page(workspace, 'Needle', mode='terms', cursor=cursor)
    assert app._selections == before


def test_more_than_twenty_selected_libraries_uses_one_bounded_owned_session(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = [app.create_workspace(f'Library {number:02d}') for number in range(23)]
    for number, workspace in enumerate(selected):
        app.add_text(workspace, f'Source {number:02d}', f'Needle in library {number:02d}.')
    service = FederatedSearch(app)
    first = service.search(selected, 'Needle', paged=True, timeout=300)
    old = first.hits[0]
    assert first.workspaces == tuple(selected[:20]) and first.has_more_libraries
    assert first.selected_workspaces == tuple(selected) and len(first.hits) == 20
    last = service.search(selected, 'Needle', paged=True, library_offset=20, timeout=300)
    assert last.workspaces == tuple(selected[20:]) and not last.has_more_libraries
    assert len(last.hits) == 3 and len(service._completed.app._selections) <= 2000
    with pytest.raises(ValueError, match='latest completed'):
        service.copy(old)
    assert service.copy(last.hits[0], 'quote') == 'Needle'
    assert app._selections == {}


@pytest.mark.parametrize('allowance', [61, 300, 3600, MAX_TIMEOUT_SECONDS])
def test_explicit_larger_time_allowances_reach_all_nested_source_engines(tmp_path, allowance):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit operation budget')
    app.add_text(workspace, 'Source', 'Needle remains an exact source.')
    for mode in ('phrase', 'thematic', 'semantic'):
        result = app.search_page(workspace, 'Needle', mode=mode, timeout=allowance)
        assert len(result.results) == 1 and result.results[0].verified


@pytest.mark.parametrize('allowance', [0, -1, True, float('nan'), float('inf'), MAX_TIMEOUT_SECONDS + 1])
def test_invalid_time_allowances_never_replace_previous_handles(tmp_path, allowance):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Invalid budget')
    app.add_text(workspace, 'Source', 'Needle stays unchanged.')
    first = app.search_page(workspace, 'Needle')
    before = dict(app._selections)
    with pytest.raises(ValueError, match='budget'):
        app.search_page(workspace, 'Needle', timeout=allowance)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Needle'


def test_cancellation_during_window_selection_keeps_previous_completed_source_handles(catalog, monkeypatch):
    app, workspace, _ = catalog
    first = app.search_page(workspace, 'Needle', mode='terms')
    before = dict(app._selections)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 12
    with pytest.raises((InterruptedError, TimeoutError)):
        app.search_page(workspace, 'Needle', mode='terms',
                        cursor=first.next_source_cursor, cancelled=cancelled)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Needle'


def test_source_cursor_cannot_cross_an_authorized_topic(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Topic cursor scope')
    first_topic = app.create_topic(workspace, 'First topic')
    other_topic = app.create_topic(workspace, 'Other topic')
    app.add_text(workspace, 'Long source', '\f'.join('Needle.' for _ in range(101)))
    first = app.search_page(workspace, 'Needle', mode='terms', authorized_topic=first_topic)
    before = dict(app._selections)
    with pytest.raises(ValueError, match='cursor|scope'):
        app.search_page(workspace, 'Needle', mode='terms', authorized_topic=other_topic,
                        cursor=first.next_source_cursor)
    assert app._selections == before
