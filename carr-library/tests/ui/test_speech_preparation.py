"""Real executor and current ReadingDocument handlers; native peers are disclosed."""
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch
import runpy

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import speech_preparation as ui

_shared = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'speech' / 'test_pronunciation_handlers.py'))
BaseWidget, Sizer, ReaderWorker = _shared['Widget'], _shared['Sizer'], _shared['ReaderWorker']


class Widget(BaseWidget):
    def _delete_native(self):
        self.native()
        for child in tuple(self.children):
            Widget._delete_native(child)
        for handler in tuple(self.handlers.values()):
            if getattr(handler, '__name__', '') in ('on_destroy', 'parent_destroyed'):
                handler(SimpleNamespace(GetId=lambda: id(self), Skip=lambda: None))
        if self.parent is not None:
            self.parent.children.remove(self)
        self.destroyed = True

    def Destroy(self):
        if self.destroyed:
            return False
        Widget._delete_native(self)
        return True


class CheckBox(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, value=False, **kwargs)


def native():
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
        'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE',
        'EVT_CHOICE', 'EVT_CHECKBOX', 'ID_CANCEL', 'EVT_WINDOW_DESTROY')
    return SimpleNamespace(**{key: index + 1 for index, key in enumerate(constants)},
        Dialog=Widget, ScrolledWindow=Widget, StaticText=Widget, TextCtrl=Widget,
        Choice=Widget, CheckBox=CheckBox, Button=Widget, BoxSizer=Sizer)


def configure(view):
    view.mode.SetSelection(1)
    view.locale.SetSelection(2)
    view.expansions['numbers'].SetValue(True)
    view.expansions['units'].SetValue(True)
    view.on_change()


@pytest.fixture
def view(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    store = ReaderDraftStore(root)
    state = SimpleNamespace(document=ReadingDocument('😀 retained\r\nCount 12 kg; 5.'),
        selection=(0, 0), language='en-US', voice='fixture:one',
        authorized=True, applied=0, undone=0, save_failure=False)
    def guard():
        if not state.authorized:
            raise ValueError('The source authorization changed.')
    def capture():
        guard()
        return ui.capture_speech(state.document, state.selection, language=state.language, voice_id=state.voice)
    def apply(proposal):
        ui.apply_speech(state.document, proposal, capture())
        state.selection = (proposal.bounds[0], proposal.bounds[0] + len(proposal.result.text))
        state.applied += 1
        accepted = capture()
        if state.save_failure:
            return accepted, 'The change remains in this reading copy; recovery saving failed.'
        store.save(ReaderResume(state.document, state.voice))
        return accepted, ''
    def undo(applied):
        ui.undo_speech(state.document, applied, capture())
        state.selection = (0, 0)
        state.undone += 1
        store.save(ReaderResume(state.document, state.voice))
        return ''
    cancel = Event()
    worker = ReaderWorker(cancel)
    parent, wx = Widget(), native()
    dialog = ui.create_speech_preparation(parent, capture=capture, apply=apply, undo=undo,
        guard=guard, run=worker, cancel=cancel, wx_module=wx)
    result = SimpleNamespace(root=root, store=store, state=state, cancel=cancel, worker=worker, parent=parent,
        wx=wx, dialog=dialog, capture=capture, apply=apply, undo=undo, guard=guard)
    try:
        yield result
    finally:
        worker.close()
        if not dialog.destroyed:
            dialog.Destroy()
        if not parent.destroyed:
            parent.Destroy()


def review(view):
    configure(view.dialog)
    view.dialog.on_preview()
    assert view.dialog.busy and not view.dialog.buttons['apply'].enabled
    view.worker.drain()
    assert view.dialog.proposal is not None and not view.dialog.busy
    assert view.dialog.buttons['apply'].enabled


def test_default_verbatim_preview_does_not_change_text_or_create_recovery(view):
    before = view.state.document.to_dict()
    view.dialog.on_preview()
    view.worker.drain()
    assert view.dialog.proposal and not view.dialog.proposal.result.changed
    assert not view.dialog.buttons['apply'].enabled
    view.dialog.on_apply()
    assert not view.state.applied and view.state.document.to_dict() == before
    assert not view.store.path.exists()


def test_explicit_profile_and_selected_scope_apply_and_undo_current_history(view):
    source = view.state.document.text
    start = source.index('12')
    view.state.selection = (start, start + len('12 kg'))
    view.dialog.scope.SetSelection(1)
    review(view)
    assert view.dialog.before.value == '12 kg'
    assert view.dialog.after.value == 'twelve kilograms'
    assert view.state.document.text == source and not view.state.applied
    view.dialog.on_apply()
    assert view.state.document.text == source[:start] + 'twelve kilograms' + source[start + 5:]
    assert view.state.document.source_text == source and view.state.applied == 1
    saved = view.store.load()
    assert saved.document.to_dict() == view.state.document.to_dict()
    assert view.state.document.revision == 1
    view.dialog.on_undo()
    assert view.state.document.text == source and view.state.undone == 1
    assert view.state.document.revision == 0 and view.state.document.can_redo
    assert view.worker.worker_threads and all(thread != get_ident() for thread in view.worker.worker_threads)


@pytest.mark.parametrize('phase', ['preview', 'apply'])
@pytest.mark.parametrize('changed', ['text', 'selection', 'revision', 'options', 'authorization', 'language', 'voice'])
def test_stale_worker_result_or_apply_cannot_change_current_scope(view, phase, changed):
    if phase == 'apply':
        review(view)
    else:
        configure(view.dialog)
        view.worker.gate.clear()
        view.dialog.on_preview()
    if changed == 'text':
        view.state.document.set_text('An independent edit')
    elif changed == 'selection':
        view.state.selection = (1, 2)
    elif changed == 'revision':
        view.state.document.title = 'Other title'
    elif changed == 'options':
        view.dialog.locale.SetSelection(1)
    elif changed == 'language':
        view.state.language = 'sk-SK'
    elif changed == 'voice':
        view.state.voice = 'fixture:two'
    else:
        view.state.authorized = False
    before = view.state.document.to_dict()
    if phase == 'apply':
        view.dialog.on_apply()
    else:
        view.worker.gate.set()
        view.worker.drain()
        assert view.dialog.proposal is None
    assert not view.state.applied and view.state.document.to_dict() == before


def test_recovery_failure_keeps_accepted_reading_edit_and_undo(view):
    review(view)
    view.state.save_failure = True
    view.dialog.on_apply()
    assert view.state.applied == 1 and view.dialog.applied is not None
    assert 'recovery saving failed' in view.dialog.status.value
    assert 'twelve kilograms' in view.state.document.text
    view.dialog.on_undo()
    assert view.state.undone == 1


def test_unrelated_later_edit_cannot_be_undone_as_this_preparation(view):
    review(view)
    view.dialog.on_apply()
    view.state.document.set_text('Independent revision')
    view.dialog.on_undo()
    assert view.state.document.text == 'Independent revision' and not view.state.undone


def test_locale_required_and_actual_admission_refusal_cancel_and_retry(view):
    view.dialog.mode.SetSelection(1)
    view.dialog.on_preview()
    assert 'explicitly' in view.dialog.status.value and not view.dialog.busy
    configure(view.dialog)
    view.worker.reject = True
    view.dialog.on_preview()
    assert not view.dialog.busy and 'current reader operation' in view.dialog.status.value
    view.worker.reject = False
    view.worker.gate.clear()
    view.dialog.on_preview()
    view.dialog.on_cancel()
    view.worker.gate.set()
    view.worker.drain()
    assert view.dialog.proposal is None and not view.state.applied
    review(view)


@pytest.mark.parametrize('phase', ['pending', 'queued'])
@pytest.mark.parametrize('mode', ['child', 'parent'])
def test_real_worker_after_forced_deletion_never_touches_native_peers(view, phase, mode):
    configure(view.dialog)
    view.worker.gate.clear()
    view.dialog.on_preview()
    if phase == 'queued':
        view.worker.gate.set()
        view.worker.ready()
    if mode == 'child':
        view.dialog.Destroy()
    else:
        view.parent.Destroy()
    view.worker.gate.set()
    view.worker.drain()
    for method in ('on_preview', 'on_apply', 'on_undo', 'on_change', 'on_cancel', 'on_close', 'controls'):
        getattr(view.dialog, method)()
    assert not view.state.applied and view.dialog.Destroy() is False


def test_close_veto_waits_for_the_existing_real_worker_and_discards_result(view):
    configure(view.dialog)
    view.worker.gate.clear()
    view.dialog.on_preview()
    veto = []
    view.dialog.on_close(SimpleNamespace(CanVeto=lambda: True, Veto=lambda: veto.append(True)))
    assert veto == [True] and view.dialog.alive and view.dialog.busy
    view.worker.gate.set()
    view.worker.drain()
    assert not view.dialog.alive and not view.dialog.busy and not view.state.applied


@pytest.mark.parametrize('error', [KeyboardInterrupt, SystemExit])
def test_interrupted_admission_releases_dialog_state_before_rethrow(view, error):
    def rejected(*args, **kwargs):
        raise error('admission interrupted')
    dialog = ui.create_speech_preparation(view.parent, capture=view.capture, apply=view.apply, undo=view.undo,
        guard=view.guard, run=rejected, cancel=view.cancel, wx_module=view.wx)
    try:
        configure(dialog)
        with pytest.raises(error):
            dialog.on_preview()
        assert not dialog.busy and not view.state.applied
    finally:
        dialog.Destroy()


def test_failed_native_construction_disposes_the_partial_dialog(view):
    created = []
    original = Widget.__init__
    def observed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)
    with patch.object(Widget, '__init__', observed):
        with pytest.raises(ValueError, match='revoked'):
            ui.create_speech_preparation(view.parent, capture=view.capture, apply=view.apply, undo=view.undo,
                guard=lambda: (_ for _ in ()).throw(ValueError('Source revoked')),
                run=view.worker, cancel=view.cancel, wx_module=view.wx)
    assert created and all(widget.destroyed for widget in created)



def test_idle_close_and_refused_admission_never_clear_or_cancel_another_host_operation(view):
    # An unrelated operation already owns the shared event; this dialog has not
    # been accepted. Its own cancellation state cannot alter that event.
    view.cancel.set()
    configure(view.dialog)
    view.worker.reject = True
    view.dialog.on_preview()
    assert view.cancel.is_set() and not view.dialog.busy
    view.cancel.clear()
    view.dialog.Destroy()
    assert not view.cancel.is_set()
