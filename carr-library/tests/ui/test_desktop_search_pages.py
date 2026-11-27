"""Actual host paging through desktop handlers; control doubles are not native wx."""
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import shutil
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.search_model import Hit, SearchModel


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection = value, selection
        self.enabled = True
        self.owner = get_ident()
    def _ui(self):
        assert get_ident() == self.owner, 'A worker accessed a native-control boundary.'
    def GetValue(self): self._ui(); return self.value
    def GetSelection(self): self._ui(); return self.selection
    def ChangeValue(self, value): self._ui(); self.value = value
    def SetSelection(self, value): self._ui(); self.selection = value
    def SetInsertionPoint(self, value): self._ui()
    def SetFocus(self): self._ui()
    def Enable(self, value=True): self._ui(); self.enabled = bool(value)


@pytest.fixture
def view(tmp_path):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    cls = next(node for node in ast.parse(path.read_text('utf-8')).body
               if isinstance(node, ast.ClassDef) and node.name == 'DesktopSearchPanel')
    namespace = {'PagedSearchPanel': object, 'Hit': Hit}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), namespace)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Desktop result pages')
    topic = app.create_topic(workspace, 'Bound research topic')
    imported = app.add_text(workspace, 'Original paged source',
        '\f'.join(f'Original needle passage number {index:03d}.' for index in range(101)))
    panel = object.__new__(namespace['DesktopSearchPanel'])
    panel.frame = SimpleNamespace(library=app, active=(workspace, topic), _closing=False,
        working=False, cancel_event=Event(), report=Mock(), run_job=Mock())
    panel.bound_library, panel.bound_workspace, panel.bound_topic = app, workspace, topic
    panel.model, panel.results = SearchModel(), {}
    panel.model.set_workspace(workspace)
    panel._context_epoch, panel._search_alive = 0, True
    panel._init_paging()
    panel.query = Control('needle')
    panel.retrieval, panel.result_mode, panel.collection = Control(), Control(), Control()
    panel.collection_ids, panel.filters = [None], {}
    panel.grid, panel._refresh_details = Mock(), Mock()
    panel.details, panel.status = Control(), Control()
    panel.previous_result_page, panel.next_result_page = Control(), Control()
    panel._set_page_navigation()
    panel.source_path = app.assets.resolve(imported['asset'])
    panel.initial_edition = imported['edition']
    return panel


def submitted(panel):
    _, operation, done = panel.frame.run_job.call_args.args
    failed = panel.frame.run_job.call_args.kwargs['failed']
    return operation, done, failed


def finish(panel):
    operation, done, _ = submitted(panel)
    with ThreadPoolExecutor(max_workers=1) as executor:
        page = executor.submit(operation).result(timeout=20)
    done(page)
    return page


def first(panel):
    panel.on_search()
    return finish(panel)


def snapshot(panel):
    return (panel.model.hits, dict(panel.results), list(panel._page_history),
            panel._page_index, panel._page_origin, panel._next_cursor,
            dict(panel.bound_library._selections))


def copy_row(panel, row):
    return panel.bound_library.copy(row.workspace, row.ref, row.revision, 'quote')


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced'])
def test_all_main_modes_reach_the_101st_result_and_retire_previous_handles(view, mode):
    view.retrieval.selection = view.MODES.index(mode)
    view.query.value = '"needle" AND "passage"' if mode == 'advanced' else 'needle'
    original = view.source_path.read_bytes()
    first_page = first(view)
    assert len(first_page.results) == 100 and first_page.next_cursor is not None
    assert view.next_result_page.enabled and not view.previous_result_page.enabled
    old = first_page.results[0]
    assert copy_row(view, old) == old.quote
    view.on_next_result_page()
    last = finish(view)
    assert len(last.results) == 1 and last.next_cursor is None
    assert view.previous_result_page.enabled and not view.next_result_page.enabled
    assert copy_row(view, last.results[0]) == last.results[0].quote
    with pytest.raises(ValueError):
        copy_row(view, old)
    assert 'No further matching result page' in view.status.value
    view.on_previous_result_page()
    previous = finish(view)
    assert {row.quote for row in previous.results} == {row.quote for row in first_page.results}
    assert not ({row.ref for row in previous.results} & {row.ref for row in first_page.results})
    with pytest.raises(ValueError):
        copy_row(view, last.results[0])
    assert view.source_path.read_bytes() == original


@pytest.mark.parametrize('when', ['before-worker', 'after-worker'])
def test_cancelled_navigation_retains_exact_page_and_action_handles(view, when):
    initial = first(view)
    before = snapshot(view)
    view.on_next_result_page()
    operation, done, failed = submitted(view)
    if when == 'after-worker':
        with ThreadPoolExecutor(max_workers=1) as executor:
            page = executor.submit(operation).result(timeout=20)
    view.frame.cancel_event.set()
    with pytest.raises(InterruptedError) as caught:
        operation() if when == 'before-worker' else done(page)
    failed(caught.value)
    assert snapshot(view) == before
    assert view.next_result_page.enabled
    assert 'previous completed result page' in view.status.value
    view.frame.cancel_event.clear()
    assert copy_row(view, initial.results[0]) == initial.results[0].quote


def test_catalog_change_refuses_next_without_overwriting_the_accepted_page(view):
    initial = first(view)
    before = snapshot(view)
    view.bound_library.add_text(view.bound_workspace, 'New catalog entry', 'Unrelated source.')
    view.on_next_result_page()
    operation, _, failed = submitted(view)
    with pytest.raises(ValueError, match='catalog changed') as caught:
        operation()
    failed(caught.value)
    assert snapshot(view) == before
    assert copy_row(view, initial.results[0]) == initial.results[0].quote


def test_previous_to_first_page_remains_bound_to_original_catalog_generation(view):
    first(view)
    view.on_next_result_page()
    finish(view)
    before = snapshot(view)
    view.bound_library.add_text(view.bound_workspace, 'New edition', 'Unrelated content.')
    view.on_previous_result_page()
    operation, _, failed = submitted(view)
    with pytest.raises(ValueError, match='catalog changed') as caught:
        operation()
    failed(caught.value)
    assert snapshot(view) == before


@pytest.mark.parametrize('change', ['query', 'presentation', 'filter', 'collection'])
def test_navigation_cannot_reuse_an_old_request_after_silent_control_change(view, change):
    first(view)
    before_calls = view.frame.run_job.call_count
    if change == 'query':
        view.query.value = 'different'
    elif change == 'presentation':
        view.result_mode.selection = 1
    elif change == 'filter':
        view.filters['author'] = Control('Different author')
    else:
        view.collection_ids.append(view.bound_library.create_collection(view.bound_workspace, 'Different scope'))
        view.collection.selection = 1
    view.on_next_result_page()
    assert view.frame.run_job.call_count == before_calls
    assert not view.model.hits and not view.results
    assert not view.bound_library._selections
    assert not view.previous_result_page.enabled and not view.next_result_page.enabled


@pytest.mark.parametrize('change', ['scope', 'root-object', 'same-path-root', 'destroy'])
def test_late_page_completion_never_populates_withdrawn_window_or_scope(view, tmp_path, change):
    first(view)
    view.on_next_result_page()
    operation, done, failed = submitted(view)
    with ThreadPoolExecutor(max_workers=1) as executor:
        page = executor.submit(operation).result(timeout=20)
    before_status = view.status.value
    prior_root = view.bound_library.root
    retained = tmp_path / 'retained-original-root'
    if change == 'scope':
        view.frame.active = (view.bound_workspace, None)
    elif change == 'root-object':
        view.frame.library = LibraryApp(tmp_path / 'replacement')
    elif change == 'same-path-root':
        prior_root.rename(retained)
        shutil.copytree(retained, prior_root)
    else:
        event = SimpleNamespace(GetEventObject=lambda: view, Skip=Mock())
        view._search_destroyed(event)
        event.Skip.assert_called_once()
    try:
        with pytest.raises(InterruptedError) as caught:
            done(page)
        failed(caught.value)
        assert view.status.value == before_status
        assert not ({row.ref for row in page.results} & set(view.bound_library._selections))
    finally:
        if change == 'same-path-root':
            shutil.rmtree(prior_root)
            retained.rename(prior_root)


def test_child_destroy_event_does_not_withdraw_search_window(view):
    page = first(view)
    event = SimpleNamespace(GetEventObject=lambda: object(), Skip=Mock())
    view._search_destroyed(event)
    assert view._search_current()
    assert copy_row(view, page.results[0]) == page.results[0].quote


def test_archived_bound_topic_refuses_navigation_with_fresh_authorization(view):
    first(view)
    before = snapshot(view)
    with view.bound_library.topic_transaction(view.bound_workspace) as tree:
        tree.archive(view.bound_topic)
    view.on_next_result_page()
    operation, _, failed = submitted(view)
    with pytest.raises(ValueError) as caught:
        operation()
    failed(caught.value)
    assert snapshot(view) == before


def test_more_than_500_sources_remain_reachable_after_an_empty_source_window(view):
    # The initial source and 500 additional editions exceed the old Main cap.
    editions = []
    for number in range(500):
        phrase = f'uniquesourceword{number:04d}'
        row = view.bound_library.add_text(view.bound_workspace, f'Edition {number:04d}', phrase)
        editions.append((row['edition'], phrase))
    edition, phrase = max(editions)
    # Ensure the selected matching edition lies beyond the first 500. The
    # existing initial edition may sort after it; choose that one if needed.
    last = max(edition, view.initial_edition)
    if last != edition:
        view.query.value = 'needle'
    else:
        view.query.value = phrase
    page = first(view)
    assert page.edition_start == 1 and page.edition_end == 500
    assert page.results == () and page.next_cursor is not None
    assert view.next_result_page.enabled
    assert 'later windows remain available' in view.status.value
    view.on_next_result_page()
    final = finish(view)
    assert final.edition_start == final.edition_end == 501
    assert final.results
    assert copy_row(view, final.results[0]) == final.results[0].quote


def test_rendering_failure_keeps_old_page_and_its_verified_source_handles(view):
    initial = first(view)
    before = snapshot(view)
    view.on_next_result_page()
    operation, done, failed = submitted(view)
    with ThreadPoolExecutor(max_workers=1) as executor:
        page = executor.submit(operation).result(timeout=20)
    view.grid.refresh.side_effect = [RuntimeError('Native view rejected the new page'), None]
    with pytest.raises(RuntimeError, match='Native view') as caught:
        done(page)
    failed(caught.value)
    assert snapshot(view) == before
    assert copy_row(view, initial.results[0]) == initial.results[0].quote
    assert not ({row.ref for row in page.results} & set(view.bound_library._selections))


def test_malformed_candidate_does_not_retire_the_visible_page(view):
    initial = first(view)
    before = snapshot(view)
    view.on_next_result_page()
    operation, done, failed = submitted(view)
    page = operation()
    bad = replace(page, results=(replace(page.results[0], workspace='foreign'),))
    with pytest.raises(ValueError, match='out-of-scope') as caught:
        done(bad)
    failed(caught.value)
    assert snapshot(view) == before
    assert copy_row(view, initial.results[0]) == initial.results[0].quote


def test_retirement_only_withdraws_the_exact_owned_selection_value(view):
    initial = first(view)
    old = initial.results[0]
    original_selection = view.bound_library._selections[old.ref]
    replacement = replace(original_selection)
    assert replacement == original_selection and replacement is not original_selection
    view.bound_library._selections[old.ref] = replacement
    view.on_next_result_page()
    finish(view)
    assert view.bound_library._selections[old.ref] is replacement
    assert all(row.ref not in view.bound_library._selections for row in initial.results[1:])


def test_reentrant_option_change_during_view_adoption_cannot_restore_stale_rows(view):
    first(view)
    view.on_next_result_page()
    operation, done, _ = submitted(view)
    page = operation()
    def changed():
        view.grid.refresh.side_effect = None
        view.query.value = 'new request'
        view.on_options_changed()
    view.grid.refresh.side_effect = changed
    with pytest.raises(InterruptedError):
        done(page)
    assert not view.model.hits and not view.results
    assert not view.bound_library._selections
    assert view._accepted_request is None
    assert not view.next_result_page.enabled
    assert 'Search options changed' in view.status.value
