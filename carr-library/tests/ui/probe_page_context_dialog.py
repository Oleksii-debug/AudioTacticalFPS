"""Run explicitly with PR26/34 dependencies; fake wx, real Context/anchors."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context
from carr_library.ui.page_context_dialog import PageSession, show_page_context


@pytest.fixture
def source():
    selection = ('A', 'hit', 'old')
    identity = Identity('A', 'edition', 'a'*64, 'old')
    meta = Metadata('Author', 'Title')
    raw = ['Перший текст\n', 'Čas second']
    loc = [Locator('physical_page', 3, 'iv', True), Locator('physical_page', 4)]
    anchors = [make_anchor(identity, l, t, 0, len(t), meta, text_verified=True)
               for l, t in zip(loc, raw)]
    state = SimpleNamespace(selection=selection, fault=None, anchors=anchors)
    def loader(i, l):
        if state.fault == 'missing':
            raise FileNotFoundError
        text = raw[loc.index(l)]
        if state.fault == 'drift':
            text += ' changed'
        return i, l, text, state.fault != 'unverified'
    def load(w, ref, rev):
        assert (w, ref, rev) == selection
        return open_context(w, state.anchors, loader)
    return selection, state, raw, load


def test_pages(source):
    selection, state, raw, load = source
    session = PageSession(selection, lambda: state.selection, load)
    for index in (0, 1, 0):
        page, count = session.visit(index)
        assert page.raw_text == raw[index] and count == 2
        assert ('p. iv' if index == 0 else 'printed page unverified') in page.citation
    for index in (-1, 2, True):
        with pytest.raises(IndexError):
            session.visit(index)
    assert session.position == 0


@pytest.mark.parametrize('fault', ['missing', 'drift', 'unverified'])
def test_reload_failure(source, fault):
    selection, state, _, load = source
    session = PageSession(selection, lambda: state.selection, load)
    session.visit(0)
    state.fault = fault
    with pytest.raises((ValueError, FileNotFoundError)):
        session.visit(1)
    assert session.position == 0


def test_scope_change_before_or_during_load(source):
    selection, state, _, load = source
    def changed(*args):
        ctx = load(*args)
        state.selection = ('B', 'hit', 'old')
        return ctx
    session = PageSession(selection, lambda: state.selection, changed)
    with pytest.raises(ValueError):
        session.visit(0)
    forbidden = Mock()
    with pytest.raises(ValueError):
        PageSession(selection, lambda: state.selection, forbidden).visit(0)
    forbidden.assert_not_called()


def test_different_anchor_rejected(source):
    selection, state, _, load = source
    session = PageSession(selection, lambda: state.selection, load)
    session.visit(0)
    state.anchors = [replace(a, metadata=Metadata('Other', 'Title')) for a in state.anchors]
    with pytest.raises(ValueError):
        session.visit(1)


def test_native_adapter_controls_and_failure(source, monkeypatch):
    selection, state, raw, load = source
    status, reader, prev, nxt, back, dialog = [Mock() for _ in range(6)]
    wx = SimpleNamespace(Dialog=Mock(return_value=dialog), BoxSizer=Mock(), StaticText=Mock(),
                         TextCtrl=Mock(side_effect=[status, reader]), Button=Mock(side_effect=[prev,nxt,back]),
                         VERTICAL=1,HORIZONTAL=2,ALL=4,EXPAND=8,TE_READONLY=16,TE_MULTILINE=32,
                         ID_CANCEL=64,EVT_BUTTON=object())
    def interaction():
        reader.ChangeValue.assert_called_with(raw[0])
        prev.Enable.assert_called_with(False)
        nxt.Enable.assert_called_with(True)
        nxt.Bind.call_args.args[1](None)
        reader.ChangeValue.assert_called_with(raw[1])
        status.ChangeValue.assert_called_with('Context page 2 of 2\nAuthor, Title, PDF page 4 (printed page unverified)')
        prev.Enable.assert_called_with(True)
        nxt.Enable.assert_called_with(False)
        state.fault = 'drift'
        prev.Bind.call_args.args[1](None)
        reader.ChangeValue.assert_called_with('')
        prev.Enable.assert_called_with(False)
        nxt.Enable.assert_called_with(False)
        status.SetFocus.assert_called()
    dialog.ShowModal.side_effect = interaction
    monkeypatch.setitem(sys.modules, 'wx', wx)
    show_page_context(None, selection, lambda: state.selection, load)
    dialog.Destroy.assert_called_once()
