"""Real shared-worker actions with synthetic provider replies; no GUI/acoustics claim."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui.transcription_remote_dialog import RemoteTranscriptionActions

_spec = importlib.util.spec_from_file_location('remote_stt_ui_fixture',
    Path(__file__).parents[1] / 'transcription' / 'remote_fixture.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection, self.enabled, self.focused = value, selection, True, False
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def Enable(self, value=True): self.enabled = value
    def SetFocus(self): self.focused = True


@pytest.fixture
def view(tmp_path, monkeypatch):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    executor = ThreadPoolExecutor(max_workers=1)
    gate = Event()
    gate.set()
    jobs = []
    frame = SimpleNamespace(library=owner.app, active=(owner.workspace, 'Selected library'),
        working=False, _closing=False, cancel_event=Event(), executor=executor)
    def run_job(label, work, *, done, failed):
        if frame.working:
            return None
        frame.working = True
        frame.cancel_event.clear()
        def queued():
            assert gate.wait(30)
            return work()
        future = executor.submit(queued)
        jobs.append(SimpleNamespace(label=label, future=future, done=done, failed=failed))
        return future
    frame.run_job = run_job
    actions = RemoteTranscriptionActions(frame, owner.service, on_job=Mock())
    actions.provider = Control(selection=0)
    actions.file = Control(str(owner.selected))
    actions.language, actions.cost = Control('auto'), Control('0.50')
    actions.consent = Control(False)
    actions.preview, actions.status = Control(), Control('Initial state')
    for name in ('review_button', 'send_button', 'stop_button', 'close_button'):
        setattr(actions, name, Control())
    actions.mutable = [actions.provider, actions.file, actions.language, actions.cost, actions.consent]
    actions.confirm, actions.finish_close = Mock(return_value=False), Mock()
    actions.controls()
    actions.owner, actions.calls, actions.jobs, actions.gate = owner, calls, jobs, gate
    try:
        yield actions
    finally:
        actions.close(force=True)
        gate.set()
        executor.shutdown(wait=True, cancel_futures=False)


def finish(view):
    row = view.jobs[-1]
    try:
        value = row.future.result(timeout=30)
    except BaseException as error:
        view.frame.working = False
        row.failed(error)
        return error
    view.frame.working = False
    row.done(value)
    return value


def review(view):
    assert view.review() is True
    result = finish(view)
    assert isinstance(result, dict) and view.reviewed is not None
    return result


def test_review_is_local_shows_exact_bytes_destination_and_local_budget(view):
    value = review(view)
    assert not view.calls and not fixture.Connection.requests
    assert value['audio']['sha256'] in view.preview.value
    assert value['source_path'] in view.preview.value
    assert 'https://api.elevenlabs.io/v1/speech-to-text' in view.preview.value
    assert 'provider-enforced billing cap' in view.preview.value
    assert 'normal service retention' in view.preview.value
    assert not view.consent.value and not view.send_button.enabled
    assert not view.owner.service.jobs_root.exists()


def test_unchecked_consent_refuses_even_after_local_review(view):
    review(view)
    before = len(view.jobs)
    assert view.send() is False
    assert len(view.jobs) == before and not view.calls
    view.confirm.assert_not_called()
    assert not view.owner.service.jobs_root.exists()


@pytest.mark.parametrize('answer', [False, True])
def test_explicit_default_no_or_yes_uses_one_existing_worker_and_retained_receipt(view, answer):
    review(view)
    view.consent.SetValue(True)
    view.controls()
    assert view.send_button.enabled
    view.confirm.return_value = answer
    admitted = view.send()
    assert admitted is answer
    if answer:
        result = finish(view)
        assert result['status'] == 'completed' and len(view.calls) == 1
        assert view.last_result == result
        assert view.last_job == result['id']
        assert not view.consent.value and view.reviewed is None
        view.on_job.assert_called_once_with(result['id'])
        assert view.owner.service.verify(result['id'])['receipts'] == result['receipts']
    else:
        assert len(view.jobs) == 1 and not view.calls
        assert not view.owner.service.jobs_root.exists()


@pytest.mark.parametrize('changed', ['file', 'language', 'cost', 'profile'])
def test_current_inputs_cannot_reuse_an_earlier_transfer_review(view, changed):
    review(view)
    view.consent.SetValue(True)
    if changed == 'profile': view.provider.SetSelection(-1)
    else: getattr(view, changed).SetValue('Changed since review')
    before = len(view.jobs)
    assert view.send() is False
    assert len(view.jobs) == before and not view.calls
    view.confirm.assert_not_called()


@pytest.mark.parametrize('changed', ['workspace', 'recording'])
def test_modal_confirmation_rechecks_current_view_and_selected_recording(view, changed):
    review(view)
    view.consent.SetValue(True)
    def change(reviewed):
        if changed == 'workspace':
            view.frame.active = ('another-workspace', 'Changed')
        else:
            view.file.SetValue('different recording.wav')
        return True
    view.confirm.side_effect = change
    assert view.send() is False
    assert len(view.jobs) == 1 and not view.calls
    assert not view.owner.service.jobs_root.exists()


def test_queued_close_cancels_before_source_retention_or_remote_admission(view):
    review(view)
    view.consent.SetValue(True)
    view.confirm.return_value = True
    view.gate.clear()
    assert view.send() is True
    before = view.status.value
    view.close(force=True)
    view.gate.set()
    error = finish(view)
    assert isinstance(error, InterruptedError)
    assert not view.calls and not view.owner.service.jobs_root.exists()
    assert view.status.value == before
    assert view.last_result is None
    view.on_job.assert_not_called()


def test_busy_close_requests_stop_and_keeps_host_ownership_until_settlement(view):
    review(view)
    view.consent.SetValue(True)
    view.confirm.return_value = True
    view.gate.clear()
    assert view.send() is True
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    assert view.close(event) is False and view.cancel.is_set()
    assert view.alive and view.busy
    event.Veto.assert_called_once()
    view.finish_close.assert_not_called()
    view.gate.set()
    assert isinstance(finish(view), InterruptedError)
    assert not view.busy and not view.calls
    assert view.close() is True
    view.finish_close.assert_called_once()


def test_late_completed_receipt_is_retained_without_touching_a_replaced_view(view):
    review(view)
    view.consent.SetValue(True)
    view.confirm.return_value = True
    assert view.send() is True
    result = view.jobs[-1].future.result(timeout=30)
    before = (view.status.value, view.preview.value)
    view.frame.library = SimpleNamespace(root=view.root)
    finish(view)
    assert view.last_result == result
    assert (view.status.value, view.preview.value) == before
    view.on_job.assert_not_called()
    assert len(view.calls) == 1


def test_actual_same_path_library_change_refuses_queued_remote_work_without_canary_writes(view):
    review(view)
    view.consent.SetValue(True)
    view.confirm.return_value = True
    view.gate.clear()
    assert view.send() is True
    original = view.owner.app.root
    moved = original.with_name('original-moved')
    original.rename(moved)
    original.mkdir()
    canary = original / 'canary'
    canary.write_bytes(b'replacement stays unchanged')
    try:
        view.gate.set()
        assert isinstance(finish(view), InterruptedError)
        assert not view.calls and list(original.iterdir()) == [canary]
        assert canary.read_bytes() == b'replacement stays unchanged'
    finally:
        canary.unlink()
        original.rmdir()
        moved.rename(original)


@pytest.mark.parametrize('refused', [None, False])
def test_host_admission_refusal_never_leaves_a_busy_remote_child(view, refused):
    view.frame.run_job = Mock(return_value=refused)
    assert view.review() is False
    assert not view.busy and not view.calls
    assert view.future is None


def test_repeated_close_never_repeats_the_modal_finish(view):
    assert view.close() is True
    assert view.close() is True
    view.finish_close.assert_called_once()


def test_native_parent_close_waits_for_actual_refresh_and_enabled_modal_control():
    # Execute the actual native generator without importing wx or substituting
    # any product operation. A returned child starts the existing parent read.
    import ast

    source = Path(__file__).parents[2] / 'scripts' / 'windows_transcription_remote_smoke.py'
    module = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
    mains = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    assert len(mains) == 1
    helpers = [node for node in mains[0].body
               if isinstance(node, ast.FunctionDef) and node.name == 'close_parent']
    assert len(helpers) == 1
    events = []
    frame = SimpleNamespace(working=True)
    parent = SimpleNamespace(modal=True, live=True)
    parent.IsModal = lambda: parent.modal
    parent.GetHandle = lambda: 73
    button = SimpleNamespace(enabled=False)
    button.IsEnabled = lambda: button.enabled
    def named(window, name):
        assert window is parent and name == 'Закрити'
        return button
    def key(control):
        assert control is button and button.enabled and parent.modal and not frame.working
        events.append('actual keyboard helper admitted')
        yield (lambda: not parent.live, 'controlled keyboard release', 5)
    scope = dict(frame=frame, named=named, key=key,
                 user32=SimpleNamespace(IsWindow=lambda handle: handle == 73 and parent.live),
                 wait_for=lambda predicate, message, seconds=45: (predicate, message, seconds))
    exec(compile(ast.Module(body=helpers, type_ignores=[]), str(source), 'exec'), scope)
    iterator = scope['close_parent'](parent)
    pending = next(iterator)
    assert pending[2] == 45 and not pending[0]() and not events
    frame.working = False
    assert not pending[0]() and not events
    button.enabled = True
    parent.modal = False
    assert not pending[0]() and not events
    parent.modal = True
    assert pending[0]() and not events
    key_wait = next(iterator)
    assert events == ['actual keyboard helper admitted'] and not key_wait[0]()
    parent.live = False
    assert key_wait[0]()
    close_wait = next(iterator)
    assert close_wait[0]()
    with pytest.raises(StopIteration):
        next(iterator)
