"""Execute real recording callbacks; these are not hardware or native UI claims."""
import ast
from concurrent.futures import Future
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio.models import AudioCancelled, AudioCleanupPending
from carr_library.audio.recording import RecordingOptions


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection, self.items, self.enabled = value, selection, [], True
    def Enable(self, enabled): self.enabled = enabled
    def GetSelection(self): return self.selection
    def SetSelection(self, selected): self.selection = selected
    def SetItems(self, items): self.items = list(items)
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def GetStringSelection(self): return str(self.value)


class Progress:
    def __init__(self):
        self.ready, self.fail_on, self.holds, self.events = False, None, [], []
        self.closed, self.close_count = False, 0
    def close(self):
        self.closed = True
        self.close_count += 1
    def hold_audio(self, *, recording=False):
        assert recording is True
        if self.fail_on == 'hold':
            raise RuntimeError('Another media owner cannot confirm silence')
        owner = self
        class Hold:
            closed = False
            @property
            def ready(self): return owner.ready
            def close(self): self.closed = True
        hold = Hold()
        self.holds.append(hold)
        return hold
    def start(self, *args, **kwargs):
        if self.fail_on == 'start': raise RuntimeError('Detached progress view')
        return 1
    def update(self, *args, **kwargs): pass
    def succeed(self, *args, **kwargs):
        if self.fail_on == 'succeed': raise RuntimeError('Detached progress view')
        self.events.append('completed')
    def fail(self, *args, **kwargs): self.events.append('failed')
    def cancel(self, *args, **kwargs): self.events.append('cancelled')


class Executor:
    def __init__(self): self.pending, self.shutdown_calls = [], []
    def submit(self, callback):
        future = Future()
        self.pending.append((callback, future))
        return future
    def finish(self):
        callback, future = self.pending.pop(0)
        if not future.running() and not future.set_running_or_notify_cancel():
            return
        try:
            result = callback()
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
    def shutdown(self, *, wait, cancel_futures):
        self.shutdown_calls.append((wait, cancel_futures))
        if cancel_futures:
            for _, future in self.pending:
                future.cancel()


def handlers(values):
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/recording_dialog.py'
    module = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'show_recording')
    names = {'controls', 'release_holds', 'release_completed', 'release_when_stopped', 'poll_cleanup', 'display_record', 'show_records', 'run', 'refresh',
             'start', 'close', 'stop_recording', 'cancel_recording', 'detach', 'on_destroy'}
    definitions = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result + '}\n')
    factory.body[0].body[:0] = definitions
    namespace = {'RecordingOptions': RecordingOptions, 'AudioCancelled': AudioCancelled, 'AudioCleanupPending': AudioCleanupPending, 'time': time, 'threading': threading}
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), namespace)
    return SimpleNamespace(**namespace['build'](**values))


@pytest.fixture
def view():
    progress, parent = Progress(), Progress()
    deferred, captures, executor = [], [], Executor()
    device = SimpleNamespace(id='selected-device', name='Explicit microphone')
    result = {'id': 'retained', 'title': 'My recording', 'created': '2026-11-27T01:00:00Z',
              'path': '/private/retained.wav', 'audio': {'duration_seconds': 1.0, 'sample_rate': 24000, 'channels': 1}}
    def capture(device_id, **kwargs):
        captures.append((device_id, kwargs))
        if kwargs['cancelled'](): raise AudioCancelled('User cancelled before capture')
        kwargs['progress'](1.0)
        return result
    service = SimpleNamespace(devices=Mock(return_value=(device,)), capture=capture)
    buttons = {name: Control() for name in ('refresh', 'start', 'stop', 'cancel', 'preview', 'use', 'stop_preview', 'close')}
    values = dict(busy=False, alive=True, closing=False, generation=0, active_future=None, devices=(), records=(), holds=[],
        cleanup_owner=None, cleanup_future=None, cleanup_retry=None, hold_guard=threading.RLock(), frame=Mock(),
        owner_id=101, parent_id=102, destroyed=False, parent_destroyed=False, parent_progress=parent,
        selected_result=None, controllers=(progress, parent), progress=progress, player=Mock(),
        executor=executor, service=service, cancelled=threading.Event(), stopped=threading.Event(),
        microphone=Control(selection=-1), title=Control('My recording'), minutes=Control(2),
        sample_rate=Control('24000'), channels=Control(selection=0), saved=Control(selection=-1),
        buttons=buttons, details=Control(), status=Control(), dialog=Mock(),
        wx=SimpleNamespace(NOT_FOUND=-1, ID_CANCEL=2, CallAfter=lambda callback: callback(),
                           CallLater=lambda delay, callback: deferred.append(callback)))
    return SimpleNamespace(handlers=handlers(values), values=values, progress=progress, parent=parent,
        deferred=deferred, captures=captures, executor=executor, service=service, result=result)


def discovered(view):
    view.handlers.refresh()
    view.executor.finish()
    view.values['microphone'].SetSelection(0)


def test_discovery_never_records_or_automatically_selects_even_a_single_microphone(view):
    view.handlers.start()
    assert view.captures == [] and view.executor.pending == []
    view.handlers.refresh()
    assert view.service.devices.call_count == 0 and view.captures == []
    view.executor.finish()
    assert view.values['microphone'].GetSelection() == -1
    assert view.values['microphone'].items == ['Explicit microphone (selected)']
    view.handlers.start()
    assert view.captures == [] and view.executor.pending == []


def test_start_waits_for_recording_silence_then_passes_explicit_device_and_options(view):
    discovered(view)
    view.handlers.start()
    assert view.captures == [] and view.executor.pending == []
    assert len(view.progress.holds) == len(view.parent.holds) == 1
    view.progress.ready = view.parent.ready = True
    view.deferred.pop(0)()
    assert view.captures == []
    view.handlers.stop_recording()
    view.executor.finish()
    device_id, options = view.captures[0]
    assert device_id == 'selected-device'
    assert options['title'] == 'My recording'
    assert options['options'].max_seconds == 120 and options['options'].sample_rate == 24000
    assert options['stop_requested']() is True and options['cancelled']() is False
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    assert view.values['status'].value.startswith('Saved and verified.')


@pytest.mark.parametrize('action', ['stop_recording', 'cancel_recording'])
def test_stop_or_cancel_while_waiting_for_silence_never_opens_microphone(view, action):
    discovered(view)
    view.handlers.start()
    getattr(view.handlers, action)()
    view.deferred.pop(0)()
    assert view.captures == [] and view.executor.pending == []
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    assert view.values['buttons']['refresh'].enabled


def test_close_active_capture_waits_for_cleanup_without_releasing_recording_hold(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    view.handlers.start()
    event = Mock()
    view.handlers.close(event)
    event.Veto.assert_called_once()
    assert view.values['cancelled'].is_set() and not view.progress.holds[0].closed
    view.values['dialog'].EndModal.assert_not_called()
    view.executor.finish()
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    view.values['dialog'].EndModal.assert_called_once_with(2)
    assert view.progress.events[-1] == 'cancelled'


def test_partial_silence_admission_failure_releases_existing_holds_and_does_not_record(view):
    discovered(view)
    view.parent.fail_on = 'hold'
    view.handlers.start()
    assert view.progress.holds[0].closed
    assert view.captures == [] and view.executor.pending == []
    assert 'cannot confirm silence' in view.values['status'].value


def test_exceptional_modal_exit_retains_audio_hold_until_capture_future_completes(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    view.handlers.start()
    view.values['cancelled'].set()
    view.handlers.release_when_stopped()
    assert not view.progress.holds[0].closed and not view.parent.holds[0].closed
    view.executor.finish()
    assert view.progress.holds[0].closed and view.parent.holds[0].closed


@pytest.mark.parametrize('failure', ['start', 'succeed'])
def test_progress_observer_failure_cannot_turn_retained_recording_into_failed_work(view, failure):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    view.progress.fail_on = failure
    view.handlers.start()
    view.executor.finish()
    assert view.values['status'].value.startswith('Saved and verified.')
    assert view.values['saved'].items == ['My recording (2026-11-27T01:00:00)']
    assert len(view.captures) == 1 and view.progress.holds[0].closed
    assert 'Detached progress view' in view.progress.reporting_error


class PendingOwner:
    def __init__(self):
        self.quiescent_event = threading.Event()
        self.callbacks = []
        self.retries = 0
    @property
    def quiescent(self):
        return self.quiescent_event.is_set()
    def add_quiescent_callback(self, callback):
        if self.quiescent:
            callback()
        else:
            self.callbacks.append(callback)
    def close(self):
        self.retries += 1
        return self.quiescent
    def confirm(self):
        self.quiescent_event.set()
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def test_detached_waiting_prepare_never_touches_destroyed_controls_or_opens_capture(view):
    discovered(view)
    view.handlers.start()
    view.handlers.detach()
    for value in view.values.values():
        if isinstance(value, Control):
            value.Enable = value.SetValue = lambda *a: pytest.fail('Detached native control accessed')
    view.handlers.release_when_stopped()
    for callback in tuple(view.deferred):
        callback()
    assert view.captures == [] and view.executor.pending == []
    assert view.progress.holds[0].closed and view.parent.holds[0].closed


def test_pending_native_cleanup_retains_holds_after_future_and_modal_completion(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    owner = PendingOwner()
    def pending(*args, **kwargs):
        raise AudioCleanupPending('Native cleanup is not confirmed', cleanup_owner=owner)
    view.service.capture = pending
    view.handlers.start()
    view.executor.finish()
    assert not view.progress.holds[0].closed and not view.parent.holds[0].closed
    assert 'not confirmed silence' in view.values['status'].value
    assert not view.values['buttons']['start'].enabled
    view.handlers.detach()
    view.handlers.release_when_stopped()
    for value in view.values.values():
        if isinstance(value, Control):
            value.Enable = value.SetValue = lambda *a: pytest.fail('Detached native control accessed')
    for callback in tuple(view.deferred):
        callback()
    assert not view.progress.holds[0].closed
    owner.confirm()
    assert view.progress.holds[0].closed and view.parent.holds[0].closed


def test_cleanup_retry_start_failure_keeps_existing_owner_and_quiet_holds(view, monkeypatch):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    owner = PendingOwner()
    def pending(*args, **kwargs):
        raise AudioCleanupPending('Native cleanup is not confirmed', cleanup_owner=owner)
    view.service.capture = pending
    view.handlers.start()
    view.executor.finish()
    def no_thread(self):
        raise RuntimeError('Synthetic retry thread unavailable')
    monkeypatch.setattr(threading.Thread, 'start', no_thread)
    view.handlers.stop_recording()
    assert 'remain retained' in view.values['status'].value
    assert not view.progress.holds[0].closed and not view.parent.holds[0].closed
    owner.confirm()
    assert view.progress.holds[0].closed and view.parent.holds[0].closed


def destroyed_controls(view):
    def dead(*args, **kwargs): pytest.fail('Destroyed recording native control accessed')
    for value in (*view.values.values(), *view.values['buttons'].values()):
        if isinstance(value, Control):
            value.Enable = value.SetValue = value.SetItems = value.SetSelection = value.GetSelection = dead
    view.values['dialog'].EndModal = view.values['dialog'].Destroy = view.values['dialog'].IsModal = dead
    return dead


def destroy(view, target):
    identity = view.values['owner_id' if target == 'dialog' else 'parent_id']
    event = SimpleNamespace(GetId=lambda: identity, Skip=Mock(),
        GetEventObject=lambda: pytest.fail('The dying recording wrapper must not be inspected'))
    view.handlers.on_destroy(event)
    event.Skip.assert_called_once()


@pytest.mark.parametrize('target', ['dialog', 'parent'])
def test_direct_destroy_while_waiting_for_silence_never_opens_microphone(view, target):
    discovered(view)
    view.handlers.start()
    assert view.deferred and not view.executor.pending
    destroy(view, target)
    destroyed_controls(view)
    for callback in tuple(view.deferred):
        callback()
    view.handlers.controls()
    view.handlers.close()
    assert not view.captures and not view.executor.pending
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    assert view.values['cancelled'].is_set() and view.progress.closed
    assert view.parent.closed == (target == 'parent')
    view.handlers.detach()
    assert view.executor.shutdown_calls == [(False, True)] and view.progress.close_count == 1


@pytest.mark.parametrize('target', ['dialog', 'parent'])
def test_direct_destroy_cancels_queued_capture_without_native_completion(view, target):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    view.handlers.start()
    _, future = view.executor.pending[0]
    destroy(view, target)
    destroyed_controls(view)
    assert future.cancelled()
    view.executor.finish()
    assert not view.captures
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    assert view.executor.shutdown_calls == [(False, True)]


@pytest.mark.parametrize('target', ['dialog', 'parent'])
def test_direct_destroy_discards_already_queued_recording_completion(view, target):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    queued = []
    view.values['wx'].CallAfter = queued.append
    view.handlers.start()
    view.executor.finish()
    assert len(view.captures) == 1 and len(queued) == 1
    destroy(view, target)
    destroyed_controls(view)
    queued.pop()()
    assert view.values['saved'].items == []
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    assert view.executor.shutdown_calls == [(False, True)]


@pytest.mark.parametrize('target', ['dialog', 'parent'])
def test_destroyed_owner_keeps_running_capture_holds_until_actual_cleanup_confirmation(view, target):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    view.handlers.start()
    _, future = view.executor.pending[0]
    assert future.set_running_or_notify_cancel()
    destroy(view, target)
    destroyed_controls(view)
    assert not future.cancelled()
    assert not view.progress.holds[0].closed and not view.parent.holds[0].closed
    owner = PendingOwner()
    future.set_exception(AudioCleanupPending('Owned capture still running', cleanup_owner=owner))
    assert not view.progress.holds[0].closed and not view.parent.holds[0].closed
    owner.confirm()
    assert view.progress.holds[0].closed and view.parent.holds[0].closed


def test_child_destroy_does_not_detach_live_recording_owner(view):
    event = SimpleNamespace(GetId=lambda: 999, Skip=Mock())
    view.handlers.on_destroy(event)
    event.Skip.assert_called_once()
    assert not view.progress.closed and not view.parent.closed
    assert not view.values['cancelled'].is_set() and not view.executor.shutdown_calls
    discovered(view)
    assert len(view.values['microphone'].items) == 1
