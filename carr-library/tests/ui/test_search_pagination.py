"""Actual paged service behind native-owner-aware desktop handler stand-ins."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.search_model import Hit, SearchModel


class Control:
    def __init__(self, value='', selection=0):
        self.owner = get_ident()
        self.value, self.selection, self.enabled = value, selection, True
        self.focused = False

    def check(self):
        assert get_ident() == self.owner, 'A worker touched a native control.'

    def GetValue(self):
        self.check()
        return self.value

    def GetSelection(self):
        self.check()
        return self.selection

    def ChangeValue(self, value):
        self.check()
        self.value = value

    def Enable(self, enabled=True):
        self.check()
        self.enabled = enabled

    def Disable(self):
        self.Enable(False)

    def SetFocus(self):
        self.check()
        self.focused = True

    def SetInsertionPoint(self, position):
        self.check()


@pytest.fixture
def panel(tmp_path):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DesktopSearchPanel')
    env = {'PagedSearchPanel': object, 'Hit': Hit}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), env)
    p = object.__new__(env['DesktopSearchPanel'])
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Paged handler scope')
    source = library.add_text(workspace, 'Source',
        '\f'.join(f'Exact section {n}: needle and original evidence.' for n in range(205)))
    assert source['state'] == 'complete'
    p.frame = SimpleNamespace(working=False, library=library, report=Mock(),
        cancel_event=Event(), run_job=Mock())
    p.bound_library = library
    p.bound_workspace, p.bound_topic = workspace, None
    p.model = SearchModel()
    p.model.set_workspace(workspace)
    for name in ('query', 'retrieval', 'result_mode', 'collection', 'details', 'status',
                 'previous_page', 'next_page'):
        setattr(p, name, Control())
    p.query.value = 'needle'
    p.filters, p.results, p.collection_ids = {}, {}, [None]
    p._context_epoch = 0
    p.grid, p._refresh_details = Mock(), Mock()
    p._reset_pages()
    return p


def execute(panel, *, complete=True):
    _, operation, done, failed = panel.frame.run_job.call_args.args
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            result = pool.submit(operation).result()
        except Exception as exc:
            failed(exc)
            return None
    if complete:
        done(result)
    return result, done


def test_next_previous_and_last_page_keep_exact_source_results(panel):
    panel.on_search(None)
    first, _ = execute(panel)
    first_quotes = [(h.page, h.quote) for h in first]
    assert len(first) == 100 and panel._page_number == 1
    assert not panel.previous_page.enabled and panel.next_page.enabled
    assert panel.grid.focus_row.call_args.args == (0,)
    panel.on_next_page()
    second, _ = execute(panel)
    assert len(second) == 100 and panel._page_number == 2
    assert not set(first_quotes).intersection((h.page, h.quote) for h in second)
    assert panel.previous_page.enabled and panel.next_page.enabled
    panel.on_next_page()
    third, _ = execute(panel)
    assert len(third) == 5 and panel._page_number == 3
    assert panel.previous_page.enabled and not panel.next_page.enabled
    assert 'last results page' in panel.status.value
    panel.on_previous_page()
    repeated, _ = execute(panel)
    assert [(h.page, h.quote) for h in repeated] == [(h.page, h.quote) for h in second]
    assert panel._page_number == 2 and panel.next_page.enabled
    selected = repeated[0]
    assert panel.frame.library.copy(selected.workspace, selected.ref, selected.revision, 'quote') == selected.quote


@pytest.mark.parametrize('change', ['options', 'workspace', 'library'])
def test_late_page_cannot_replace_changed_search_scope(panel, change):
    panel.on_search(None)
    execute(panel)
    panel.on_next_page()
    page, done = execute(panel, complete=False)
    if change == 'options':
        panel.query.value = 'different'
        panel.on_options_changed()
    elif change == 'workspace':
        panel.model.set_workspace('other')
        panel._context_epoch += 1
    else:
        panel.frame.library = object()
    before = (panel.model.hits, dict(panel.results), panel._page_cursor,
              panel._next_cursor, panel._page_number, panel.status.value)
    done(page)
    assert before == (panel.model.hits, panel.results, panel._page_cursor,
                      panel._next_cursor, panel._page_number, panel.status.value)


def test_cancelled_next_page_keeps_previous_results_and_retry_cursor(panel):
    panel.on_search(None)
    execute(panel)
    previous = (panel.model.hits, dict(panel.results), panel._next_cursor,
                panel._page_cursor, panel._page_history)
    panel.on_next_page()
    panel.frame.cancel_event.set()
    assert execute(panel) is None
    assert previous == (panel.model.hits, panel.results, panel._next_cursor,
                        panel._page_cursor, panel._page_history)
    assert panel.next_page.enabled and panel.status.focused
    panel.frame.cancel_event.clear()
    panel.on_next_page()
    assert execute(panel) is not None and panel._page_number == 2


def test_changed_options_drop_all_continuations_and_previous_history(panel):
    panel.on_search(None)
    execute(panel)
    panel.on_next_page()
    execute(panel)
    assert panel._page_history
    panel.on_options_changed()
    assert not panel.model.hits and not panel.results
    assert panel._page_cursor is panel._next_cursor is None
    assert panel._page_history == () and panel._page_number == 1
    assert not panel.previous_page.enabled and not panel.next_page.enabled


def test_repeated_next_while_worker_owns_operation_does_not_change_page(panel):
    panel.on_search(None)
    execute(panel)
    panel.frame.run_job.reset_mock()
    panel.frame.working = True
    old = panel._page_cursor, panel._next_cursor, panel._page_number, panel.model.hits
    panel.on_next_page()
    assert old == (panel._page_cursor, panel._next_cursor, panel._page_number, panel.model.hits)
    panel.frame.run_job.assert_not_called()
