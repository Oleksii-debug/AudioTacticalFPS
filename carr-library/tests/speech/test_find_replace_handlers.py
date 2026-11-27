"""Execute native find/replace handlers through the reader's real worker contract."""
import importlib.util
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.ui import find_replace_dialog as ui


_spec = importlib.util.spec_from_file_location('carr_find_replace_widget_fixtures',
    Path(__file__).with_name('test_pronunciation_handlers.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
Widget, Sizer, ReaderWorker = _fixtures.Widget, _fixtures.Sizer, _fixtures.ReaderWorker


@pytest.fixture
def view():
    owner = get_ident()
    state = SimpleNamespace(document=ReadingDocument('😀 One\r\none two one'), selection=(0, 0),
                            authorized=True, closed=False, applied=0, undone=0, warning='')
    def guard():
        if not state.authorized or state.closed:
            raise ValueError('The selected reading source is no longer available.')
    def capture():
        assert get_ident() == owner
        guard(); return ui.ReadingSnapshot.capture(state.document, state.selection)
    def select(expected, selection):
        assert capture() == expected
        state.selection = selection
    def apply(preview):
        ui.apply_replacement(state.document, preview, capture())
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
                 'EVT_TEXT', 'EVT_CHECKBOX', 'EVT_CHOICE', 'ID_CANCEL')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'CheckBox', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    cancel = Event(); worker = ReaderWorker(cancel)
    dialog = ui.create_find_replace_dialog(None, capture=capture, select=select, apply=apply,
        undo=undo, guard=guard, run=worker, cancel=cancel, wx_module=wx)
    harness = SimpleNamespace(dialog=dialog, state=state, worker=worker, cancel=cancel, capture=capture)
    try:
        yield harness
    finally:
        if dialog.alive:
            dialog.finish_close()
        worker.close()
        if not dialog.destroyed:
            dialog.Destroy()


def query(view, pattern='one', replacement='ONE', *, regex=False, ignore_case=False):
    view.dialog.pattern.ChangeValue(pattern)
    view.dialog.replacement.ChangeValue(replacement)
    view.dialog.regex.SetValue(regex)
    view.dialog.ignore_case.SetValue(ignore_case)
    view.dialog.on_change()


def test_actual_find_previous_next_wrap_and_review_single_replace_undo(view):
    query(view, ignore_case=True)
    view.dialog.on_find(); view.worker.drain()
    assert view.state.selection == (2, 5)
    assert 'Match 1 of 3' in view.dialog.status.value
    assert view.state.document.text == view.state.document.source_text
    view.dialog.on_find(); view.worker.drain()
    assert view.state.selection == (7, 10)
    view.dialog.on_find(reverse=True); view.worker.drain()
    assert view.state.selection == (2, 5)
    view.dialog.on_find(reverse=True); view.worker.drain()
    assert view.state.selection == (15, 18) and 'Wrapped' in view.dialog.status.value
    view.dialog.on_preview(); view.worker.drain()
    assert view.dialog.buttons['apply'].enabled
    view.dialog.on_apply()
    assert view.state.document.text == '😀 One\r\none two ONE'
    assert view.state.applied == 1 and view.state.document.revision == 1
    view.dialog.on_undo()
    assert view.state.undone == 1 and view.state.document.text == view.state.document.source_text


def test_selected_scope_stays_fixed_while_find_moves_selection(view):
    view.state.selection = (7, 14)
    view.dialog.scope_snapshot = view.capture()
    view.dialog.scope.SetSelection(1)
    query(view)
    view.dialog.on_find(); view.worker.drain()
    assert view.state.selection == (7, 10)
    view.dialog.on_preview(replace_all=True); view.worker.drain()
    assert len(view.dialog.preview.indexes) == 1
    view.dialog.on_apply()
    assert view.state.document.text == '😀 One\r\nONE two one'
    assert view.dialog.scope.GetSelection() == 0


def test_replace_all_is_explicit_and_one_undoable_edit(view):
    query(view, pattern=r'\bone\b', replacement='ONE', regex=True, ignore_case=True)
    view.dialog.on_preview(replace_all=True); view.worker.drain()
    assert view.state.applied == 0
    assert len(view.dialog.preview.indexes) == 3
    view.dialog.on_apply()
    assert view.state.document.text == '😀 ONE\r\nONE two ONE'
    assert view.state.document.revision == 1
    assert view.state.document.source_text == '😀 One\r\none two one'
    view.dialog.on_undo()
    assert view.state.document.text == view.state.document.source_text


@pytest.mark.parametrize('change', ['text', 'selection', 'form', 'authorization'])
def test_stale_worker_completion_cannot_offer_apply(view, change):
    query(view); view.worker.gate.clear()
    view.dialog.on_preview(replace_all=True)
    assert view.dialog.busy and not view.dialog.buttons['apply'].enabled
    if change == 'text':
        view.state.document.replace(0, 1, 'changed')
    elif change == 'selection':
        view.state.selection = (7, 10)
    elif change == 'form':
        view.dialog.replacement.ChangeValue('different')
    else:
        view.state.authorized = False
    before = view.state.document.to_dict()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.preview is None and not view.dialog.buttons['apply'].enabled
    assert view.state.document.to_dict() == before and view.state.applied == 0


def test_cancel_before_completion_preserves_copy_and_can_retry(view):
    query(view); view.worker.gate.clear()
    view.dialog.on_preview(replace_all=True); view.dialog.on_cancel()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.preview is None and view.state.applied == 0
    assert view.state.document.text == view.state.document.source_text
    view.dialog.on_preview(replace_all=True); view.worker.drain()
    assert view.dialog.preview is not None


def test_closed_dialog_ignores_late_worker_result_without_native_access(view):
    query(view); view.worker.gate.clear()
    view.dialog.on_preview(replace_all=True)
    view.dialog.finish_close(); view.dialog.Destroy()
    view.worker.gate.set(); view.worker.drain()
    assert not view.dialog.alive and view.state.applied == 0


def test_stale_apply_and_stale_undo_are_refused(view):
    query(view); view.dialog.on_preview(replace_all=True); view.worker.drain()
    view.state.selection = (7, 10)
    view.dialog.on_apply()
    assert view.state.applied == 0
    view.dialog.on_preview(replace_all=True); view.worker.drain(); view.dialog.on_apply()
    assert view.state.applied == 1
    view.state.document.replace(0, 1, 'new')
    before = view.state.document.to_dict()
    view.dialog.on_undo()
    assert view.state.undone == 0 and view.state.document.to_dict() == before


def test_committed_apply_survives_optional_recovery_warning(view):
    query(view); view.dialog.on_preview(replace_all=True); view.worker.drain()
    view.state.warning = 'Recovery save failed. Use Save As before closing.'
    view.dialog.on_apply()
    assert view.state.applied == 1 and 'Recovery save failed' in view.dialog.status.value
    assert view.dialog.applied is not None


def test_reader_worker_rejection_and_no_match_keep_controls_usable(view):
    query(view); view.worker.reject = True
    view.dialog.on_preview(replace_all=True)
    assert not view.dialog.busy and view.dialog.buttons['all'].enabled
    assert view.state.applied == 0
    view.worker.reject = False; query(view, pattern='missing')
    view.dialog.on_find(); view.worker.drain()
    assert 'No matches' in view.dialog.status.value
    assert not view.dialog.busy and view.dialog.preview is None
