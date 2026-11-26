"""Navigation revalidates in workers; late callbacks cannot touch a closed dialog."""
from concurrent.futures import ThreadPoolExecutor
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context
from carr_library.ui.page_context_dialog import show_page_context


@pytest.fixture
def context():
    selection = ('library','hit','revision')
    identity = Identity('library','edition','a'*64,'revision')
    anchors = tuple(make_anchor(identity,Locator('physical_page',i),text,0,len(text),
        Metadata('Author','Title'),text_verified=True) for i,text in enumerate(('First page','Second page'),1))
    state = SimpleNamespace(selection=selection,valid=True)
    def page_loader(identity,locator):
        anchor = anchors[locator.index - 1]
        return identity,locator,anchor.quote,state.valid
    def load(*args):
        assert args == selection
        return open_context('library',anchors,page_loader)
    return selection,state,load


@pytest.fixture
def controls(monkeypatch):
    status,reader,previous,following,back,dialog = [Mock() for _ in range(6)]
    wx = SimpleNamespace(Dialog=Mock(return_value=dialog),BoxSizer=Mock(),StaticText=Mock(),
        TextCtrl=Mock(side_effect=[status,reader]),Button=Mock(side_effect=[previous,following,back]),
        VERTICAL=1,HORIZONTAL=2,ALL=4,EXPAND=8,TE_READONLY=16,TE_MULTILINE=32,ID_CANCEL=64,EVT_BUTTON=object())
    monkeypatch.setitem(sys.modules,'wx',wx)
    return SimpleNamespace(status=status,reader=reader,previous=previous,following=following,dialog=dialog)


def test_next_page_uses_fresh_background_verification_and_clears_failed_source(context,controls):
    selection,state,load = context
    submit = Mock()
    def interaction():
        controls.reader.ChangeValue.assert_called_with('First page')
        controls.following.Bind.call_args.args[1](None)
        _,operation,complete,failed = submit.call_args.args
        controls.reader.ChangeValue.assert_called_with('')
        with ThreadPoolExecutor(max_workers=1) as pool:
            loaded = pool.submit(operation).result()
        controls.reader.ChangeValue.assert_called_with('')
        complete(loaded)
        controls.reader.ChangeValue.assert_called_with('Second page')
        state.valid = False
        controls.previous.Bind.call_args.args[1](None)
        _,operation,_,failed = submit.call_args.args
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ValueError) as error:
                pool.submit(operation).result()
        failed(error.value)
        controls.reader.ChangeValue.assert_called_with('')
        assert 'Context unavailable' in controls.status.ChangeValue.call_args.args[0]
    controls.dialog.ShowModal.side_effect = interaction
    show_page_context(None,selection,lambda:state.selection,load,initial_context=load(*selection),run_job=submit)


@pytest.mark.parametrize('completion',['success','failure'])
def test_closed_dialog_ignores_pending_navigation_callback(context,controls,completion):
    selection,state,load = context
    submit = Mock()
    controls.dialog.ShowModal.side_effect = lambda: controls.following.Bind.call_args.args[1](None)
    show_page_context(None,selection,lambda:state.selection,load,initial_context=load(*selection),run_job=submit)
    _,operation,complete,failed = submit.call_args.args
    before = controls.reader.mock_calls.copy(),controls.status.mock_calls.copy()
    if completion == 'success':
        complete(operation())
    else:
        failed(ValueError('Source changed'))
    assert (controls.reader.mock_calls,controls.status.mock_calls) == before
    controls.dialog.Destroy.assert_called_once()
