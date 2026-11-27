"""Bound spelling handlers, real worker/library, source guards and native-lifetime stand-ins."""
from pathlib import Path
import runpy
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.ui import spellcheck_dialog as ui

_fixtures = runpy.run_path(str(Path(__file__).with_name('test_pronunciation_handlers.py')))
BaseWidget, Sizer, ReaderWorker = (_fixtures[name] for name in ('Widget', 'Sizer', 'ReaderWorker'))
dictionary = runpy.run_path(str(Path(__file__).with_name('test_spellcheck.py')))['dictionary']


class Widget(BaseWidget):
    violations = []
    def native(self):
        if self.destroyed or self.owner != get_ident():
            self.violations.append(self.name)
        super().native()
    def Clear(self): self.native(); self.choices = []; self.selection = -1
    def SetItems(self, items): self.Set(items)


@pytest.fixture
def view(tmp_path):
    owner = get_ident()
    Widget.violations = []
    selected = dictionary(tmp_path)
    state = SimpleNamespace(document=ReadingDocument('😀 hello wurld books'), selection=(0, 0),
                            authorized=True, closed=False, applied=0, undone=0, warning='')
    def guard():
        assert get_ident() == owner
        if not state.authorized or state.closed:
            raise ValueError('The selected reading source is no longer available.')
    def capture():
        guard(); return ui.ReadingSnapshot.capture(state.document, state.selection)
    def apply(preview):
        ui.apply_spelling_preview(state.document, preview, capture())
        state.selection = (0, 0); state.applied += 1
        return capture(), state.warning
    def undo(expected):
        if capture() != expected:
            raise ValueError('The reading copy changed after applying.')
        assert state.document.undo()
        state.selection = (0, 0); state.undone += 1
        return state.warning
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
                 'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE',
                 'EVT_TEXT', 'EVT_CHOICE', 'ID_CANCEL', 'ID_OK', 'EVT_WINDOW_DESTROY', 'FD_OPEN', 'FD_FILE_MUST_EXIST')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    cancel = Event(); worker = ReaderWorker(cancel)
    dialog = ui.create_spellcheck_dialog(None, library_root=tmp_path, capture=capture, apply=apply,
        undo=undo, guard=guard, run=worker, cancel=cancel, wx_module=wx)
    dialog.dictionary.ChangeValue(str(selected))
    harness = SimpleNamespace(dialog=dialog, state=state, worker=worker, cancel=cancel,
                              capture=capture, selected=selected, wx=wx)
    try:
        yield harness
    finally:
        dialog.dispose(); worker.close()
        if not dialog.destroyed:
            dialog.Destroy()
        assert Widget.violations == [], 'Even swallowed late native accesses fail this regression.'


def reviewed(view):
    view.dialog.on_review(); view.worker.drain()
    assert view.dialog.review is not None, view.dialog.status.value
    view.dialog.on_preview()
    assert view.dialog.preview is not None, view.dialog.status.value


def delete(view, mode):
    if mode == 'direct':
        view.dialog.Destroy()
    else:
        identity = view.dialog._native_id
        Widget.Destroy(view.dialog)
        view.dialog.on_destroy(SimpleNamespace(GetId=lambda: identity,
            GetEventObject=lambda: pytest.fail('Destroyed wrapper identity is not ownership.'), Skip=lambda: None))


def test_actual_review_preview_apply_undo_and_optional_recovery_warning(view):
    reviewed(view)
    assert view.state.document.text == view.state.document.source_text
    view.state.warning = 'Recovery save failed; use Save As before closing.'
    view.dialog.on_apply(); view.worker.drain()
    assert view.state.applied == 1 and view.state.document.text == '😀 hello world books'
    assert 'Recovery save failed' in view.dialog.status.value
    assert view.dialog.applied is not None
    view.dialog.on_undo()
    assert view.state.undone == 1 and view.state.document.text == view.state.document.source_text


@pytest.mark.parametrize('change', ['text', 'selection', 'dictionary_choice', 'authorization'])
def test_late_review_does_not_offer_stale_suggestions(view, change):
    view.worker.gate.clear(); view.dialog.on_review()
    if change == 'text': view.state.document.set_text('independent copy')
    elif change == 'selection': view.state.selection = (2, 7)
    elif change == 'dictionary_choice': view.dialog.dictionary.ChangeValue('other.aff')
    else: view.state.authorized = False
    before = view.state.document.to_dict()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.review is None and view.dialog.preview is None
    assert view.state.document.to_dict() == before and view.state.applied == 0


def test_dictionary_mutation_before_apply_rejects_without_reading_change(view):
    reviewed(view)
    view.selected.write_text('SET UTF-8\nTRY abc\n')
    before = view.state.document.to_dict()
    view.dialog.on_apply(); view.worker.drain()
    assert view.state.applied == 0 and view.state.document.to_dict() == before
    assert 'dictionary changed' in view.dialog.status.value


def test_source_guard_is_rechecked_after_async_apply_verification(view):
    reviewed(view)
    view.worker.gate.clear(); view.dialog.on_apply()
    view.state.authorized = False
    view.worker.gate.set(); view.worker.drain()
    assert not view.state.applied and view.state.document.text == view.state.document.source_text


def test_cancel_and_worker_admission_rejection_do_not_edit_text(view):
    view.worker.reject = True
    view.dialog.on_review()
    assert not view.dialog.busy and 'current reader operation' in view.dialog.status.value
    view.worker.reject = False; view.worker.gate.clear()
    view.dialog.on_review(); view.dialog.on_cancel()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.review is None and not view.state.applied
    reviewed(view)


@pytest.mark.parametrize('mode', ['direct', 'parent'])
@pytest.mark.parametrize('phase', ['pending', 'queued'])
def test_real_worker_after_owner_deletion_never_touches_controls_or_applies(view, mode, phase):
    view.worker.gate.clear(); view.dialog.on_review()
    ignored = []
    view.dialog.on_destroy(SimpleNamespace(GetId=lambda: view.dialog._native_id + 1, Skip=lambda: ignored.append(True)))
    assert view.dialog.alive and ignored == [True]
    if phase == 'queued':
        view.worker.gate.set(); view.worker.ready()
    delete(view, mode)
    view.worker.gate.set(); view.worker.drain()
    view.cancel.clear()
    for name in ('on_review', 'on_choose', 'on_preview', 'on_apply', 'on_undo', 'on_change', 'on_cancel'):
        getattr(view.dialog, name)()
    assert view.dialog.Destroy() is False
    assert not view.cancel.is_set() and not view.state.applied


@pytest.mark.parametrize('mode', ['direct', 'parent'])
def test_dictionary_picker_parent_deletion_is_checked_before_native_result(view, mode):
    class Picker(Widget):
        def ShowModal(self):
            delete(view, mode)
            return view.wx.ID_OK
        def GetPath(self):
            pytest.fail('Deleted picker result must not be read.')
    view.wx.FileDialog = Picker
    view.dialog.on_choose()
    assert not view.dialog.alive and not view.state.applied


def test_selected_scope_remains_exact_and_stale_undo_is_rejected(view):
    view.state.document = ReadingDocument('wurld outside; wurld')
    view.state.selection = (15, 20)
    view.dialog.initial = view.capture(); view.dialog.scope.SetSelection(1)
    reviewed(view)
    view.dialog.on_apply(); view.worker.drain()
    assert view.state.document.text == 'wurld outside; world'
    view.state.document.set_text('another independent edit')
    before = view.state.document.to_dict()
    view.dialog.on_undo()
    assert view.state.document.to_dict() == before and not view.state.undone
