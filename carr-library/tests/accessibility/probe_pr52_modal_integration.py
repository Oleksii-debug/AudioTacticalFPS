"""Independent candidate integration: actual dialog/reader/anchors, mocked wx.
Run with combined exact PR24/26/34/43/52 source tree on PYTHONPATH.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context
from carr_library.ui import page_context_dialog as modal


def context(edition='A', workspace='W'):
    identity = Identity(workspace, edition, ('a' if edition == 'A' else 'b') * 64, 'r1')
    raw = ('Точний уривок ' + edition, 'Čas ' + edition)
    anchors = [make_anchor(identity, Locator('physical_page', n), s, 0, len(s),
                           Metadata('Synthetic author', 'Synthetic title', edition=edition),
                           text_verified=True) for n, s in enumerate(raw, 1)]
    return open_context(workspace, anchors, lambda i, l: (i, l, raw[l.index-1], True))


@pytest.fixture
def harness(monkeypatch):
    status, reader, prev, nxt, back, dialog = [Mock() for _ in range(6)]
    wx = SimpleNamespace(Panel=object, Dialog=Mock(return_value=dialog), BoxSizer=Mock(),
                         StaticText=Mock(), TextCtrl=Mock(side_effect=[status, reader]),
                         Button=Mock(side_effect=[prev, nxt, back]), VERTICAL=1, HORIZONTAL=2,
                         ALL=4, EXPAND=8, TE_READONLY=16, TE_MULTILINE=32, ID_CANCEL=64,
                         EVT_BUTTON=object())
    monkeypatch.setitem(sys.modules, 'wx', wx)
    root = Path(modal.__file__).parent
    env = {'wx': wx, 'show_page_context': modal.show_page_context}
    for filename, classname in [('search_panel.py', 'SearchPanel'),
                                ('paged_search_panel.py', 'PagedSearchPanel')]:
        path = root / filename
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == classname)
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), env)
    p = object.__new__(env['PagedSearchPanel'])
    p._context_epoch = 1
    good = context()
    hit = SimpleNamespace(workspace='W', ref='hit-A', revision='r1', title='Synthetic title',
                          author='Synthetic author', edition='A', page='PDF page 1',
                          quote=good.page(0).quote, citation=good.page(0).citation)
    p.model, p.grid, p.details, p.status, p.go = [Mock() for _ in range(5)]
    p.model.workspace = 'W'
    p.model.hit.return_value = hit
    p.grid.selected_rows.return_value = [0]
    p._load_context = Mock(return_value=good)
    return SimpleNamespace(p=p, status=status, reader=reader, prev=prev, nxt=nxt,
                           dialog=dialog, good=good, hit=hit)


def test_verified_two_page_navigation_and_return(harness):
    h = harness
    h.dialog.ShowModal.side_effect = lambda: h.nxt.Bind.call_args.args[1](None)
    h.p.on_more(None)
    assert [c.args[0] for c in h.reader.ChangeValue.call_args_list] == [
        h.good.page(0).raw_text, h.good.page(1).raw_text]
    assert h.p._load_context.call_args_list[0].args == ('W', 'hit-A', 'r1')
    h.p.grid.focus_row.assert_called_once_with(0)
    h.dialog.Destroy.assert_called_once()


def test_wrong_workspace_is_rejected(harness):
    h = harness
    h.p._load_context.return_value = context(workspace='OTHER')
    h.p.on_more(None)
    h.reader.ChangeValue.assert_called_once_with('')
    assert 'unavailable' in h.status.ChangeValue.call_args.args[0]


def test_substitution_after_first_page_is_rejected(harness):
    h = harness
    h.p._load_context.side_effect = [h.good, context(edition='B')]
    h.dialog.ShowModal.side_effect = lambda: h.nxt.Bind.call_args.args[1](None)
    h.p.on_more(None)
    assert [c.args[0] for c in h.reader.ChangeValue.call_args_list] == [h.good.page(0).raw_text, '']
    assert 'unavailable' in h.status.ChangeValue.call_args.args[0]


def test_scope_change_before_next_visit_clears_context_and_no_old_focus(harness):
    h = harness
    def during_modal():
        h.p.model.workspace = 'OTHER'
        h.nxt.Bind.call_args.args[1](None)
    h.dialog.ShowModal.side_effect = during_modal
    h.p.on_more(None)
    assert h.reader.ChangeValue.call_args.args[0] == ''
    h.p.grid.focus_row.assert_not_called()


def test_first_visit_wrong_edition_must_not_render(harness):
    # Host deliberately misresolves hit-A to another internally valid edition.
    # Acceptance regression, not a claim of a production host leak.
    h = harness
    h.p._load_context.return_value = context(edition='B')
    h.p.on_more(None)
    assert h.reader.ChangeValue.call_args.args[0] == ''


def test_scope_change_while_modal_idle_must_invalidate_visible_text(harness):
    # A programmatic scope switch inside nested event loop, without pressing Next.
    h = harness
    def during_modal():
        h.p.set_workspace('OTHER')
        # Observe the displayed text before closing the modal.
        h.observed = h.reader.ChangeValue.call_args.args[0]
    h.dialog.ShowModal.side_effect = during_modal
    h.p.on_more(None)
    h.p.grid.focus_row.assert_not_called()
    assert h.observed == ''
