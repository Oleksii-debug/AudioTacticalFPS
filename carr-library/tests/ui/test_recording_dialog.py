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
from carr_library.audio.recording import RecordingOptions, RecordingState
from carr_library.audio.output_devices import OutputDevice


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
        self.event = SimpleNamespace(state='idle', token=1)
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
        self.event.state = 'running'
        return 1
    def pause(self, *args, **kwargs):
        self.event.state = 'paused'
    def resume(self, *args, **kwargs):
        self.event.state = 'running'
    def update(self, *args, **kwargs): pass
    def succeed(self, *args, **kwargs):
        if self.fail_on == 'succeed': raise RuntimeError('Detached progress view')
        self.events.append('completed')
        self.event.state = 'succeeded'
    def fail(self, *args, **kwargs): self.events.append('failed')
    def cancel(self, *args, **kwargs): self.events.append('cancelled')


class Executor:
    def __init__(self): self.pending = []
    def submit(self, callback):
        future = Future()
        self.pending.append((callback, future))
        return future
    def finish(self):
        callback, future = self.pending.pop(0)
        try:
            result = callback()
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)


def handlers(values):
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/recording_dialog.py'
    module = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'show_recording')
    names = {'controls', 'release_holds', 'release_completed', 'release_when_stopped', 'poll_cleanup', 'display_record', 'show_records', 'run', 'refresh',
             'start', 'close', 'stop_recording', 'cancel_recording', 'detach', 'on_destroy',
             'report', 'preview', 'use', 'stop_playback', 'refresh_monitor', 'pause_recording', 'resume_recording'}
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
    service = SimpleNamespace(devices=Mock(return_value=(device,)), capture=capture,
                              monitoring_outputs=Mock(return_value=()))
    buttons = {name: Control() for name in ('refresh', 'refresh_monitor', 'start', 'stop', 'cancel', 'pause', 'resume', 'preview', 'use', 'stop_preview', 'close')}
    values = dict(native_id=71, busy=False, alive=True, closing=False, generation=0, active_future=None, devices=(), records=(), holds=[],
        cleanup_owner=None, cleanup_future=None, cleanup_retry=None, hold_guard=threading.RLock(), frame=Mock(),
        monitor_outputs=(), capture_phase='idle', current_token=None, paused=threading.Event(),
        monitoring=Control(False), monitor_output=Control(selection=-1), library=object(),
        selected_result=None, on_selected=None, controllers=(progress, parent), progress=progress, player=Mock(),
        executor=executor, service=service, cancelled=threading.Event(), stopped=threading.Event(),
        microphone=Control(selection=-1), title=Control('My recording'), minutes=Control(2),
        sample_rate=Control('24000'), channels=Control(selection=0), saved=Control(selection=-1),
        buttons=buttons, details=Control(), status=Control(), dialog=Mock(),
        wx=SimpleNamespace(NOT_FOUND=-1, ID_CANCEL=2, CallAfter=lambda callback: callback(),
                           CallLater=lambda delay, callback: deferred.append(callback)))
    values['frame'].library = values['library']
    return SimpleNamespace(handlers=handlers(values), values=values, progress=progress, parent=parent,
        deferred=deferred, captures=captures, executor=executor, service=service, result=result)


def discovered(view):
    view.handlers.refresh()
    view.executor.finish()
    view.values['microphone'].SetSelection(0)


def test_native_destruction_ignores_children_and_discards_pending_delivery(view):
    view.handlers.refresh()
    child = Mock()
    child.GetId.return_value = 72
    view.handlers.on_destroy(child)
    assert not view.values['cancelled'].is_set()
    own = Mock()
    own.GetId.return_value = 71
    view.handlers.on_destroy(own)
    assert view.values['cancelled'].is_set()
    before = view.values['status'].value, tuple(view.values['microphone'].items)
    view.executor.finish()
    assert before == (view.values['status'].value, tuple(view.values['microphone'].items))
    assert not view.captures
    child.Skip.assert_called_once()
    own.Skip.assert_called_once()


def test_recording_destroy_and_finalizer_tolerate_deleted_native_wrapper():
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/recording_dialog.py'
    window = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                  if isinstance(node, ast.FunctionDef) and node.name == 'show_recording')
    view_class = next(node for node in window.body if isinstance(node, ast.ClassDef))
    class DeletedBase:
        def __bool__(self): return False
        def Destroy(self): pytest.fail('Dead native recording wrapper was destroyed twice')
    namespace = {'wx': SimpleNamespace(Dialog=DeletedBase)}
    exec(compile(ast.Module(body=[view_class], type_ignores=[]), str(source), 'exec'), namespace)
    deleted = namespace['RecordingDialog']()
    deleted.detach_callbacks = Mock()
    assert deleted.Destroy() is False
    deleted.detach_callbacks.assert_called_once()
    # Execute the complete production finalizer after native parent deletion.
    final = next(node for node in reversed(window.body) if isinstance(node, ast.Try) and node.finalbody)
    cleanup = ast.Module(body=final.finalbody, type_ignores=[])
    environment = dict(detach=Mock(), executor=Mock(), player=Mock(), frame=Mock(),
                       controllers=(), release_when_stopped=Mock(), progress=Mock(), dialog=deleted)
    exec(compile(cleanup, str(source), 'exec'), environment)
    environment['detach'].assert_called_once()
    environment['player'].close.assert_called_once()
    environment['release_when_stopped'].assert_called_once()
    environment['progress'].close.assert_called_once()


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


def poison_controls(view):
    def deleted(*args, **kwargs):
        raise AssertionError('A deleted native control was accessed')
    for key in ('microphone', 'title', 'minutes', 'sample_rate', 'channels', 'saved', 'details', 'status'):
        control = view.values[key]
        for name in ('Enable', 'GetSelection', 'SetSelection', 'SetItems', 'GetValue', 'SetValue',
                     'GetStringSelection'):
            setattr(control, name, deleted)
    for control in view.values['buttons'].values():
        control.Enable = deleted
    view.values['dialog'].EndModal.side_effect = deleted


def test_matching_native_destroy_event_cancels_pending_capture_and_late_completion_is_inert(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    view.handlers.start()
    event = Mock()
    event.GetId.return_value = view.values['native_id']
    event.GetEventObject.return_value = object()  # A rewrapped matching event.
    view.handlers.on_destroy(event)
    assert view.values['cancelled'].is_set() and view.values['stopped'].is_set()
    assert view.values['dialog'].recording_alive is False
    assert not view.progress.holds[0].closed
    view.handlers.release_when_stopped()
    poison_controls(view)
    view.executor.finish()
    for name in ('refresh', 'start', 'preview', 'use', 'close', 'stop_playback',
                 'stop_recording', 'cancel_recording', 'controls', 'display_record'):
        getattr(view.handlers, name)()
    view.handlers.report('Late progress')
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    view.values['dialog'].EndModal.assert_not_called()
    event.Skip.assert_called_once()


def test_unrelated_child_destroy_event_does_not_cancel_recording_selection(view):
    event = Mock()
    event.GetId.return_value = view.values['native_id'] + 1
    view.handlers.on_destroy(event)
    assert not view.values['cancelled'].is_set()
    discovered(view)
    assert view.values['microphone'].GetSelection() == 0
    assert view.values['microphone'].items
    event.Skip.assert_called_once()


def test_destroy_while_waiting_for_silence_prevents_capture_and_controls(view):
    discovered(view)
    view.handlers.start()
    assert view.deferred
    view.handlers.detach()
    view.handlers.release_when_stopped()
    poison_controls(view)
    view.deferred.pop(0)()
    assert not view.captures and not view.executor.pending
    assert view.progress.holds[0].closed and view.parent.holds[0].closed


def test_pause_and_resume_require_acknowledged_microphone_close_and_keep_quiet_holds(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    def capture(device_id, **options):
        options['state_changed'](RecordingState('recording', 2400, 24000, 0))
        assert view.values['buttons']['pause'].enabled
        view.handlers.pause_recording()
        assert options['pause_requested']() and view.progress.event.state == 'running'
        assert not view.values['buttons']['resume'].enabled
        options['state_changed'](RecordingState('pausing', 2400, 24000, 0))
        assert view.progress.event.state == 'running'
        options['state_changed'](RecordingState('paused', 3000, 24000, 1))
        assert view.progress.event.state == 'paused'
        assert view.values['buttons']['resume'].enabled
        assert all(not hold.closed for hold in (*view.progress.holds, *view.parent.holds))
        view.handlers.resume_recording()
        assert not options['pause_requested']() and view.progress.event.state == 'paused'
        options['state_changed'](RecordingState('opening', 3000, 24000, 1))
        assert view.progress.event.state == 'running'
        options['state_changed'](RecordingState('recording', 6000, 24000, 1))
        return view.result
    view.service.capture = capture
    view.handlers.start()
    view.executor.finish()
    assert view.progress.events == ['completed']
    assert view.values['dialog'].capture_state == 'idle'


def test_stop_while_paused_finishes_without_accepting_resume(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    def capture(device_id, **options):
        options['state_changed'](RecordingState('recording', 2400, 24000, 0))
        view.handlers.pause_recording()
        options['state_changed'](RecordingState('paused', 2400, 24000, 1))
        view.handlers.stop_recording()
        view.handlers.resume_recording()
        assert options['stop_requested']() and not options['pause_requested']()
        options['state_changed'](RecordingState('finalizing', 2400, 24000, 1))
        assert view.progress.event.state == 'running'
        assert not view.values['buttons']['resume'].enabled
        return view.result
    view.service.capture = capture
    view.handlers.start()
    view.executor.finish()
    assert view.progress.events == ['completed']


def test_monitoring_is_default_off_worker_discovery_explicit_output_and_immutable_start_selection(view):
    discovered(view)
    selected = OutputDevice(1, 'interface-one', 'Chosen headphones', channels=2)
    view.service.monitoring_outputs.return_value = (selected,)
    assert view.values['monitoring'].GetValue() is False
    assert view.service.monitoring_outputs.call_count == 0
    view.handlers.refresh_monitor()
    assert view.service.monitoring_outputs.call_count == 0
    view.executor.finish()
    assert view.service.monitoring_outputs.call_count == 1
    assert view.values['monitor_output'].GetSelection() == -1
    assert view.values['monitoring'].GetValue() is False
    view.values['monitoring'].SetValue(True)
    view.handlers.start()
    assert view.executor.pending == [] and 'Select a monitoring output' in view.values['status'].value
    view.values['monitor_output'].SetSelection(0)
    view.progress.ready = view.parent.ready = True
    view.handlers.start()
    view.values['monitoring'].SetValue(False)
    view.values['monitor_output'].SetSelection(-1)
    view.executor.finish()
    assert view.captures[0][1]['monitor_output'] is selected


@pytest.mark.parametrize('invalidate', ['detach', 'library_changed'])
def test_queued_capture_state_cannot_touch_destroyed_or_replaced_library_view(view, invalidate):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    queued = []
    view.values['wx'].CallAfter = queued.append
    def capture(device_id, **options):
        options['state_changed'](RecordingState('recording', 2400, 24000, 0))
        options['state_changed'](RecordingState('paused', 2400, 24000, 1))
        return view.result
    view.service.capture = capture
    view.handlers.start()
    view.executor.finish()
    assert len(queued) == 3
    if invalidate == 'detach':
        view.handlers.detach()
    else:
        view.values['frame'].library = object()
    before = view.values['status'].value
    for callback in queued[:2]:
        callback()
    assert view.values['status'].value == before
    if invalidate == 'detach':
        queued[2]()
        assert view.values['status'].value == before
