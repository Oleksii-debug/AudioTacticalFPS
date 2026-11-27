"""Cursor-only navigation and real service workflows; not native wx/NVDA proof."""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.search.pagination import MAX_CURSOR_BYTES
from carr_library.ui.federated_search import SearchCenter, SearchOptions
from carr_library.ui.search_paging import PageHistory

from .test_federated_search import Frame, View, Dialogs
from ..search.test_search_pagination import publish_text_corpus


class PagingView(View):
    def __init__(self):
        super().__init__()
        self.navigation = (False, False)
        self.navigation_history = []

    def set_paging(self, previous, following):
        self.navigation = bool(previous), bool(following)
        self.navigation_history.append(self.navigation)


@pytest.fixture
def paged_center(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Paged native workflow source')
    topic = app.create_topic(workspace, 'Unchanged active notebook')
    identities, _ = publish_text_corpus(app, workspace, count=3)
    frame, view = Frame(app, workspace, topic), PagingView()
    controller = SearchCenter(frame, view, dialogs=Dialogs())
    options = SearchOptions((workspace,), 'Needle', limit_per_library=1, paged=True)
    return app, workspace, topic, identities, frame, view, controller, options


def test_next_previous_rerun_source_and_never_reuse_old_result_handles(paged_center):
    app, workspace, topic, identities, frame, view, controller, options = paged_center
    host_note = frame.unsaved_note
    controller.search(options)
    first = controller.rows[0]
    assert first.result.edition_id == identities[0] and view.navigation == (False, True)
    assert 'Page 1:' in view.messages[-1] and 'never a global score' in view.messages[-1]
    controller.next_page()
    second = controller.rows[0]
    assert second.result.edition_id == identities[1] and view.navigation == (True, True)
    with pytest.raises(ValueError, match='latest completed'):
        controller.service.copy(first)
    controller.next_page()
    assert controller.rows[0].result.edition_id == identities[2]
    assert view.navigation == (True, False) and 'All selected source batches examined' in view.messages[-1]
    controller.previous_page()
    current = controller.rows[0]
    assert current.result.edition_id == identities[1] and current is not second
    assert current.result.ref != second.result.ref and view.navigation == (True, True)
    with pytest.raises(ValueError, match='latest completed'):
        controller.service.copy(second)
    controller.copy('quote')
    assert view.copied[-1] == 'Needle source text.'
    assert frame.active == (workspace, topic) and frame.unsaved_note == host_note
    assert app._selections == {}


def test_changed_options_clear_navigation_and_withdraw_previous_actions(paged_center):
    _, _, _, _, _, view, controller, options = paged_center
    controller.search(options)
    previous = controller.rows[0]
    controller.next_page()
    controller.options_changed()
    assert controller.rows == () and view.navigation == (False, False)
    assert not controller.paging.can_previous and not controller.paging.can_next
    with pytest.raises(ValueError, match='latest completed'):
        controller.service.copy(previous)
    controller.next_page()
    assert controller.rows == ()
    controller.search(replace(options, query='absent'))
    assert controller.rows == () and not controller.paging.can_next


def test_stale_page_reports_restart_and_does_not_display_partial_new_results(paged_center):
    app, workspace, _, identities, _, view, controller, options = paged_center
    controller.search(options)
    app.update_metadata(workspace, identities[-1], title='Changed source title')
    controller.next_page()
    assert controller.rows == () and not view.labels and view.navigation == (False, False)
    assert 'changed' in view.messages[-1] and 'Search again' in view.messages[-1]
    assert not controller.running


def test_deferred_navigation_cannot_repopulate_changed_notebook_scope(paged_center):
    _, _, _, _, frame, view, controller, options = paged_center
    controller.search(options)
    frame.deferred = True
    controller.next_page()
    assert controller.running and frame.pending
    frame.active = ('other-library', None)
    frame.finish()
    assert controller.rows == () and not view.labels and not controller.running
    assert 'changed' in view.messages[-1] or 'cancelled' in view.messages[-1].lower()


def test_page_history_bounds_only_cursors_and_retains_forward_continuation():
    history = PageHistory(max_bytes=MAX_CURSOR_BYTES)
    history.accept('first', None, 'a' * 4000, 1)
    for number in range(2, 9):
        requested = history.request('next')
        history.accept('next', requested, str(number) * 4000, number)
        assert history.bytes <= history.max_bytes
        assert all(item is None or type(item) is str for item in history.previous)
    assert history.expired and history.can_next and history.can_previous
    assert 'expired' in history.history_notice
    requested = history.request('previous')
    history.accept('previous', requested, 'following', 7)
    assert history.current_cursor == requested and history.can_next
    history.reset()
    assert history.page_number == 0 and history.bytes == 0 and not history.expired
    assert not history.can_next and not history.can_previous


def test_history_rejects_a_result_for_a_changed_requested_cursor():
    history = PageHistory()
    history.accept('first', None, 'next', 1)
    before = (history.current_cursor, history.next_cursor, tuple(history.previous))
    with pytest.raises(ValueError, match='changed'):
        history.accept('next', 'wrong', None, 2)
    assert (history.current_cursor, history.next_cursor, tuple(history.previous)) == before
    with pytest.raises(ValueError):
        history.accept('next', 'next', 'x' * (MAX_CURSOR_BYTES + 1), 2)
    assert (history.current_cursor, history.next_cursor, tuple(history.previous)) == before


@pytest.mark.parametrize('maximum', (True, 0, MAX_CURSOR_BYTES - 1, 64 * 1024 * 1024 + 1))
def test_history_metadata_budget_is_explicit(maximum):
    with pytest.raises(ValueError):
        PageHistory(max_bytes=maximum)


def test_native_options_allow_explicitly_selected_libraries_beyond_20():
    ids = tuple(f'library-{number}' for number in range(25))
    assert SearchOptions(ids, 'Needle', paged=True).validate().workspaces == ids
    with pytest.raises(ValueError, match='20 libraries'):
        SearchOptions(ids, 'Needle').validate()
