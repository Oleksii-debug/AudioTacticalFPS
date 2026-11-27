"""Real executor, current controller and native-lifetime speech-preview contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from queue import Empty, Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from carr_library.reader.narration import NarrationStore, encode_project, new_project
from carr_library.reader.speech_normalization import SpeechOptions
from carr_library.ui.narration_editor import NarrationController
from carr_library.ui import speech_preparation as ui
from tests.spelling._fixtures import ReaderWorker, Sizer, Widget


class CheckBox(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, value=False, **kwargs)


def native(wx_dialog=Widget, dispatch=lambda action, *args: action(*args)):
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
        'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE',
        'EVT_CHOICE', 'EVT_CHECKBOX', 'ID_CANCEL', 'EVT_WINDOW_DESTROY')
    return SimpleNamespace(**{key: index + 1 for index, key in enumerate(constants)},
        Dialog=wx_dialog, ScrolledWindow=Widget, StaticText=Widget, TextCtrl=Widget,
        Choice=Widget, CheckBox=CheckBox, Button=Widget, BoxSizer=Sizer, CallAfter=dispatch)


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
    state = SimpleNamespace(document=NarrationController(NarrationStore(root),
        new_project('😀 retained\r\nCount 12 kg; 5.', title='Speech preparation fixture')),
        selection=(0, 0), authorized=True, applied=0, undone=0, save_failure=False)
    def guard():
        if not state.authorized:
            raise ValueError('The source authorization changed.')
    state.document.guard = guard
    def capture():
        return ui.capture_speech(state.document, state.selection)
    def apply(proposal):
        ui.apply_speech(state.document, proposal, capture())
        state.selection = (proposal.bounds[0], proposal.bounds[0] + len(proposal.result.text))
        state.applied += 1
        accepted = capture()
        if state.save_failure:
            return accepted, 'The change remains in this reading copy; recovery saving failed.'
        return accepted, state.document.autosave()
    def undo(applied):
        ui.undo_speech(state.document, applied, capture())
        state.selection = (0, 0)
        state.undone += 1
        return state.document.autosave()
    cancel = Event()
    worker = ReaderWorker(cancel)
    parent, wx = Widget(), native()
    dialog = ui.create_speech_preparation(parent, capture=capture, apply=apply, undo=undo,
        guard=guard, run=worker, cancel=cancel, wx_module=wx)
    result = SimpleNamespace(root=root, state=state, cancel=cancel, worker=worker, parent=parent,
        wx=wx, dialog=dialog, capture=capture, apply=apply, undo=undo, guard=guard)
    try:
        yield result
    finally:
        worker.close()
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
    before = encode_project(view.state.document.project)
    view.dialog.on_preview()
    view.worker.drain()
    assert view.dialog.proposal and not view.dialog.proposal.result.changed
    assert not view.dialog.buttons['apply'].enabled
    view.dialog.on_apply()
    assert not view.state.applied and encode_project(view.state.document.project) == before
    assert not (view.root / 'narration-projects').exists()


def test_explicit_profile_and_selected_scope_apply_and_undo_current_history(view):
    source = view.state.document.pending_text
    start = source.index('12')
    view.state.selection = (start, start + len('12 kg'))
    view.dialog.scope.SetSelection(1)
    review(view)
    assert view.dialog.before.value == '12 kg'
    assert view.dialog.after.value == 'twelve kilograms'
    assert view.state.document.pending_text == source and not view.state.applied
    view.dialog.on_apply()
    assert view.state.document.pending_text == source[:start] + 'twelve kilograms' + source[start + 5:]
    assert view.state.document.project.original_text == source and view.state.applied == 1
    saved, _ = view.state.document.store.load(view.state.document.project.id, draft=True)
    assert saved.current_text == view.state.document.pending_text
    view.dialog.on_undo()
    assert view.state.document.pending_text == source and view.state.undone == 1
    assert len(view.state.document.project.revisions) == 2
    assert view.worker.worker_threads and all(thread != get_ident() for thread in view.worker.worker_threads)


@pytest.mark.parametrize('changed', ['text', 'selection', 'revision', 'options', 'authorization'])
def test_late_preview_never_exposes_a_stale_apply(view, changed):
    configure(view.dialog)
    view.worker.gate.clear()
    view.dialog.on_preview()
    if changed == 'text':
        view.state.document.set_text('An independent edit')
    elif changed == 'selection':
        view.state.selection = (1, 2)
    elif changed == 'revision':
        view.state.document.project = replace(view.state.document.project, title='Other title')
    elif changed == 'options':
        view.dialog.locale.SetSelection(1)
    else:
        view.state.authorized = False
    before = encode_project(view.state.document.project)
    view.worker.gate.set()
    view.worker.drain()
    assert view.dialog.proposal is None and not view.state.applied
    assert encode_project(view.state.document.project) == before


@pytest.mark.parametrize('changed', ['text', 'selection', 'revision', 'options', 'authorization'])
def test_current_identity_and_options_are_rechecked_at_apply(view, changed):
    review(view)
    if changed == 'text':
        view.state.document.set_text('Another explicit edit')
    elif changed == 'selection':
        view.state.selection = (0, 1)
    elif changed == 'revision':
        view.state.document.project = replace(view.state.document.project, title='Renamed')
    elif changed == 'options':
        view.dialog.expansions['numbers'].SetValue(False)
    else:
        view.state.authorized = False
    before = view.state.document.pending_text
    view.dialog.on_apply()
    assert not view.state.applied and view.state.document.pending_text == before


def test_recovery_failure_does_not_discard_an_accepted_reading_edit(view):
    review(view)
    view.state.save_failure = True
    view.dialog.on_apply()
    assert view.state.applied == 1 and view.dialog.applied is not None
    assert 'recovery saving failed' in view.dialog.status.value
    assert 'twelve kilograms' in view.state.document.pending_text
    view.dialog.on_undo()
    assert view.state.undone == 1


def test_unrelated_later_edit_cannot_be_undone_as_this_preparation(view):
    review(view)
    view.dialog.on_apply()
    view.state.document.apply('Independent revision', label='Separate explicit edit')
    view.dialog.on_undo()
    assert view.state.document.pending_text == 'Independent revision' and not view.state.undone


def test_locale_is_required_and_operation_rejection_or_cancel_has_no_edit(view):
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
def test_real_worker_after_forced_destruction_never_touches_native_controls_or_text(view, phase, mode):
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


def test_failed_native_construction_disposes_partial_controls(view):
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


def test_close_veto_waits_for_actual_existing_session_worker_settlement(tmp_path, monkeypatch):
    callbacks, owner = Queue(), get_ident()
    started, release = Event(), Event()
    controller = NarrationController(NarrationStore(tmp_path), new_project('12 kg'))
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), cancel_event=Event(), working=False, _closing=False)
    futures, captured = [], {}
    def dispatch(action, *args):
        callbacks.put((action, args))
    class Modal(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.modal = False
        def ShowModal(self):
            self.native()
            self.modal = True
            while self.modal:
                action, args = callbacks.get(timeout=10)
                action(*args)
            return 1
        def IsModal(self):
            self.native()
            return self.modal
        def EndModal(self, result):
            self.native()
            self.modal = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        def run_job(title, operation, done, failed, **kwargs):
            assert get_ident() == owner and not frame.working
            frame.working = True
            future = executor.submit(operation)
            futures.append(future)
            def arrived(future):
                def settle():
                    frame.working = False
                    try:
                        done(future.result())
                    except Exception as exc:
                        failed(exc)
                dispatch(settle)
            future.add_done_callback(arrived)
            return True
        frame.run_job = run_job
        actual = ui.prepare_selection
        def held(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return actual(*args, **kwargs)
        monkeypatch.setattr(ui, 'prepare_selection', held)
        def ready(dialog, session):
            captured.update(dialog=dialog, session=session)
            configure(dialog)
            dialog.on_preview()
            assert started.wait(3)
            veto = []
            dialog.on_close(SimpleNamespace(CanVeto=lambda: True, Veto=lambda: veto.append(True)))
            assert veto == [True] and dialog.alive and dialog.IsModal()
            assert session.busy and session.close_requested and not session.closed
            release.set()
        try:
            ui.show_speech_preparation(None, frame=frame, library_root=tmp_path,
                capture=lambda: ui.capture_speech(controller, (0, 0)),
                apply=lambda *_: pytest.fail('Close applied text'), undo=lambda *_: pytest.fail('Close undid text'),
                guard=lambda: None, wx_module=native(Modal, dispatch), on_ready=ready)
            assert captured['session'].closed and captured['dialog'].destroyed
            assert controller.pending_text == '12 kg'
        finally:
            release.set()
            for future in futures:
                future.result(timeout=5)
            while True:
                try:
                    action, args = callbacks.get_nowait()
                except Empty:
                    break
                action(*args)
    assert all(future.done() for future in futures) and not frame.working


def test_current_normalized_revision_and_undo_survive_complete_backup(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Fixture')
    store = NarrationStore(app.root)
    controller = NarrationController(store, new_project('Original 12 kg\r\n', title='Separate copy'))
    snapshot = ui.capture_speech(controller, (0, len(controller.pending_text)))
    proposal = ui.prepare_selection(snapshot, snapshot.selection,
        SpeechOptions(mode='speech', locale='en-US', numbers=True, units=True))
    ui.apply_speech(controller, proposal, snapshot)
    controller.save()
    before = encode_project(controller.project)
    digest = create_backup(app, tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', digest)
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    saved, pin = NarrationStore(restored).load(controller.project.id)
    assert encode_project(saved) == before
    reopened = NarrationController(NarrationStore(restored), saved, expected_digest=pin)
    applied = ui.capture_speech(reopened, (0, 0))
    ui.undo_speech(reopened, applied, applied)
    assert reopened.pending_text == 'Original 12 kg\r\n'
    assert reopened.project.original_text == 'Original 12 kg\r\n'
