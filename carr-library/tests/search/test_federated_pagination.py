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
def test_portable_continuation_cannot_rebind_selected_scope(app, tmp_path, change):
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
    with pytest.raises(ValueError, match='start a new search'):
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
        service.search_page(selected, 'railway', cursor=page.next_cursor, cancelled=lambda: True)
    assert service.current is before
    assert service.copy(page.session.hits[0], 'quote') == 'railway'
    app.archive_workspace(selected[1])
    with pytest.raises(ValueError, match='archived'):
        service.search_page(selected, 'railway', cursor=page.next_cursor)
    assert service.current is before


def test_whole_query_budget_can_be_explicitly_configured_above_one_minute(app):
    workspace = app.create_workspace('Configurable operation budget')
    app.add_text(workspace, 'Book', 'railway')
    page = FederatedSearch(app).search_page([workspace], 'railway', timeout=120.0)
    assert page.complete and page.session.hits[0].result.verified
    with pytest.raises(ValueError):
        FederatedSearch(app).search_page([workspace], 'railway', timeout=float('inf'))


def test_cancellation_callback_validation_does_not_turn_into_arbitrary_exception(app):
    workspace = app.create_workspace('Validation')
    with pytest.raises(ValueError, match='callback'):
        FederatedSearch(app).search_page([workspace], 'railway', cancelled=None)
