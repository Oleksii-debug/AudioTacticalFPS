"""Actual review handlers and reader worker ownership; synthetic engine responses."""
from hashlib import sha256
import importlib.util
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.spelling import SpellingIssue, SpellingResult, word_ranges
from carr_library.ui import spelling_dialog as ui

_spec = importlib.util.spec_from_file_location('carr_spelling_widget_fixtures',
    Path(__file__).with_name('test_pronunciation_handlers.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
Widget, Sizer, ReaderWorker = _fixtures.Widget, _fixtures.Sizer, _fixtures.ReaderWorker


@pytest.fixture
def view(tmp_path, monkeypatch):
    owner = get_ident()
    state = SimpleNamespace(document=ReadingDocument('😀 hello\r\nwrld wrld'), selection=(0, 0),
        authorized=True, closed=False, applied=0, undone=0, warning='', engine_calls=0)
    def guard():
        assert get_ident() == owner
        if not state.authorized or state.closed:
            raise ValueError('The selected reading source is no longer available.')
    def capture():
        guard(); return ui.ReadingSnapshot.capture(state.document, state.selection)
    def select(expected, selection):
        assert capture() == expected
        state.selection = selection
    def apply(preview):
        ui.apply_correction(state.document, preview, capture())
        state.selection = (0, 0); state.applied += 1
        return capture(), state.warning
    def undo(expected):
        if capture() != expected:
            raise ValueError('The reading copy changed after applying.')
        assert state.document.undo()
        state.selection = (0, 0); state.undone += 1
        return state.warning
    def synthetic_engine(text, profile, *, bounds, cancelled):
        assert get_ident() != owner
        if cancelled():
            raise InterruptedError('cancelled')
        state.engine_calls += 1
        bounds, words = word_ranges(text, bounds=bounds, cancelled=cancelled)
        issues = tuple(SpellingIssue(start, end, word, ('world',))
                       for start, end, word in words if word == 'wrld')
        return SpellingResult(profile, sha256(text.encode()).hexdigest(), bounds, len(words), 'UTF-8', issues)
    monkeypatch.setattr(ui, 'check_spelling', synthetic_engine)
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
                 'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE',
                 'EVT_TEXT', 'EVT_LISTBOX', 'EVT_CHOICE', 'ID_CANCEL')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'Choice', 'ListBox', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    cancel = Event(); worker = ReaderWorker(cancel)
    dialog = ui.create_spelling_dialog(None, capture=capture, select=select, apply=apply,
        undo=undo, guard=guard, run=worker, cancel=cancel, wx_module=wx)
    for name, content in (('library', b'not executable'), ('affix', b'SET UTF-8\n'), ('dictionary', b'1\nworld\n')):
        path = tmp_path / (name + '.fixture'); path.write_bytes(content)
        getattr(dialog, name).ChangeValue(str(path))
    dialog.language.ChangeValue('en-test')
    dialog.provenance.ChangeValue('Original synthetic protocol fixture; no native runtime qualification')
    harness = SimpleNamespace(dialog=dialog, state=state, worker=worker, cancel=cancel, capture=capture)
    try:
        yield harness
    finally:
        if dialog.alive:
            dialog.finish_close()
        worker.close()
        if not dialog.destroyed:
            dialog.Destroy()


def reviewed(view):
    previous_calls = view.state.engine_calls
    view.dialog.on_profile(); view.worker.drain()
    assert view.dialog.profile is not None and view.state.engine_calls == previous_calls
    assert 'SHA256' in view.dialog.identity.value


def checked(view):
    reviewed(view)
    view.dialog.on_check(); view.worker.drain()
    assert view.dialog.review is not None


def previewed(view, index=0):
    checked(view)
    view.dialog.issues.SetSelection(index); view.dialog.on_issue()
    view.dialog.suggestions.SetSelection(0); view.dialog.on_suggestion()
    view.dialog.on_preview()
    assert view.dialog.preview is not None and view.dialog.buttons['apply'].enabled


def test_profile_review_does_not_execute_engine_and_corrections_are_explicit(view):
    assert view.dialog.profile is None and not view.dialog.buttons['check'].enabled
    before = view.state.document.to_dict()
    previewed(view, 1)
    assert view.state.document.to_dict() == before and view.state.selection == (14, 18)
    assert 'Before: wrld\nAfter: world' == view.dialog.preview_text.value
    view.dialog.on_apply()
    assert view.state.document.text == '😀 hello\r\nwrld world'
    assert view.state.document.source_text == '😀 hello\r\nwrld wrld'
    assert view.state.applied == 1 and view.state.document.revision == 1
    assert view.dialog.review is None and view.dialog.profile is not None
    view.dialog.on_undo()
    assert view.state.undone == 1 and view.state.document.text == view.state.document.source_text


def test_original_selection_scope_survives_issue_navigation(view):
    view.state.selection = (9, 13)
    view.dialog.scope_snapshot = view.capture(); view.dialog.scope.SetSelection(1)
    checked(view)
    assert len(view.dialog.review.result.issues) == 1
    view.dialog.issues.SetSelection(0); view.dialog.on_issue()
    assert view.state.selection == (9, 13)
    view.dialog.replacement.ChangeValue('world'); view.dialog.on_replacement(); view.dialog.on_preview()
    view.dialog.on_apply()
    assert view.state.document.text == '😀 hello\r\nworld wrld'


@pytest.mark.parametrize('change', ['text', 'selection', 'profile', 'scope', 'authorization'])
def test_stale_worker_completion_never_offers_correction(view, change):
    reviewed(view); view.worker.gate.clear()
    view.dialog.on_check()
    if change == 'text': view.state.document.replace(0, 1, 'changed')
    elif change == 'selection': view.state.selection = (9, 13)
    elif change == 'profile': view.dialog.language.ChangeValue('different')
    elif change == 'scope': view.dialog.scope.SetSelection(1)
    else: view.state.authorized = False
    before = view.state.document.to_dict()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.review is None and view.dialog.preview is None
    assert not view.dialog.buttons['apply'].enabled
    assert view.state.document.to_dict() == before


def test_profile_edit_during_hash_cannot_authorize_previous_selection(view):
    view.worker.gate.clear(); view.dialog.on_profile()
    view.dialog.provenance.ChangeValue('changed provenance')
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.profile is None and not view.dialog.buttons['check'].enabled
    assert view.state.engine_calls == 0


def test_cancel_and_late_close_do_not_access_dead_widgets_or_apply_text(view):
    reviewed(view); view.worker.gate.clear(); view.dialog.on_check(); view.dialog.on_cancel()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.review is None and not view.dialog.busy and view.state.applied == 0
    view.worker.gate.clear(); view.dialog.on_check()
    view.dialog.finish_close(); view.dialog.Destroy()
    view.worker.gate.set(); view.worker.drain()
    assert view.state.document.text == view.state.document.source_text


@pytest.mark.parametrize('change', ['replacement', 'selection', 'dictionary'])
def test_stale_apply_does_not_mutate_reading_copy(view, change):
    previewed(view)
    if change == 'replacement': view.dialog.replacement.ChangeValue('different')
    elif change == 'selection': view.state.selection = (0, 0)
    else: view.dialog.dictionary.ChangeValue('different-file.dic')
    before = view.state.document.to_dict()
    view.dialog.on_apply()
    assert view.state.applied == 0 and view.state.document.to_dict() == before


def test_recovery_warning_does_not_erase_committed_correction_or_undo(view):
    previewed(view); view.state.warning = 'Recovery save failed. Use Save As before closing.'
    view.dialog.on_apply()
    assert view.state.applied == 1 and 'Recovery save failed' in view.dialog.status.value
    assert view.dialog.applied is not None
    view.dialog.on_undo()
    assert view.state.undone == 1 and 'Recovery save failed' in view.dialog.status.value


def test_undo_after_unrelated_edit_is_refused(view):
    previewed(view); view.dialog.on_apply()
    view.state.document.replace(0, 1, 'later')
    before = view.state.document.to_dict(); view.dialog.on_undo()
    assert view.state.document.to_dict() == before and view.state.undone == 0


def test_profile_changes_invalidate_result_and_worker_rejection_is_recoverable(view):
    checked(view); view.dialog.on_profile_change()
    assert view.dialog.profile is None and view.dialog.review is None
    view.worker.reject = True; view.dialog.on_profile()
    assert not view.dialog.busy and view.dialog.buttons['review'].enabled
    view.worker.reject = False; reviewed(view)
    assert view.dialog.buttons['check'].enabled
