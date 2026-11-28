"""Execute real recording callbacks; these are not hardware or native UI claims."""
import ast
from concurrent.futures import Future
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.audio.recording import RecordingOptions, RecordingState
from carr_library.audio.process import CaptureCleanupError
from carr_library.progress import ProgressHub, ProgressState, bind_progress, current_progress
from carr_library.speech.playback_devices import PlaybackDevice


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
        self.ready, self.fail, self.holds, self.events = False, None, [], []
        self.hub, self.active_task = ProgressHub(), None
    def hold_audio(self, *, recording=False):
        assert recording is True
        if self.fail == 'hold':
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
    def begin(self, phase, **kwargs):
        if self.fail == 'start': raise RuntimeError('Detached progress view')
        self.active_task = self.hub.start(phase, **kwargs)
        return self.active_task
    def finish(self, task, state, message):
        if state == 'succeeded':
            if self.fail == 'succeed': raise RuntimeError('Detached progress view')
            task.succeed(durable=True, message=message)
            self.events.append('completed')
        elif state == 'failed':
            task.fail(message)
            self.events.append('failed')
        elif state == 'cancelled':
            task.cancelled(message)
            self.events.append('cancelled')
        else:
            raise AssertionError(state)
    def reporting_unavailable(self, task, state, message):
        if not task.snapshot().terminal:
            task.unavailable(message)


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
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/recording_dialog.py'
    module = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'show_recording')
    names = {'controls', 'release_holds', 'release_when_stopped', 'display_record', 'show_records', 'run', 'refresh',
             'refresh_monitor', 'start', 'close', 'stop_recording', 'cancel_recording', 'pause_recording', 'resume_recording'}
    definitions = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result + '}\n')
    factory.body[0].body[:0] = definitions
    namespace = {'RecordingOptions': RecordingOptions, 'AudioCancelled': AudioCancelled, 'time': time,
                 'ProgressState': ProgressState, 'bind_progress': bind_progress,
                 'current_progress': current_progress, 'CaptureCleanupError': CaptureCleanupError,
                 '_UNCONFIRMED_RECORDING_HOLDS': values['retained_holds']}
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
    buttons = {name: Control() for name in ('refresh', 'refresh_monitor', 'start', 'stop', 'cancel',
                                          'pause', 'resume', 'preview', 'use', 'stop_preview', 'close')}
    values = dict(busy=False, alive=True, closing=False, generation=0, active_future=None, devices=(), records=(), holds=[],
        selected_result=None, controllers=(progress, parent), progress=progress, player=Mock(),
        executor=executor, service=service, cancelled=threading.Event(), stopped=threading.Event(),
        paused=threading.Event(), capture_phase='idle', monitor_outputs=(), retained_holds=[],
        monitoring=Control(False), monitor_output=Control(selection=-1),
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
    assert view.progress.active_task.snapshot().state == ProgressState.CANCELLED


def test_partial_silence_admission_failure_releases_existing_holds_and_does_not_record(view):
    discovered(view)
    view.parent.fail = 'hold'
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
    view.progress.fail = failure
    view.handlers.start()
    view.executor.finish()
    assert view.values['status'].value.startswith('Saved and verified.')
    assert view.values['saved'].items == ['My recording (2026-11-27T01:00:00)']
    assert len(view.captures) == 1 and view.progress.holds[0].closed
    assert 'Detached progress view' in view.progress.reporting_error


def test_pause_request_waits_for_confirmed_backend_state_and_resume_keeps_same_progress_task(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    observed = []
    def capture(device_id, **options):
        task = current_progress()
        observed.append(task)
        options['state_changed'](RecordingState('recording', 2400, 24000, 0))
        options['progress'](.1)
        assert view.values['buttons']['pause'].enabled and not view.values['buttons']['resume'].enabled
        view.handlers.pause_recording()
        assert options['pause_requested']() and task.snapshot().state == ProgressState.RUNNING
        assert not view.values['buttons']['resume'].enabled
        options['state_changed'](RecordingState('pausing', 2400, 24000, 0))
        assert task.snapshot().state == ProgressState.RUNNING
        options['state_changed'](RecordingState('paused', 3000, 24000, 1))
        assert task.snapshot().state == ProgressState.PAUSED
        assert view.values['buttons']['resume'].enabled and not view.values['buttons']['pause'].enabled
        assert all(not hold.closed for hold in (*view.progress.holds, *view.parent.holds))
        view.handlers.resume_recording()
        assert not options['pause_requested']() and task.snapshot().state == ProgressState.PAUSED
        options['state_changed'](RecordingState('opening', 3000, 24000, 1))
        assert current_progress() is task and task.snapshot().state == ProgressState.RUNNING
        options['state_changed'](RecordingState('recording', 6000, 24000, 1))
        options['progress'](.25)
        assert task.snapshot().completed == 250
        return view.result
    view.service.capture = capture
    view.handlers.start()
    view.executor.finish()
    assert observed == [view.progress.active_task]
    assert view.progress.active_task.snapshot().state == ProgressState.SUCCEEDED


def test_stop_while_paused_resumes_final_verification_before_success_without_opening_input(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    def capture(device_id, **options):
        options['state_changed'](RecordingState('recording', 2400, 24000, 0))
        view.handlers.pause_recording()
        options['state_changed'](RecordingState('paused', 2400, 24000, 1))
        assert current_progress().snapshot().state == ProgressState.PAUSED
        view.handlers.stop_recording()
        view.handlers.resume_recording()
        assert options['stop_requested']() and options['pause_requested']()
        options['state_changed'](RecordingState('finalizing', 2400, 24000, 1))
        assert current_progress().snapshot().state == ProgressState.RUNNING
        assert not view.values['buttons']['resume'].enabled
        return view.result
    view.service.capture = capture
    view.handlers.start()
    view.executor.finish()
    assert view.progress.active_task.snapshot().state == ProgressState.SUCCEEDED


def test_monitoring_is_default_off_inventory_is_worker_only_and_selection_is_frozen(view):
    discovered(view)
    selected = PlaybackDevice(1, 'Chosen headphones', 'interface-one', 1, 2, 3, 2)
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


def test_unconfirmed_capture_cleanup_does_not_release_quiet_or_report_discarded_success(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    def capture(*args, **kwargs):
        raise CaptureCleanupError('Owned monitoring buffers remain active; restart required.')
    view.service.capture = capture
    view.handlers.start()
    view.handlers.release_when_stopped()
    view.executor.finish()
    assert len(view.values['retained_holds']) == 2
    assert all(not hold.closed for hold in (*view.progress.holds, *view.parent.holds))
    assert view.progress.active_task.snapshot().state == ProgressState.FAILED
    assert 'restart required' in view.values['status'].value


def test_late_recording_state_callbacks_after_close_do_not_touch_native_controls(view):
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
    view.handlers.close(Mock())
    original = view.values['status'].value
    for callback in queued[:2]:
        callback()
    assert view.values['status'].value == original
    queued[2]()
    assert view.values['dialog'].EndModal.call_count == 1


def test_recording_progress_uses_actual_duration_in_the_bound_canonical_task(view):
    discovered(view)
    view.progress.ready = view.parent.ready = True
    observed = []
    actual = view.service.capture

    def capture(device_id, **options):
        assert current_progress() is view.progress.active_task
        result = actual(device_id, **options)
        observed.append(current_progress().snapshot())
        return result

    view.service.capture = capture
    view.handlers.start()
    assert view.values['dialog'].working is True
    view.executor.finish()
    snapshot, = observed
    assert snapshot.state == ProgressState.RUNNING
    assert snapshot.completed == 1000 and snapshot.unit == 'milliseconds'
    assert snapshot.total is None and snapshot.percent is None
    assert snapshot.phase == 'Recorded 1.0 seconds'
    completed = view.progress.active_task.snapshot()
    assert completed.operation_id == snapshot.operation_id
    assert completed.state == ProgressState.SUCCEEDED and completed.percent == 100
    assert view.progress.hub.snapshot(completed.operation_id) == completed
    assert current_progress() is None and view.values['dialog'].working is False
    assert view.progress.holds[0].closed and view.parent.holds[0].closed
    assert len(view.captures) == 1 and view.values['saved'].items


def test_complete_dialog_construction_keeps_monitoring_off_and_never_opens_input(tmp_path, monkeypatch):
    """Run the complete production constructor with explicit native/device stand-ins."""
    import sys
    from unittest.mock import MagicMock
    import carr_library.ui.recording_dialog as module

    wx = MagicMock()
    wx.CheckBox.return_value.GetValue.return_value = False
    def choice(*args, **kwargs):
        control = MagicMock()
        control.GetSelection.return_value = -1
        return control
    wx.Choice.side_effect = choice
    monkeypatch.setitem(sys.modules, 'wx', wx)
    service, player, progress, executor = (MagicMock() for _ in range(4))
    future = Future()
    executor.submit.return_value = future
    executor.shutdown.side_effect = lambda **kwargs: future.cancel()
    monkeypatch.setattr(module, 'RecordingService', Mock(return_value=service))
    monkeypatch.setattr(module, 'WavePlayer', Mock(return_value=player))
    monkeypatch.setattr(module, 'NativeProgressPanel', Mock(return_value=progress))
    monkeypatch.setattr(module, 'ThreadPoolExecutor', Mock(return_value=executor))
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), working=False,
                            progress_panel=None, report=Mock())

    assert module.show_recording(frame) is None
    wx.Dialog.return_value.ShowModal.assert_called_once_with()
    wx.CheckBox.assert_called_once_with(wx.ScrolledWindow.return_value,
                                       label='Enable dry input monitoring')
    wx.CheckBox.return_value.SetValue.assert_called_once_with(False)
    wx.CheckBox.return_value.SetName.assert_any_call('Enable dry input monitoring')
    service.devices.assert_not_called()
    service.capture.assert_not_called()
    service.monitoring_outputs.assert_not_called()
    player.play.assert_not_called()
    player.close.assert_called_once_with()
    progress.unregister_player.assert_called_once_with(player, silence_confirmed=True)
    progress.close.assert_called_once_with()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    wx.Dialog.return_value.Destroy.assert_called_once_with()


@pytest.mark.parametrize('delivered_id,same_wrapper,wrapper_live,worker_kind,accepted', [
    pytest.param(-31994, False, False, None, True, id='native-id-new-python-wrapper'),
    pytest.param(-31994, True, False, None, True, id='native-id-same-python-wrapper'),
    pytest.param(-31995, False, False, None, False, id='child-event-is-not-owner-destruction'),
    pytest.param(None, False, False, None, False, id='dead-wrapper-without-native-event'),
    pytest.param(-31994, False, True, None, False, id='native-event-with-live-wrapper'),
    pytest.param(-31994, False, False, 'desktop', False, id='desktop-worker-still-live'),
    pytest.param(-31994, False, False, 'recording', False, id='recording-worker-still-live'),
])
def test_native_recording_close_requires_owner_event_dead_wrapper_and_reaped_threads(
        delivered_id, same_wrapper, wrapper_live, worker_kind, accepted):
    """Run both exact native observer/cleanup closures, with real held workers."""
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_recording_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name in {'owner_destroyed', 'finish_owner_close'}]
    assert {node.name for node in functions} == {'owner_destroyed', 'finish_owner_close'}
    assert len(functions) == 2

    class Frame:
        def __bool__(self):
            return wrapper_live

    frame, destroyed, release = Frame(), threading.Event(), threading.Event()
    worker = None
    if worker_kind is not None:
        worker = threading.Thread(
            target=release.wait,
            name='carr-recording-native-test' if worker_kind == 'recording' else 'desktop-native-test')
        worker.start()
    frame.executor = SimpleNamespace(_threads={worker} if worker_kind == 'desktop' else set())
    frame.Destroy = Mock()
    checks = []

    def check(name, condition):
        assert condition, name
        checks.append(name)

    namespace = {
        'frame': frame, 'captured_frame_id': -31994, 'frame_destroyed': destroyed,
        'threading': threading, 'deadline': 80, 'monotonic': lambda: 81,
        'guarded': lambda callback: callback, 'check': check,
        'wx': SimpleNamespace(CallLater=Mock()),
        'app': SimpleNamespace(ExitMainLoop=Mock()),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    try:
        if delivered_id is not None:
            event = SimpleNamespace(
                GetId=lambda: delivered_id,
                GetEventObject=lambda: frame if same_wrapper else object(), Skip=Mock())
            namespace['owner_destroyed'](event)
            event.Skip.assert_called_once_with()
        assert destroyed.is_set() is (delivered_id == -31994)
        if accepted:
            namespace['finish_owner_close']()
            assert checks == [
                'actual native frame destruction finishes owned workers',
                'native recording frame wrapper is no longer live',
            ]
            namespace['app'].ExitMainLoop.assert_called_once_with()
        else:
            if delivered_id == -31994 and wrapper_live and worker_kind is None:
                with pytest.raises(AssertionError, match='wrapper is no longer live'):
                    namespace['finish_owner_close']()
            else:
                with pytest.raises(TimeoutError, match='owners did not finish closing'):
                    namespace['finish_owner_close']()
            namespace['app'].ExitMainLoop.assert_not_called()
        namespace['wx'].CallLater.assert_not_called()
        frame.Destroy.assert_not_called()
    finally:
        release.set()
        if worker is not None:
            worker.join(timeout=5)
            assert not worker.is_alive()


@pytest.mark.parametrize('wrapper_live', [False, True], ids=['already-deleted', 'still-live'])
def test_native_recording_failure_cleanup_never_destroys_a_deleted_wrapper(wrapper_live):
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_recording_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    guards = [node for node in ast.walk(tree)
              if isinstance(node, ast.If) and ast.unparse(node.test) == 'frame is not None'
              and any(isinstance(call, ast.Call) and ast.unparse(call.func) == 'frame.Destroy'
                      for call in ast.walk(node))]
    assert len(guards) == 1

    class Frame:
        def __bool__(self):
            return wrapper_live

    frame = Frame()
    frame.cancel_event = SimpleNamespace(set=Mock())
    frame.executor = SimpleNamespace(shutdown=Mock())
    frame.Destroy = Mock()
    namespace = {'frame': frame, 'frame_destroyed': threading.Event()}
    exec(compile(ast.Module(body=guards, type_ignores=[]), str(path), 'exec'), namespace)
    frame.cancel_event.set.assert_called_once_with()
    frame.executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
    if wrapper_live:
        frame.Destroy.assert_called_once_with()
    else:
        frame.Destroy.assert_not_called()
