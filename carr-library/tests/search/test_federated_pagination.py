"""Actual selected-library continuation, authority expiry and atomic pages."""
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'library')


def test_more_than_twenty_libraries_can_be_visited_without_unbounded_handles(app):
    selected = []
    for number in range(25):
        workspace = app.create_workspace(f'Library {number}')
        app.add_text(workspace, f'Book {number}', f'railway source {number}')
        selected.append(workspace)
    service = FederatedSearch(app)
    first = service.search_page(selected, 'railway')
    assert len(first.session.groups) == 20 and first.next_cursor
    assert first.selected_libraries == 25
    assert len(service._completed.app._selections) == 20
    old = first.session.hits[0]
    assert service.copy(old, 'quote') == 'railway'
    second = service.search_page(selected, 'railway', cursor=first.next_cursor)
    assert second.complete and len(second.session.groups) == 5
    assert tuple(group.workspace for group in (*first.session.groups, *second.session.groups)) == tuple(selected)
    with pytest.raises(ValueError, match='latest'):
        service.copy(old, 'quote')
    assert all(service.copy(hit, 'quote') == 'railway' for hit in second.session.hits)


def test_continuation_finishes_current_library_before_advancing(app):
    first, second = [app.create_workspace(label) for label in ('Many matches', 'Next')]
    app.add_text(first, 'Repeated source', 'railway ' * 205)
    app.add_text(second, 'Other source', 'railway')
    service = FederatedSearch(app)
    cursor = None
    counts, order = {first: 0, second: 0}, []
    while True:
        page = service.search_page([first, second], 'railway', cursor=cursor, limit_per_library=37)
        for group in page.session.groups:
            order.append(group.workspace)
            counts[group.workspace] += len(group.hits)
        assert len(service._completed.app._selections) <= 20 * 100
        cursor = page.next_cursor
        if cursor is None:
            break
    assert counts == {first: 205, second: 1}
    assert all(value == first for value in order[:order.index(second)])


@pytest.mark.parametrize('change', ['query', 'order', 'filter', 'root'])
def test_opaque_continuation_cannot_rebind_selected_scope(app, tmp_path, change):
    selected = [app.create_workspace(str(n)) for n in range(2)]
    for workspace in selected:
        app.add_text(workspace, 'Book', 'railway source')
    service = FederatedSearch(app)
    page = service.search_page(selected, 'railway', library_budget=1)
    before = service.current
    options = dict(cursor=page.next_cursor, library_budget=1)
    query = 'railway'
    if change == 'query':
        query = 'source'
    elif change == 'order':
        selected.reverse()
    elif change == 'filter':
        options['title'] = 'Another'
    else:
        service = FederatedSearch(LibraryApp(tmp_path / 'other'))
    with pytest.raises(ValueError, match='continuation'):
        service.search_page(selected, query, **options)
    if change != 'root':
        assert service.current is before


def test_failed_or_cancelled_next_page_preserves_current_authority(app):
    selected = [app.create_workspace(str(n)) for n in range(2)]
    for workspace in selected:
        app.add_text(workspace, 'Book', 'railway source')
    service = FederatedSearch(app)
    page = service.search_page(selected, 'railway', library_budget=1)
    before = service.current
    with pytest.raises(TimeoutError):
        service.search_page(selected, 'railway', cursor=page.next_cursor, library_budget=1, cancelled=lambda: True)
    assert service.current is before
    assert service.copy(page.session.hits[0], 'quote') == 'railway'
    app.archive_workspace(selected[1])
    with pytest.raises(ValueError, match='archived'):
        service.search_page(selected, 'railway', cursor=page.next_cursor, library_budget=1)
    assert service.current is before


def test_whole_page_retains_the_existing_one_minute_operation_ceiling(app):
    workspace = app.create_workspace('Bounded operation budget')
    app.add_text(workspace, 'Book', 'railway')
    page = FederatedSearch(app).search_page([workspace], 'railway', timeout=60.0)
    assert page.complete and page.session.hits[0].result.verified
    for invalid in (120.0, float('inf'), 0, True):
        with pytest.raises(ValueError, match='budget'):
            FederatedSearch(app).search_page([workspace], 'railway', timeout=invalid)


def test_cancellation_callback_validation_does_not_turn_into_arbitrary_exception(app):
    workspace = app.create_workspace('Validation')
    with pytest.raises(ValueError, match='callback'):
        FederatedSearch(app).search_page([workspace], 'railway', cancelled=None)


def test_new_controller_and_serialized_values_never_adopt_existing_tokens(app):
    workspace = app.create_workspace('Opaque owner')
    app.add_text(workspace, 'Book', 'railway railway')
    service = FederatedSearch(app)
    page = service.search_page([workspace], 'railway', limit_per_library=1)
    assert page.next_cursor and len(page.next_cursor) <= 128
    previous = service.current
    tokens = tuple(service._completed.app._search_cursors._values.items())
    for cursor in ('{"index":0,"source":null}', page.next_cursor + 'x', True, {'index': 0}):
        with pytest.raises(ValueError, match='continuation'):
            service.search_page([workspace], 'railway', limit_per_library=1, cursor=cursor)
        assert service.current is previous
        assert tuple(service._completed.app._search_cursors._values.items()) == tokens
    with pytest.raises(ValueError, match='continuation'):
        FederatedSearch(app).search_page([workspace], 'railway', limit_per_library=1,
                                         cursor=page.next_cursor)
    assert service.copy(page.hits[0], 'quote') == 'railway'


def test_cancel_after_actual_candidate_page_keeps_previous_refs_and_cursor_table(app, monkeypatch):
    workspace = app.create_workspace('Atomic next page')
    app.add_text(workspace, 'Book', 'railway ' * 4)
    service = FederatedSearch(app)
    first = service.search_page([workspace], 'railway', limit_per_library=1)
    completed = service._completed
    tokens = tuple(completed.app._search_cursors._values.items())
    selections, host_selections = dict(completed.app._selections), dict(app._selections)
    original, cancelled, seen = LibraryApp.search_page, [False], []
    def real_page(candidate, *args, **kwargs):
        page = original(candidate, *args, **kwargs)
        seen.append(page)
        cancelled[0] = True  # Actual source verification and candidate tokens have finished.
        return page
    with monkeypatch.context() as patch:
        patch.setattr(LibraryApp, 'search_page', real_page)
        with pytest.raises(TimeoutError, match='cancelled'):
            service.search_page([workspace], 'railway', cursor=first.next_cursor,
                                limit_per_library=1, cancelled=lambda: cancelled[0])
    assert seen and seen[0].results
    assert service._completed is completed
    assert tuple(completed.app._search_cursors._values.items()) == tokens
    assert completed.app._selections == selections and app._selections == host_selections
    assert service.copy(first.hits[0], 'quote') == 'railway'
    resumed = service.search_page([workspace], 'railway', cursor=first.next_cursor, limit_per_library=1)
    assert len(resumed.hits) == 1 and resumed.hits[0].result.ref != first.hits[0].result.ref


def test_ranked_empty_source_window_remains_reachable_through_federated_cursor(app):
    workspace = app.create_workspace('Empty ranked window')
    app.add_text(workspace, 'Book', '\f'.join(['unrelated'] * 4 + ['railway history']))
    service = FederatedSearch(app)
    page = service.search_page([workspace], 'railway', mode='thematic',
                               limit_per_library=2, page_budget=2)
    assert page.hits == () and page.next_cursor and page.scanned_segments == 2
    seen = []
    for _ in range(4):
        page = service.search_page([workspace], 'railway', mode='thematic',
            limit_per_library=2, page_budget=2, cursor=page.next_cursor)
        seen.extend(page.hits)
        if page.complete:
            break
    assert page.complete and len(seen) == 1 and seen[0].result.verified
    assert 'local' in page.ordering and 'never across libraries' in page.ordering
