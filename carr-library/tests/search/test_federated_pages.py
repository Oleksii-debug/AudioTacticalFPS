"""Bounded journeys across real catalogs and more than20 selected libraries."""
from dataclasses import replace
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch


@pytest.fixture
def libraries(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = []
    for library_number in range(3):
        workspace = app.create_workspace(f'Library {library_number}')
        selected.append(workspace)
        for edition_number in range(5):
            app.add_text(workspace, f'Source {edition_number}',
                f'Railway history source {edition_number}.', authors=('Historian',),
                edition_id=f'edition-{edition_number}', work_id=f'work-{edition_number}')
    return app, tuple(selected)


@pytest.mark.parametrize('mode', ('word', 'phrase', 'terms', 'advanced', 'thematic', 'semantic'))
def test_continuation_reaches_all_selected_sources_without_retaining_old_handles(libraries, mode):
    app, selected = libraries
    service = FederatedSearch(app)
    baseline = dict(app._selections)
    cursor, found, previous = None, [], None
    for _ in range(20):
        session = service.search_page(selected, 'Railway', mode=mode, cursor=cursor,
            library_page_size=2, edition_limit=2, limit_per_library=1)
        assert session.selected_workspaces == selected
        assert len(session.workspaces) <= 2 and len(session.hits) <= 2
        if previous is not None:
            with pytest.raises(ValueError, match='latest completed'):
                service.copy(previous, 'quote')
        for hit in session.hits:
            found.append((hit.workspace, hit.result.edition_id))
            assert service.copy(hit, 'quote') == hit.result.quote
        previous = session.hits[0] if session.hits else previous
        cursor = session.next_cursor
        if cursor is None:
            break
    assert len(found) == len(set(found)) == 15
    assert app._selections == baseline


def test_twenty_is_a_page_budget_not_a_limit_on_explicit_library_selection(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = tuple(app.create_workspace(f'Library {number}') for number in range(23))
    for workspace in selected:
        app.add_text(workspace, 'History', 'Railway archives.')
    hidden = app.create_workspace('Unselected private library')
    app.add_text(hidden, 'Unselected', 'Railway archives.')
    service = FederatedSearch(app)
    first = service.search_page(selected, 'Railway')
    assert first.workspaces == selected[:20] and len(first.hits) == 20
    assert (first.library_start, first.library_end) == (1, 20)
    second = service.search_page(selected, 'Railway', cursor=first.next_cursor)
    assert second.workspaces == selected[20:] and len(second.hits) == 3
    assert (second.library_start, second.library_end) == (21, 23)
    assert second.next_cursor is None and not second.has_more
    assert hidden not in {hit.workspace for hit in (*first.hits, *second.hits)}


def test_failed_next_page_preserves_exact_previous_session(libraries):
    app, selected = libraries
    service = FederatedSearch(app)
    first = service.search_page(selected, 'Railway', library_page_size=1, edition_limit=2, limit_per_library=1)
    app.update_metadata(selected[0], 'edition-4', title='Changed', authors=('Historian',))
    with pytest.raises(ValueError, match='catalog changed'):
        service.search_page(selected, 'Railway', library_page_size=1, edition_limit=2,
            limit_per_library=1, cursor=first.next_cursor)
    assert service.current is first
    assert service.copy(first.hits[0], 'quote') == first.hits[0].result.quote


def test_cancelled_and_rebound_continuations_are_not_accepted(libraries):
    app, selected = libraries
    service = FederatedSearch(app)
    first = service.search_page(selected, 'Railway', edition_limit=2, limit_per_library=1)
    with pytest.raises((TimeoutError, InterruptedError)):
        service.search_page(selected, 'Railway', edition_limit=2, limit_per_library=1,
            cursor=first.next_cursor, cancelled=lambda: True)
    assert service.current is first
    with pytest.raises(ValueError, match='different selected libraries'):
        service.search_page(tuple(reversed(selected)), 'Railway', edition_limit=2,
            limit_per_library=1, cursor=first.next_cursor)
    with pytest.raises(ValueError, match='different selected libraries'):
        service.search_page(selected, 'archives', edition_limit=2,
            limit_per_library=1, cursor=first.next_cursor)
    assert service.current is first


@pytest.mark.parametrize('options', ({'limit_per_library': 101}, {'library_page_size': 21},
    {'edition_limit': 501}, {'offset_per_library': 1}, {'unknown': 'option'}))
def test_page_budgets_remain_enforced(libraries, options):
    app, selected = libraries
    with pytest.raises(ValueError):
        FederatedSearch(app).search_page(selected, 'Railway', **options)


def test_empty_source_windows_have_a_continuation(libraries):
    app, selected = libraries
    service = FederatedSearch(app)
    first = service.search_page(selected, 'absenttoken', edition_limit=2, library_page_size=1)
    assert first.hits == () and first.has_more and first.next_cursor is not None
    second = service.search_page(selected, 'absenttoken', edition_limit=2,
        library_page_size=1, cursor=first.next_cursor)
    assert second.source_ranges[0][1:3] == (3, 4)


def test_replaced_data_root_with_identical_bytes_withdraws_old_session(libraries, tmp_path):
    import shutil
    app, selected = libraries
    service = FederatedSearch(app)
    first = service.search_page(selected, 'Railway', edition_limit=2, limit_per_library=1)
    old = tmp_path / 'original-root'
    app.root.rename(old)
    shutil.copytree(old, app.root)
    with pytest.raises(ValueError, match='data directory changed'):
        service.copy(first.hits[0], 'quote')
    with pytest.raises(ValueError, match='data directory changed'):
        service.search_page(selected, 'Railway', edition_limit=2, limit_per_library=1,
            cursor=first.next_cursor)
