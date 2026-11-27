"""Installed-compatible paging policy and real service calls through UI handlers."""
import ast
from importlib.util import find_spec
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.search_model import Hit, SearchModel
from carr_library.ui.search_paging import SearchNavigation


def test_navigation_retains_bounded_back_history_without_claiming_full_history():
    nav = SearchNavigation()
    nav.accept('first', None, 'cursor-1')
    for number in range(1, 150):
        nav.accept('next', f'cursor-{number}', f'cursor-{number + 1}')
    assert nav.number == 150 and len(nav.history) == 128
    for _ in range(128):
        target = nav.target('previous')
        later = nav.cursor
        nav.accept('previous', target, later)
    assert nav.number == 22 and not nav.history
    with pytest.raises(ValueError, match='unavailable'):
        nav.target('previous')
    assert nav.target('next') == 'cursor-22'


def test_failed_or_stale_completion_does_not_advance_navigation():
    nav = SearchNavigation()
    nav.accept('first', None, 'next')
    previous = (nav.number, nav.cursor, nav.next_cursor, tuple(nav.history))
    with pytest.raises(ValueError, match='changed'):
        nav.accept('next', 'different', None)
    assert (nav.number, nav.cursor, nav.next_cursor, tuple(nav.history)) == previous
    nav.reset()
    assert nav.number == 0 and nav.cursor is nav.next_cursor is None and not nav.history


@pytest.fixture
def panel(tmp_path):
    # Resolve the actually imported package, including installed-wheel runs.
    origin = Path(find_spec('carr_library.ui.desktop_app').origin)
    tree = ast.parse(origin.read_text(encoding='utf-8'))
    declaration, = [node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == 'DesktopSearchPanel']
    environment = {'PagedSearchPanel': object, 'Hit': Hit}
    exec(compile(ast.Module(body=[declaration], type_ignores=[]), str(origin), 'exec'), environment)
    app = LibraryApp(tmp_path / 'real source library')
    workspace = app.create_workspace('Paging scope')
    texts = [f'Railway source segment {number:04d}.' for number in range(205)]
    imported = app.add_text(workspace, 'Paging original', '\f'.join(texts))
    assert imported['state'] == 'complete'
    panel = object.__new__(environment['DesktopSearchPanel'])
    panel.bound_workspace, panel.bound_topic = workspace, None
    panel.model = SearchModel()
    panel.model.set_workspace(workspace)
    panel.query = Mock()
    panel.query.GetValue.return_value = 'Railway'
    panel.retrieval = Mock()
    panel.retrieval.GetSelection.return_value = 0
    panel.result_mode = Mock()
    panel.result_mode.GetSelection.return_value = 0
    panel.collection = Mock()
    panel.collection.GetSelection.return_value = 0
    panel.collection_ids = [None]
    panel.filters, panel.results = {}, {}
    panel._context_epoch = 0
    panel.grid, panel.details, panel.status = Mock(), Mock(), Mock()
    panel._refresh_details = Mock()
    panel.previous_search_page, panel.next_search_page = Mock(), Mock()
    panel.frame = SimpleNamespace(working=False, cancel_event=Event(), run_job=Mock(),
                                  library=app, report=Mock())
    panel.bound_library = app
    return panel


def submit(panel, direction='first'):
    panel.on_search(None, direction=direction)
    _, operation, complete = panel.frame.run_job.call_args.args
    failed = panel.frame.run_job.call_args.kwargs['failed']
    return operation, complete, failed


def finish(panel, direction='first'):
    operation, complete, _ = submit(panel, direction)
    page = operation()
    complete(page)
    return page


def quotes(panel):
    return tuple(hit.quote for hit in panel.model.hits)


def test_real_desktop_handlers_reach_all_segments_and_go_back_to_exact_page(panel):
    first = finish(panel)
    initial = quotes(panel)
    assert len(initial) == 100 and first.next_cursor
    assert panel._navigation().number == 1
    panel.previous_search_page.Enable.assert_called_with(False)
    panel.next_search_page.Enable.assert_called_with(True)
    finish(panel, 'next')
    second = quotes(panel)
    assert len(second) == 100 and not set(initial).intersection(second)
    finish(panel, 'previous')
    assert quotes(panel) == initial and panel._navigation().number == 1
    finish(panel, 'next')
    assert quotes(panel) == second
    last = finish(panel, 'next')
    assert len(last.results) == 5 and last.complete
    assert len(set(initial + second + quotes(panel))) == 205
    panel.next_search_page.Enable.assert_called_with(False)
    assert 'complete' in panel.status.ChangeValue.call_args.args[0]


def test_failed_next_page_keeps_retry_cursor_and_cannot_display_tampered_original(panel):
    finish(panel)
    nav = panel._navigation()
    before = (nav.number, nav.cursor, nav.next_cursor)
    operation, _, failed = submit(panel, 'next')
    original, = panel.bound_library.assets_dir.glob('*.txt')
    data = original.read_bytes()
    original.write_bytes(data.replace(b'Railway', b'Changed'))
    with pytest.raises(ValueError) as error:
        operation()
    failed(error.value)
    assert (nav.number, nav.cursor, nav.next_cursor) == before
    assert not panel.results and not panel.model.hits
    panel.next_search_page.Enable.assert_called_with(True)
    assert 'failed' in panel.status.ChangeValue.call_args.args[0]
    original.write_bytes(data)
    finish(panel, 'next')
    assert nav.number == 2


def test_options_change_withdraws_cursor_and_ignores_already_queued_completion(panel):
    finish(panel)
    operation, complete, failed = submit(panel, 'next')
    page = operation()
    panel.on_options_changed()
    before = panel.status.ChangeValue.call_count
    complete(page)
    failed(ValueError('old request'))
    assert panel.status.ChangeValue.call_count == before
    assert panel._navigation().number == 0 and not panel.results
    panel.next_search_page.Enable.assert_called_with(False)


def test_late_success_and_failure_after_library_change_cannot_update_panel(panel):
    operation, complete, failed = submit(panel)
    page = operation()
    panel.frame.library = object()
    before = panel.status.ChangeValue.call_count
    complete(page)
    failed(ValueError('old library'))
    assert panel.status.ChangeValue.call_count == before
    assert panel._navigation().number == 0 and not panel.results
