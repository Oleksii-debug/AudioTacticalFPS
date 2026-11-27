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
from carr_library.progress import ProgressHub, ProgressReporter, current_progress
from carr_library.ui.recording_dialog import _VerifiedRecording, install_recording_actions


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


class Progress:
    """Only the native quiet handoff is fake; host progress uses the real hub."""
    def __init__(self):
        self.ready, self.fail_on, self.holds = False, None, []
    def hold_audio(self):
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
    def tick(self): pass


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
    namespace = {'wx': wx}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(source), 'exec'), namespace)
    frame = namespace['Host']()
    frame.library = SimpleNamespace(root=root)
    frame._closing = frame.working = frame.busy = False
    frame.progress_hub = ProgressHub()
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
             'settled', 'poll_cleanup', 'display_record', 'show_records', 'run', 'refresh',
             'refresh_outputs', 'start', 'retake', 'preview', 'use', 'close', 'stop_playback',
             'stop_recording', 'cancel_recording'}
    definitions = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result
                        + ", 'detach': detach}\n")
    factory.body[0].body[:0] = definitions + ast.parse(
        'def detach():\n    nonlocal alive\n    alive = False\n').body
    namespace = dict(RecordingOptions=RecordingOptions, AudioCancelled=AudioCancelled,
        AudioCleanupPending=AudioCleanupPending, _VerifiedRecording=_VerifiedRecording,
        current_progress=current_progress, time=time, threading=threading)
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
    device = SimpleNamespace(id='selected-device', name='Explicit microphone')
    output_device = SimpleNamespace(id=7, name='Explicit headphones', channels=2)
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
        monitor_devices=Mock(return_value=(output_device,)), get=Mock(return_value=result),
        retain=Mock(return_value=dict(asset='a' * 64 + '.wav', sha256='a' * 64,
                                     recording_id='retained', project_id='managed')))
    buttons = {name: Control() for name in (
        'refresh', 'outputs', 'start', 'retake', 'stop', 'cancel', 'preview', 'use', 'stop_preview', 'close')}
    values = dict(busy=False, alive=True, closing=False, recording=False, generation=0,
        devices=(), output_devices=(), records=(), holds=[], cleanup_owner=None,
        cleanup_saved=None, cleanup_retry=None, preview_request=None, hold_guard=threading.RLock(),
        active_started=threading.Event(), active_finished=threading.Event(),
        frame=frame, library=frame.library, progress=progress, player=Mock(),
        selected_result=None, service=service, cancelled=threading.Event(), stopped=threading.Event(),
        microphone=Control(selection=-1), title=Control('My recording'), minutes=Control(2),
        sample_rate=Control('24000'), channels=Control(selection=0),
        monitor=Control(False), output=Control(selection=-1), saved=Control(selection=-1),
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
    assert options['monitor'] is False and options['monitor_device_id'] is None
    assert options['retake_of'] is None
    assert view.progress.holds[0].closed
    assert view.values['status'].value.startswith('Saved and verified.')
    events = view.frame.progress_hub.snapshot()
    assert len(events) == 2
    assert events[-1].state == 'complete' and events[-1].verified
    assert events[-1].completed == 1 and events[-1].total is None
    assert events[-1].unit == 'seconds'
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
    assert view.frame.progress_hub.snapshot()[-1].state == 'cancelled'


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


@pytest.mark.parametrize('failure', ['update', 'event'])
def test_progress_observer_failure_cannot_reverse_saved_result_or_retry_capture(view, monkeypatch, failure):
    ready(view)
    if failure == 'update':
        monkeypatch.setattr(ProgressReporter, 'update', lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError('Detached progress observer')))
    else:
        monkeypatch.setattr(ProgressReporter, 'event', property(lambda self: (_ for _ in ()).throw(
            RuntimeError('Detached progress observer'))))
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


def test_monitor_requires_explicit_current_output_and_passes_exact_device(view):
    ready(view)
    view.values['monitor'].SetValue(True)
    view.handlers.start()
    assert not view.captures and 'explicitly' in view.values['status'].value
    view.handlers.refresh_outputs()
    view.finish()
    assert view.values['output'].GetSelection() == -1
    view.handlers.start()
    assert not view.frame.executor.pending
    view.values['output'].SetSelection(0)
    view.handlers.start()
    view.finish()
    assert view.captures[0][1]['monitor'] is True
    assert view.captures[0][1]['monitor_device_id'] == 7


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
    event = view.frame.progress_hub.snapshot()[-1]
    assert event.state == 'complete' and event.verified


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


def test_installation_reuses_one_audio_command_without_default_shortcut(monkeypatch):
    import sys
    from carr_library.ui import keyboard_settings
    fake_wx = SimpleNamespace(ID_ANY=-1, EVT_MENU='menu')
    monkeypatch.setitem(sys.modules, 'wx', fake_wx)
    specs, items = [], []
    monkeypatch.setattr(keyboard_settings, 'register_menu_command',
                        lambda frame, spec, item: specs.append(spec))
    class Menu:
        def Append(self, identity, label):
            item = SimpleNamespace(label=label)
            items.append(item)
            return item
    menu = Menu()
    bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabel=lambda index: 'A&udio',
                          GetMenu=lambda index: menu)
    frame = SimpleNamespace(menu_bar=bar, Bind=Mock())
    first = install_recording_actions(frame)
    assert install_recording_actions(frame) is first
    assert len(items) == len(specs) == frame.Bind.call_count == 1
    assert specs[0].command_id == 'audio.recording' and specs[0].default_shortcut == ''


@pytest.mark.parametrize('mode', ['saved-script', 'literal', 'unsaved-script', 'external-script'])
def test_studio_selection_inserts_only_into_explicit_saved_structured_project(tmp_path, monkeypatch, mode):
    from carr_library.application import LibraryApp
    from carr_library.audio.managed import ManagedProjects
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
    from carr_library.ui import recording_dialog
    library = LibraryApp(tmp_path / 'library')
    controller = StudioController(private_root=library.root)
    original = 'Незмінний оригінал — [sound words are literal here].'
    controller.new(original, input_kind='narration' if mode == 'literal' else 'script')
    if mode in ('saved-script', 'literal'):
        controller.save_in_library()
    elif mode == 'external-script':
        controller.save(tmp_path / 'standalone.json')
    saved_path = controller.project_path
    saved_bytes = None if saved_path is None else saved_path.read_bytes()
    source = tmp_path / 'recording.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 240)
    retained = dict(asset='a' * 64 + '.wav', sha256='a' * 64,
                    project_id=controller.project_id, recording_id='source-recording')
    calls = []
    def select(frame, **kwargs):
        calls.append(kwargs)
        kwargs['on_selected'](dict(path=str(source), retained=retained))
    monkeypatch.setattr(recording_dialog, 'show_recording', select)
    view = SimpleNamespace(controller=controller, busy=False, closed=False,
        parent=SimpleNamespace(library=library, working=False), dialog=object(),
        playback=SimpleNamespace(enqueue=Mock()), _capture=Mock(),
        _stop_for_project_change=Mock(return_value=True), _refresh_text=Mock(),
        _refresh_playback_choices=Mock(), _message=Mock(), _error=Mock())
    AudioStudioDialog.record_microphone(view)
    view._error.assert_not_called()
    assert len(calls) == 1 and calls[0]['window_parent'] is view.dialog
    assert controller.draft.source_text == original
    if mode == 'saved-script':
        assert calls[0]['project_id'] == controller.project_id
        assert calls[0]['selection_label'] == 'Insert selected recording as a Sound cue'
        assert controller.script == original + '\n[sound ' + retained['asset'] + ']\n'
        assert controller.dirty
        view._refresh_text.assert_called_once()
        view.playback.enqueue.assert_not_called()
        assert ManagedProjects(library.root).locate(saved_path, controller.project_id) is not None
    else:
        assert calls[0]['project_id'] is None
        assert calls[0]['selection_label'] == 'Add selected recording to the playback playlist'
        assert controller.script == original
        assert controller.input_kind == ('narration' if mode == 'literal' else 'script')
        view.playback.enqueue.assert_called_once_with([source])
        view._refresh_text.assert_not_called()
    if saved_path is not None:
        assert saved_path.read_bytes() == saved_bytes


@pytest.mark.parametrize('change', ['different-project', 'foreign-retained-asset'])
def test_studio_recording_selection_cannot_cross_project_identity(tmp_path, monkeypatch, change):
    from carr_library.application import LibraryApp
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
    from carr_library.ui import recording_dialog
    library = LibraryApp(tmp_path / 'library')
    controller = StudioController(private_root=library.root)
    controller.new('Original', input_kind='script')
    controller.save_in_library()
    original = controller.project_path.read_bytes()
    def select(frame, **kwargs):
        identity = controller.project_id
        if change == 'different-project':
            controller.new('New unrelated project', input_kind='script')
        value = dict(path=str(tmp_path / 'recording.wav'), retained=dict(
            asset='b' * 64 + '.wav', sha256='b' * 64, recording_id='recording',
            project_id='wrong-project' if change == 'foreign-retained-asset' else identity))
        kwargs['on_selected'](value)
    monkeypatch.setattr(recording_dialog, 'show_recording', select)
    view = SimpleNamespace(controller=controller, busy=False, closed=False,
        parent=SimpleNamespace(library=library, working=False), dialog=object(),
        playback=SimpleNamespace(enqueue=Mock()), _capture=Mock(),
        _stop_for_project_change=Mock(return_value=True), _refresh_text=Mock(),
        _refresh_playback_choices=Mock(), _message=Mock(), _error=Mock())
    path = controller.project_path
    AudioStudioDialog.record_microphone(view)
    view._error.assert_called_once()
    view.playback.enqueue.assert_not_called()
    view._refresh_text.assert_not_called()
    assert path.read_bytes() == original
    assert '[sound ' not in controller.script
