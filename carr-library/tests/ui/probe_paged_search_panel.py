"""Explicit candidate probe: actual panel classes; native wx/reader are test doubles."""
import ast
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@dataclass(frozen=True)
class Hit:
    workspace: str = 'A'
    ref: str = 'hit'
    revision: str = 'old'
    title: str = 'Title'
    author: str = 'Author'
    edition: str = 'Edition'
    page: str = 'Printed page unknown'
    citation: str = 'Citation'
    quote: str = 'Точний текст'


def load_class(filename, classname, env):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui' / filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == classname)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), env)
    return env[classname]


@pytest.fixture
def panel():
    base = load_class('search_panel.py', 'SearchPanel', {'wx': SimpleNamespace(Panel=object)})
    show = Mock()
    cls = load_class('paged_search_panel.py', 'PagedSearchPanel',
                     {'SearchPanel': base, 'show_page_context': show})
    p = object.__new__(cls)
    p._context_epoch = 1
    p._load_context = Mock()
    p.more_service = Mock(side_effect=AssertionError('legacy More called'))
    p.model, p.grid, p.details, p.status = Mock(), Mock(), Mock(), Mock()
    p.go = Mock()
    hit = Hit()
    p.model.workspace = hit.workspace
    p.model.hit.return_value = hit
    p.grid.selected_rows.return_value = [0]
    return p, show, hit


def test_open_and_restore_exact_selection(panel):
    p, show, hit = panel
    def modal(parent, identity, current, load):
        assert parent is p and identity == ('A', 'hit', 'old')
        assert current() == identity and load is p._load_context
    show.side_effect = modal
    p.on_more(None)
    show.assert_called_once()
    p.more_service.assert_not_called()
    p.grid.focus_row.assert_called_once_with(0)
    assert hit.quote in p.details.ChangeValue.call_args.args[0]


@pytest.mark.parametrize('change', ['scope', 'revision', 'selection', 'epoch'])
def test_modal_change_never_restores_old_row(panel, change):
    p, show, hit = panel
    def modal(parent, identity, current, load):
        assert current() == identity
        if change == 'scope':
            p.model.workspace = 'B'
        elif change == 'revision':
            p.model.hit.return_value = replace(hit, revision='new')
        elif change == 'selection':
            p.grid.selected_rows.return_value = []
        else:
            p.set_workspace('A')
        assert current() is None
    show.side_effect = modal
    p.on_more(None)
    show.assert_called_once()
    p.grid.focus_row.assert_not_called()
    p.grid.select_rows.assert_not_called()


def test_search_invalidates_modal_even_with_same_hit(panel):
    p, show, _ = panel
    p.query, p.retrieval, p.result_mode = Mock(), Mock(), Mock()
    p.query.GetValue.return_value = ''
    def modal(parent, identity, current, load):
        p.on_search(None)
        assert current() is None
    show.side_effect = modal
    p.on_more(None)
    p.grid.focus_row.assert_not_called()


def test_modal_error_reports_retry_without_focus_restore(panel):
    p, show, _ = panel
    show.side_effect = RuntimeError('missing dependency')
    p.on_more(None)
    assert 'Context unavailable' in p.status.ChangeValue.call_args.args[0]
    p.status.SetFocus.assert_called_once()
    p.grid.focus_row.assert_not_called()


def test_out_of_scope_selection_never_opens_dialog(panel):
    p, show, _ = panel
    p.model.workspace = 'B'
    p.on_more(None)
    show.assert_not_called()
    p.grid.focus_row.assert_not_called()
