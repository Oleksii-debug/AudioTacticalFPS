"""Independent PR43 candidate audit; mock wx, actual PR26/34 verifier.
Run explicitly with exact combined candidate src on PYTHONPATH.
"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context
from carr_library.ui.page_context_dialog import PageSession, show_page_context

SELECTION = ('workspace-A', 'hit-A', 'rev-1')


def context(kind='physical_page', edition='edition-A', index=3):
    identity = Identity(SELECTION[0], edition, 'a' * 64, SELECTION[2])
    loc = Locator(kind, index)
    raw = 'Справжній текст\nČas'
    anchor = make_anchor(identity, loc, raw, 0, len(raw), Metadata('Synthetic author', 'Title'), text_verified=True)
    return open_context(SELECTION[0], [anchor], lambda i, l: (i, l, raw, True))


def render(monkeypatch, ctx):
    status, reader, prev, nxt, back, dialog = [Mock() for _ in range(6)]
    wx = SimpleNamespace(Dialog=Mock(return_value=dialog), BoxSizer=Mock(), StaticText=Mock(),
                         TextCtrl=Mock(side_effect=[status, reader]), Button=Mock(side_effect=[prev,nxt,back]),
                         VERTICAL=1,HORIZONTAL=2,ALL=4,EXPAND=8,TE_READONLY=16,TE_MULTILINE=32,
                         ID_CANCEL=64,EVT_BUTTON=object())
    monkeypatch.setitem(sys.modules, 'wx', wx)
    show_page_context(None, SELECTION, lambda: SELECTION, lambda *args: ctx)
    return wx, status, reader, prev, nxt, back, dialog


def test_physical_context_controls(monkeypatch):
    ctx = context()
    wx, status, reader, prev, nxt, back, dialog = render(monkeypatch, ctx)
    status.ChangeValue.assert_called_once_with('Context page 1 of 1\n' + ctx.page(0).citation)
    reader.ChangeValue.assert_called_once_with(ctx.page(0).raw_text)
    status.SetFocus.assert_called_once()
    prev.Enable.assert_called_once_with(False)
    nxt.Enable.assert_called_once_with(False)
    assert wx.Button.call_args_list[-1].args[1] == wx.ID_CANCEL
    dialog.Destroy.assert_called_once()


def test_unpaginated_status_is_not_a_page(monkeypatch):
    ctx = context('logical_segment', index=3)
    _, status, reader, *_ = render(monkeypatch, ctx)
    assert '(unpaginated)' in ctx.page(0).citation
    reader.ChangeValue.assert_called_once_with(ctx.page(0).raw_text)
    status_text = status.ChangeValue.call_args.args[0]
    assert 'page' not in status_text.split('\n')[0].casefold(), status_text


def test_unpaginated_reader_name_is_not_a_page(monkeypatch):
    _, _, reader, *_ = render(monkeypatch, context('logical_segment'))
    assert 'page' not in reader.SetName.call_args.args[0].casefold()


def test_unpaginated_navigation_names_are_not_pages(monkeypatch):
    wx, *_ = render(monkeypatch, context('logical_segment'))
    names = [c.kwargs['label'] for c in wx.Button.call_args_list[:2]]
    assert all('page' not in label.casefold() for label in names), names


def test_substitution_after_first_visit_rejected():
    current = [context()]
    session = PageSession(SELECTION, lambda: SELECTION, lambda *args: current[0])
    session.visit(0)
    current[0] = context(edition='edition-B')
    with pytest.raises(ValueError, match='anchors changed'):
        session.visit(0)


def test_first_visit_host_boundary_characterization():
    # This is deliberately a characterization, not a release acceptance pass.
    # Host is documented as responsible for the exact hit; adapter cannot detect
    # a self-consistent wrong edition with the same workspace and revision on first load.
    ctx = context(edition='edition-B')
    session = PageSession(SELECTION, lambda: SELECTION, lambda *args: ctx)
    page, _ = session.visit(0)
    assert page.anchor.identity.edition_id == 'edition-B'
