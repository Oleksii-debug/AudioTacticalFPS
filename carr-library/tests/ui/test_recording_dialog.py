"""Actual recording callbacks and the existing host runner; no hardware claim."""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.audio.recording import AudioCancelled, AudioCleanupPending, RecordingOptions
from carr_library.ui.operation_progress import ProgressTracker
from carr_library.speech.progress_speech import play_when_quiet
from carr_library.speech.output_devices import OutputDevice
from carr_library.ui.recording_dialog import _VerifiedRecording


class Control:
    def __init__(self, value='', selection=0):
        self.value, self.selection, self.items, self.enabled = value, selection, [], True
        self.pulses = 0
    def Enable(self, enabled=True): self.enabled = enabled
    def Disable(self): self.enabled = False
    def GetSelection(self): return self.selection
    def SetSelection(self, selected): self.selection = selected
    def SetItems(self, items): self.items = list(items)
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def SetLabel(self, value): self.value = value
    def GetStringSelection(self): return str(self.value)
    def Pulse(self): self.pulses += 1
    def SetFocus(self): self.focused = True


class Progress:
    """Native handoff is modeled; measured state uses the current ProgressTracker."""
    def __init__(self):
        self.ready, self.fail_on, self.holds = False, None, []
        self.tracker = ProgressTracker()
        self.history = []
    def hold_audio(self, *, recording=False):
        if self.fail_on == 'hold':
            raise RuntimeError('Another media owner cannot confirm silence')
        owner = self
        class Hold:
            closed = False
            @property
            def ready(self): return owner.ready
            def close(self): self.closed = True
        hold = Hold()
        hold.recording = recording
        self.holds.append(hold)
        return hold

    def refresh_audio_state(self): pass
    def query(self, event=None): pass
    @property
    def event(self): return self.tracker.event
    def start(self, phase, total=None):
        value = self.tracker.start(phase, total)
        self.history.append(value)
        return value.token
    def update(self, completed=None, total=None, phase=None, *, token=None):
        value = self.tracker.update(completed, total, phase, token=token)
        self.history.append(value)
        return value
    def _state(self, state, phase=None, *, token=None):
        value = self.tracker.transition(state, phase, token=token)
        self.history.append(value)
        return value
    def succeed(self, phase=None, *, token=None): return self._state('succeeded', phase, token=token)
    def fail(self, phase=None, *, token=None): return self._state('failed', phase, token=token)
    def cancel(self, phase=None, *, token=None): return self._state('cancelled', phase, token=token)
    def pause(self, phase=None, *, token=None): return self._state('paused', phase, token=token)
    def finalize(self, *, token=None): pass



class Executor:
    def __init__(self): self.pending = []
    def submit(self, callback):
        future = Future()
        self.pending.append((callback, future))
        return future
    def finish(self):
        callback, future = self.pending.pop(0)
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = callback()
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)


def actual_host(wx, root):
    """Use the real current DesktopFrame.run_job, without constructing wx."""
    source = Path(find_spec('carr_library.ui.desktop_app').origin)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == 'DesktopFrame')
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in ('run_job', '_finish_job_controls')]
    assert len(methods) == 2
    factory = ast.ClassDef(name='Host', bases=[], keywords=[], body=methods, decorator_list=[])
    namespace = {'wx': wx, '__name__': 'carr_library.ui.desktop_app', '__package__': 'carr_library.ui'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(source), 'exec'), namespace)
    frame = namespace['Host']()
    frame.library = SimpleNamespace(root=root)
    frame._closing = frame.working = frame.busy = False
    frame.operation_progress = frame.progress_view = None
    frame.executor, frame.cancel_event = Executor(), threading.Event()
    frame.cancel_button, frame.choice, frame.book = Control(), Control(), None
    frame.menu_bar = SimpleNamespace(GetMenuCount=lambda: 0)
    frame.status, frame.messages = Control(), []
    frame.report = frame.messages.append
    return frame


def handlers(values):
    source = Path(find_spec('carr_library.ui.recording_dialog').origin)
    module = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_recording')
    names = {'current', 'cancellation', 'controls', 'release_holds', 'release_when_stopped',
             'settled', 'poll_cleanup', 'display_record', 'show_records', 'run', 'refresh', 'refresh_outputs',
             'start', 'retake', 'preview', 'use', 'close', 'stop_playback',
             'stop_recording', 'cancel_recording', 'query_progress'}
    definitions = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result
                        + ", 'detach': detach}\n")
    factory.body[0].body[:0] = definitions + ast.parse(
        'def detach():\n    nonlocal alive\n    alive = False\n').body
    namespace = dict(RecordingOptions=RecordingOptions, AudioCancelled=AudioCancelled,
        AudioCleanupPending=AudioCleanupPending, _VerifiedRecording=_VerifiedRecording,
        play_when_quiet=play_when_quiet, time=time, threading=threading,
        __name__='carr_library.ui.recording_dialog', __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), namespace)
    return SimpleNamespace(**namespace['build'](**values))


@pytest.fixture
def view(tmp_path):
    deferred, queued, captures = [], [], []
    def dispatch(callback, *args): queued.append((callback, args))
    wx = SimpleNamespace(NOT_FOUND=-1, ID_CANCEL=2, ID_OK=1, CallAfter=dispatch,
                         CallLater=lambda delay, callback: deferred.append(callback))
    frame = actual_host(wx, tmp_path)
    progress = Progress()
    frame.operation_progress = progress
    device = SimpleNamespace(id='selected-device', name='Explicit microphone')
    path = tmp_path / 'retained.wav'
    def write_recording():
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 240)
    result = dict(id='retained', title='My recording', created='2026-11-27T01:00:00Z',
                  path=str(path), audio=dict(duration_seconds=.01, sample_rate=24000, channels=1))
    def capture(device_id, **kwargs):
        captures.append((device_id, kwargs))
        if kwargs['cancelled']():
            raise AudioCancelled('User cancelled before capture')
        kwargs['progress'](1.25)
        write_recording()
        return result
    service = SimpleNamespace(devices=Mock(return_value=(device,)), capture=capture,
        get=Mock(return_value=result),
        retain=Mock(return_value=dict(asset='a' * 64 + '.wav', sha256='a' * 64,
                                     recording_id='retained', project_id='managed')))
    buttons = {name: Control() for name in (
        'refresh', 'refresh_outputs', 'query', 'start', 'retake', 'stop', 'cancel', 'preview', 'use', 'stop_preview', 'close')}
    values = dict(busy=False, alive=True, closing=False, recording=False, generation=0,
        devices=(), outputs=(), records=(), holds=[], cleanup_owner=None,
        output_devices=Mock(return_value=(OutputDevice(2, 'Explicit headphones', 'opaque-output'),)),
        cleanup_saved=None, cleanup_retry=None, hold_guard=threading.RLock(),
        active_started=threading.Event(), active_finished=threading.Event(),
        frame=frame, library=frame.library, progress=progress, player=Mock(),
        selected_result=None, service=service, cancelled=threading.Event(), stopped=threading.Event(),
        microphone=Control(selection=-1), title=Control('My recording'), minutes=Control(2),
        sample_rate=Control('24000'), channels=Control(selection=0),
        monitoring=Control(False), monitor_output=Control(selection=-1),
        saved=Control(selection=-1),
        buttons=buttons, details=Control(), status=Control(), gauge=Control(0), progress_label=Control(),
        dialog=Mock(), wx=wx, project_id=None, on_selected=None)
    def pump():
        count = 0
        while queued:
            callback, arguments = queued.pop(0)
            callback(*arguments)
            count += 1
            assert count < 100
    def finish():
        frame.executor.finish()
        pump()
    return SimpleNamespace(handlers=handlers(values), values=values, progress=progress,
        deferred=deferred, captures=captures, service=service, result=result, path=path,
        frame=frame, pump=pump, finish=finish, queued=queued)


def discovered(view):
    view.handlers.refresh()
    view.finish()
    view.values['microphone'].SetSelection(0)


def ready(view):
    discovered(view)
    view.progress.ready = True


def test_discovery_never_records_or_automatically_selects_even_one_microphone(view):
    view.handlers.start()
    assert not view.captures and not view.frame.executor.pending
    view.handlers.refresh()
    assert view.service.devices.call_count == 0 and not view.captures
    view.finish()
    assert view.values['microphone'].GetSelection() == -1
    assert view.values['microphone'].items == ['Explicit microphone (selected)']
    view.handlers.start()
    assert not view.captures and not view.frame.executor.pending


def test_start_waits_for_shared_quiet_then_uses_one_real_host_task_and_explicit_options(view):
    discovered(view)
    view.handlers.start()
    assert not view.captures and not view.frame.executor.pending
    assert len(view.progress.holds) == 1
    view.progress.ready = True
    view.deferred.pop(0)()
    assert not view.captures and view.frame.working
    view.handlers.stop_recording()
    view.finish()
    device_id, options = view.captures[0]
    assert device_id == 'selected-device'
    assert options['title'] == 'My recording'
    assert options['options'].max_seconds == 120 and options['options'].sample_rate == 24000
    assert options['stop_requested']() is True and options['cancelled']() is False
    assert options['retake_of'] is None
    assert view.progress.holds[0].closed
    assert view.values['status'].value.startswith('Saved and verified.')
    assert view.progress.holds[0].recording is True
    events = view.progress.history
    assert events[-1].state == 'succeeded'
    assert events[-1].completed == 1.25 and events[-1].total is None
    assert any(event.phase.startswith('Recorded 1.2 seconds') for event in events)
    assert view.values['gauge'].value == 100 and view.values['gauge'].pulses == 1
    with wave.open(str(view.path), 'rb') as stream:
        assert stream.getnframes() == 240 and stream.getframerate() == 24000


@pytest.mark.parametrize('action', ['stop_recording', 'cancel_recording'])
def test_stop_or_cancel_while_waiting_for_silence_never_opens_microphone(view, action):
    discovered(view)
    view.handlers.start()
    getattr(view.handlers, action)()
    view.deferred.pop(0)()
    assert not view.captures and not view.frame.executor.pending
    assert view.progress.holds[0].closed
    assert view.values['buttons']['refresh'].enabled


def test_close_active_operation_waits_for_real_host_cleanup(view):
    ready(view)
    view.handlers.start()
    event = Mock()
    view.handlers.close(event)
    event.Veto.assert_called_once()
    assert view.values['cancelled'].is_set() and not view.progress.holds[0].closed
    view.values['dialog'].EndModal.assert_not_called()
    view.finish()
    assert view.progress.holds[0].closed
    assert view.values['dialog'].EndModal.call_args.args == (2,)
    assert not view.captures and not view.path.exists()
    assert view.progress.event.state == 'cancelled'


def test_quiet_handoff_failure_does_not_record_or_leave_busy_controls(view):
    discovered(view)
    view.progress.fail_on = 'hold'
    view.handlers.start()
    assert not view.captures and not view.frame.executor.pending
    assert 'cannot confirm silence' in view.values['status'].value
    assert view.values['buttons']['refresh'].enabled


def test_real_running_worker_keeps_quiet_until_cleanup_after_detach(view):
    ready(view)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def capture(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(3)
            assert kwargs['cancelled']()
            raise AudioCancelled('Stopped after actual worker cleanup')
        finally:
            finished.set()
    view.service.capture = capture
    real = ThreadPoolExecutor(max_workers=1)
    view.frame.executor = real
    try:
        view.handlers.start()
        assert entered.wait(3)
        view.handlers.detach()
        view.values['cancelled'].set()
        view.handlers.release_when_stopped()
        assert not view.progress.holds[0].closed
        release.set()
        assert finished.wait(3)
    finally:
        release.set()
        real.shutdown(wait=True, cancel_futures=True)
    view.pump()
    assert view.progress.holds[0].closed and not view.path.exists()



@pytest.mark.parametrize('failure', ['update', 'succeed'])
def test_progress_observer_failure_cannot_reverse_saved_result_or_retry_capture(view, monkeypatch, failure):
    ready(view)
    monkeypatch.setattr(view.progress, failure, lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('Detached progress observer')))
    view.handlers.start()
    view.finish()
    assert view.values['status'].value.startswith('Saved and verified.')
    assert view.values['saved'].items == ['My recording (2026-11-27T01:00:00)']
    assert len(view.captures) == 1 and view.path.exists() and view.progress.holds[0].closed


class PendingOwner:
    def __init__(self):
        self.quiescent_event = threading.Event()
        self.callbacks = []
        self.retries = 0
    @property
    def quiescent(self): return self.quiescent_event.is_set()
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
    assert not view.captures and not view.frame.executor.pending and view.progress.holds[0].closed


@pytest.mark.parametrize('saved', [False, True], ids=['uncommitted', 'durable-original'])
def test_pending_cleanup_keeps_quiet_after_host_and_modal_settle(view, saved):
    ready(view)
    owner = PendingOwner()
    def pending(*args, **kwargs):
        error = AudioCleanupPending('Native cleanup is not confirmed', cleanup_owner=owner)
        if saved:
            error.saved_recording = view.result
        raise error
    view.service.capture = pending
    view.handlers.start()
    view.finish()
    assert not view.progress.holds[0].closed
    assert 'not confirmed silence' in view.values['status'].value
    assert not view.values['buttons']['start'].enabled
    assert bool(view.values['saved'].items) == saved
    view.handlers.detach()
    view.handlers.release_when_stopped()
    for value in view.values.values():
        if isinstance(value, Control):
            value.Enable = value.SetValue = lambda *a: pytest.fail('Detached native control accessed')
    for callback in tuple(view.deferred):
        callback()
    assert not view.progress.holds[0].closed
    owner.confirm()
    view.pump()
    assert view.progress.holds[0].closed


def test_retake_passes_selected_record_identity_without_changing_prior_record(view):
    ready(view)
    old = dict(view.result, id='prior-original', path=str(view.path.with_name('old.wav')))
    view.handlers.show_records((old,), old['id'])
    view.handlers.retake()
    view.finish()
    assert view.captures[0][1]['retake_of'] == 'prior-original'
    assert len(view.values['saved'].items) == 2
    assert old['id'] == 'prior-original'


def test_submit_rejection_releases_quiet_and_never_starts_microphone(view):
    ready(view)
    view.frame.executor.submit = Mock(side_effect=RuntimeError('Executor closed'))
    view.handlers.start()
    view.pump()
    assert not view.captures and not view.path.exists()
    assert view.progress.holds[0].closed
    assert view.values['buttons']['refresh'].enabled
    assert 'Executor closed' in view.values['status'].value


def test_late_cancel_keeps_the_actual_completed_recording(view):
    ready(view)
    original = view.service.capture
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        view.frame.cancel_event.set()
        return result
    view.service.capture = capture
    view.handlers.start()
    view.finish()
    assert view.path.exists() and view.values['status'].value.startswith('Saved and verified.')
    assert view.progress.event.state == 'succeeded'


def test_selected_record_is_retained_only_for_an_explicit_managed_project(view):
    delivered = []
    view.values['project_id'] = 'managed'
    view.values['on_selected'] = delivered.append
    view.handlers = handlers(view.values)
    view.handlers.show_records((view.result,), view.result['id'])
    view.handlers.use()
    view.finish()
    assert len(delivered) == 1 and delivered[0]['retained']['project_id'] == 'managed'
    assert view.service.retain.call_args.args == ('retained', 'managed')
    view.values['dialog'].EndModal.assert_called_once_with(1)


def test_plain_selection_does_not_create_a_project_or_reinterpret_narration(view):
    delivered = []
    view.values['on_selected'] = delivered.append
    view.handlers = handlers(view.values)
    view.handlers.show_records((view.result,), view.result['id'])
    view.handlers.use()
    view.finish()
    assert delivered == [view.result]
    view.service.retain.assert_not_called()


def test_changed_library_cannot_open_a_queued_microphone_or_publish_selection(view):
    ready(view)
    view.handlers.start()
    view.frame.library = SimpleNamespace(root=view.path.parent / 'other')
    view.finish()
    assert not view.captures and not view.path.exists()
    assert view.progress.holds[0].closed
    view.values['dialog'].EndModal.assert_called_with(2)



def test_shared_cancel_while_waiting_for_silence_is_not_cleared_by_launch(view):
    discovered(view)
    view.handlers.start()
    view.frame.cancel_event.set()
    view.progress.ready = True
    view.deferred.pop(0)()
    assert not view.captures and not view.frame.executor.pending
    assert view.progress.holds[0].closed and view.frame.cancel_event.is_set()


def test_old_cancel_handler_cannot_cancel_a_new_unrelated_host_job(view):
    view.frame.working = True
    view.handlers.cancel_recording()
    assert not view.frame.cancel_event.is_set()


def test_saved_preview_verifies_before_current_player_and_quiet_admission(view):
    view.handlers.show_records((view.result,), view.result['id'])
    view.handlers.preview()
    view.values['player'].play.assert_not_called()
    view.finish()
    view.service.get.assert_called_once()
    view.values['player'].play.assert_not_called()
    view.progress.ready = True
    view.deferred.pop(0)()
    view.values['player'].play.assert_called_once_with(str(view.path))
    assert view.progress.holds[0].closed


def test_stopping_preview_cancels_an_armed_quiet_restart(view):
    view.handlers.show_records((view.result,), view.result['id'])
    view.handlers.preview()
    view.finish()
    view.handlers.stop_playback()
    view.progress.ready = True
    view.deferred.pop(0)()
    view.values['player'].play.assert_not_called()
    assert view.progress.holds[0].closed


def test_audio_studio_menu_exposes_recording_without_a_new_global_shortcut():
    source = Path(find_spec('carr_library.ui.audio_studio').origin).read_text(encoding='utf-8')
    tree = ast.parse(source)
    installer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'install_audio_studio')
    label = next(node.args[1].value for node in ast.walk(installer)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'Append' and len(node.args) > 1
                 and isinstance(node.args[1], ast.Constant)
                 and 'Record microphone' in str(node.args[1].value))
    assert '\\t' not in label
    handler = next(node for node in installer.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'open_recording')
    assert any(isinstance(node, ast.ImportFrom) and node.module == 'recording_dialog'
               for node in ast.walk(handler))

def test_old_cleanup_notification_cannot_release_a_new_recording_hold(view):
    ready(view)
    original = view.service.capture
    owner = PendingOwner()
    def pending(*args, **kwargs):
        raise AudioCleanupPending('Retained cleanup fixture', cleanup_owner=owner)
    view.service.capture = pending
    view.handlers.start()
    view.finish()
    first_hold = view.progress.holds[-1]
    owner.confirm()  # Queues the delayed completion; do not dispatch it yet.
    view.handlers.poll_cleanup()
    assert first_hold.closed
    view.service.capture = original
    view.handlers.start()
    current_hold = view.progress.holds[-1]
    assert current_hold is not first_hold and not current_hold.closed
    view.pump()  # Deliver the previous generation only after the new Start.
    assert not current_hold.closed
    view.finish()
    assert current_hold.closed and len(view.captures) == 1

def test_explicit_progress_query_is_visible_and_focused_inside_the_modal(view):
    ready(view)
    view.handlers.start()
    view.frame.status.SetValue('Recorded 2.4 seconds; total is not yet known.')
    view.handlers.query_progress()
    assert view.values['status'].value == view.frame.status.GetValue()
    assert view.values['status'].focused
    assert view.values['buttons']['query'].enabled
    assert not view.progress.holds[0].closed
    view.finish()



def test_monitoring_is_opt_in_and_missing_explicit_output_cannot_start_capture(view):
    ready(view)
    assert view.values['monitoring'].GetValue() is False
    assert view.values['output_devices'].call_count == 0
    view.values['monitoring'].SetValue(True)
    view.handlers.controls()
    assert not view.values['buttons']['start'].enabled
    view.handlers.start()
    assert not view.captures and not view.frame.executor.pending
    assert not view.progress.holds
    assert 'explicitly select' in view.values['status'].GetValue()


def test_monitoring_output_discovery_is_background_read_only_and_never_selects_default(view):
    ready(view)
    view.handlers.refresh_outputs()
    assert view.values['output_devices'].call_count == 0
    assert not view.captures and not view.progress.holds
    view.finish()
    assert view.values['output_devices'].call_count == 1
    assert view.values['monitor_output'].items == ['Explicit headphones']
    assert view.values['monitor_output'].GetSelection() == -1
    assert not view.captures and not view.progress.holds


def test_start_captures_exact_explicit_monitor_identity_under_existing_quiet_hold(view):
    ready(view)
    view.handlers.refresh_outputs()
    view.finish()
    view.values['monitoring'].SetValue(True)
    view.values['monitor_output'].SetSelection(0)
    view.handlers.controls()
    assert view.values['buttons']['start'].enabled
    view.handlers.start()
    assert len(view.progress.holds) == 1 and not view.progress.holds[0].closed
    assert not view.values['monitoring'].enabled and not view.values['monitor_output'].enabled
    assert not view.values['buttons']['refresh_outputs'].enabled
    # A later UI change cannot alter the already captured request.
    view.values['monitor_output'].SetSelection(-1)
    view.finish()
    selected = view.captures[0][1]['monitor_output']
    assert selected == OutputDevice(2, 'Explicit headphones', 'opaque-output')
    assert selected.identity == view.values['output_devices'].return_value[0].identity
    assert view.progress.holds[0].closed


def test_unchecked_monitoring_never_passes_a_previously_selected_output(view):
    ready(view)
    view.handlers.refresh_outputs()
    view.finish()
    view.values['monitor_output'].SetSelection(0)
    view.handlers.start()
    view.finish()
    assert view.captures[0][1]['monitor_output'] is None
    assert view.values['monitoring'].GetValue() is False
