"""Actual source/database paging; no corpus-throughput or native qualification."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch
from carr_library.search.paging import MAX_SOURCE_PAGES, MAX_TIMEOUT_SECONDS, SearchCursor, SearchResources


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
    cursor = first.next_source_cursor
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


def test_forged_cursor_rejects_resume_before_publishing_new_handles(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Changed cursor source')
    app.add_text(workspace, 'Long source', '\f'.join('Needle.' for _ in range(102)), edition_id='long')
    first = app.search_page(workspace, 'Needle', mode='terms')
    # Copying the visible token does not create an issued continuation.
    cursor = SearchCursor(first.next_source_cursor.token)
    before = dict(app._selections)
    with pytest.raises(ValueError, match='owner|scope'):
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

def test_cursor_rejects_another_app_on_the_same_database_before_new_handles(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Opaque owner')
    app.add_text(workspace, 'Long', '\f'.join('Needle.' for _ in range(101)))
    page = app.search_page(workspace, 'Needle', mode='terms')
    other = LibraryApp(app.root)
    before = dict(other._selections)
    with pytest.raises(ValueError, match='owner|scope|cursor'):
        other.search_page(workspace, 'Needle', mode='terms', cursor=page.next_source_cursor)
    assert other._selections == before
    assert app.copy(workspace, page.results[0].ref, page.results[0].revision, 'quote') == 'Needle'
    assert app.close_search() and other.close_search()


@pytest.mark.parametrize('change', ['metadata', 'membership', 'new_source', 'another_library'])
def test_real_database_commit_invalidates_continuation_and_preserves_current_handles(tmp_path, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Version observer')
    app.add_text(workspace, 'Long', '\f'.join('Needle.' for _ in range(101)), edition_id='long')
    first = app.search_page(workspace, 'Needle', mode='terms')
    if change == 'metadata':
        with app.write(workspace) as db:
            db.execute("UPDATE catalog_work SET title='Changed title' WHERE workspace=?", (workspace,))
    elif change == 'membership':
        app.create_collection(workspace, 'New collection')
    elif change == 'new_source':
        app.add_text(workspace, 'Additional', 'Needle after the first query.')
    else:
        app.create_workspace('Independent commit still changes this database')
    before = dict(app._selections)
    with pytest.raises(ValueError, match='changed|cursor'):
        app.search_page(workspace, 'Needle', mode='terms', cursor=first.next_source_cursor)
    assert app._selections == before
    restarted = app.search_page(workspace, 'Needle', mode='terms')
    assert restarted.results
    assert app.close_search()


def test_cursor_owner_close_withdraws_navigation_but_not_exact_source_actions(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Closed navigation')
    app.add_text(workspace, 'Long', '\f'.join('Needle.' for _ in range(101)))
    first = app.search_page(workspace, 'Needle', mode='terms')
    assert app.close_search()
    before = dict(app._selections)
    with pytest.raises(ValueError, match='owner|cursor'):
        app.search_page(workspace, 'Needle', mode='terms', cursor=first.next_source_cursor)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Needle'


def test_result_page_is_bound_to_its_current_source_window(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Stable result continuation')
    app.add_text(workspace, 'Repeated', ' '.join('Needle.' for _ in range(205)))
    first = app.search_page(workspace, 'Needle', mode='word', limit=100)
    second = app.search_page(workspace, 'Needle', mode='word', cursor=first.cursor,
                             limit=100, offset=100)
    final = app.search_page(workspace, 'Needle', mode='word', cursor=second.cursor,
                            limit=100, offset=200)
    assert len(first.results) == len(second.results) == 100 and len(final.results) == 5
    assert first.window_identity == second.window_identity == final.window_identity
    assert first.has_more_results and second.has_more_results and not final.has_more_results
    spans = [app._selections[row.ref].start for page in (first, second, final) for row in page.results]
    assert spans == sorted(set(spans)) and len(spans) == 205
    assert app.close_search()


@pytest.mark.parametrize('values', [
    {'memory_bytes': True}, {'memory_bytes': 0}, {'memory_bytes': 16 * 1024**3 + 1},
    {'disk_bytes': False}, {'disk_bytes': 0}, {'disk_bytes': 1024**4 + 1},
    {'timeout': True}, {'timeout': float('inf')}, {'timeout': float('nan')},
])
def test_explicit_resource_budget_refuses_invalid_inputs(values):
    with pytest.raises(ValueError, match='budget'):
        SearchResources(**values)


def test_window_memory_allowance_changes_capacity_without_a_total_source_ceiling(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Configured memory window')
    original = '\f'.join('Needle ' + 'x' * 30_000 for _ in range(100))
    saved = app.add_text(workspace, 'Many bounded pages', original)
    small = SearchResources(memory_bytes=16 * 1024**2)
    large = SearchResources(memory_bytes=128 * 1024**2)
    first = app.search_page(workspace, 'Needle', mode='terms', resources=small)
    assert 0 < first.source_pages < 100 and first.next_source_cursor is not None
    assert first.source_characters <= small.source_characters
    with pytest.raises(ValueError, match='scope|cursor'):
        app.search_page(workspace, 'Needle', mode='terms',
                        cursor=first.next_source_cursor, resources=large)
    expanded = app.search_page(workspace, 'Needle', mode='terms', resources=large)
    assert expanded.source_pages == 100 and expanded.next_source_cursor is None
    assert len(expanded.results) == 100
    assert app.assets.resolve(saved['asset']).read_bytes() == original.encode('utf-8')
    assert app.close_search()


def test_close_during_actual_page_work_cancels_and_closes_only_after_worker_return(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from carr_library.search import literal
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Held source window')
    app.add_text(workspace, 'Source', 'Needle remains retained.')
    entered, release = Event(), Event()
    actual = literal.search
    def hold(*args, **kwargs):
        value = actual(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(literal, 'search', hold)
    before = dict(app._selections)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(app.search_page, workspace, 'Needle')
        try:
            assert entered.wait(5)
            owner = app._search_cursor_owner
            assert app.close_search() is False
            assert owner.closed and owner._db is not None
        finally:
            release.set()
        with pytest.raises((TimeoutError, ValueError), match='cancel|timed|closed|owner'):
            future.result(timeout=5)
    assert app._selections == before
    assert owner._db is None and app.close_search()


def test_exact_database_replacement_is_rejected_or_prevented_by_native_reader(tmp_path):
    import os
    import shutil
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Database replacement')
    app.add_text(workspace, 'Long', '\f'.join('Needle.' for _ in range(101)))
    first = app.search_page(workspace, 'Needle', mode='terms')
    identity = app.database.stat().st_ino
    replacement = tmp_path / 'same-bytes.sqlite'
    shutil.copyfile(app.database, replacement)
    before = dict(app._selections)
    try:
        os.replace(replacement, app.database)
    except PermissionError:
        # Native SQLite sharing can itself prevent replacing an open Windows DB.
        assert os.name == 'nt' and app.database.stat().st_ino == identity
        current = app.search_page(workspace, 'Needle', mode='terms', cursor=first.next_source_cursor)
        assert len(current.results) == 1
    else:
        assert app.database.stat().st_ino != identity
        with pytest.raises(ValueError, match='replaced'):
            app.search_page(workspace, 'Needle', mode='terms', cursor=first.next_source_cursor)
        assert app._selections == before
    assert app.close_search()


@pytest.mark.parametrize('callback_kind', ['cancelled', 'status'])
def test_real_callback_cannot_reenter_or_replace_an_active_cursor_owner(tmp_path, callback_kind):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reentrant callback')
    app.add_text(workspace, 'Source', 'Needle remains an exact original.')
    attempts, entered = [], False
    def callback(*args):
        nonlocal entered
        owner = getattr(app, '_search_cursor_owner', None)
        # The cancellation callback also runs before operation() takes the lock.
        if entered or owner is None or owner._db is None:
            return False
        entered = True
        with pytest.raises(RuntimeError, match='already being read'):
            app.search_page(workspace, 'Needle')
        attempts.append('reentry refused')
        assert app.close_search() is False
        assert owner._db is not None
        with pytest.raises(RuntimeError, match='still releasing'):
            app.search_page(workspace, 'Needle')
        assert app._search_cursor_owner is owner
        attempts.append('same observer retained')
        return False
    before = dict(app._selections)
    options = {'mode': 'semantic', 'on_status': callback} if callback_kind == 'status' else {'cancelled': callback}
    with pytest.raises((TimeoutError, ValueError), match='cancel|timed|owner|closed'):
        app.search_page(workspace, 'Needle', **options)
    assert attempts == ['reentry refused', 'same observer retained']
    assert app._selections == before and app._search_cursor_owner._db is None
    restarted = app.search_page(workspace, 'Needle')
    assert len(restarted.results) == 1
    assert app.close_search()


def test_full_scope_ranked_cursor_visits_all_501_editions(catalog):
    app, workspace, _ = catalog
    seen, cursor, offset = [], None, 0
    while True:
        page = app.search_page(workspace, 'Needle', mode='thematic', ranking_scope='all',
                               cursor=cursor, offset=offset, limit=100, timeout=300)
        assert page.ranking_scope == 'all' and page.next_source_cursor is None
        assert page.source_editions == page.source_pages == page.total_results == 501
        seen.extend(row.edition_id for row in page.results)
        if not page.has_more_results:
            break
        cursor, offset = page.cursor, offset + 100
    assert seen == [f'edition-{number:04d}' for number in range(501)]
    assert app.close_search()
