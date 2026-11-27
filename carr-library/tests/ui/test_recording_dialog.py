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
from carr_library.audio.recording import RecordingOptions
from carr_library.progress import ProgressHub, ProgressState, bind_progress, current_progress


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
             'start', 'close', 'stop_recording', 'cancel_recording'}
    definitions = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result + '}\n')
    factory.body[0].body[:0] = definitions
    namespace = {'RecordingOptions': RecordingOptions, 'AudioCancelled': AudioCancelled, 'time': time,
                 'ProgressState': ProgressState, 'bind_progress': bind_progress}
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
