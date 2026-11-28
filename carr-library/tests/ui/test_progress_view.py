"""Headless current-host/native-widget boundaries; no Windows or NVDA claim."""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, ProgressPreferences, current_progress, load_preferences, save_preferences
from carr_library.ui import progress_view
from carr_library.ui import operation_progress as native_progress


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_keyboard = _module('progress_keyboard_boundaries', Path(__file__).with_name('test_keyboard_manager.py'))


class Control:
    focus = None

    def __init__(self, *args, **kwargs):
        self.name = ''
        self.value = kwargs.get('value', '')
        self.enabled = True
        self.selection = -1
        self.items = list(kwargs.get('choices', []))
        self.pulses = 0
        self.bindings = []
        self.destroyed = False

    def SetName(self, value): self.name = value
    def GetName(self): return self.name
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def SetFocus(self): Control.focus = self
    def SetInsertionPoint(self, value): pass
    def SetMinSize(self, value): pass
    def Bind(self, *args): self.bindings.append(args)
    def SetSizer(self, value): pass
    def Layout(self): pass
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def IsEnabled(self): return self.enabled
    def Set(self, values): self.items = list(values); self.selection = -1
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def Pulse(self): self.pulses += 1
    def GetRange(self): return 100
    def SetRange(self, value): self.range = value
    def Wrap(self, width): pass
    def GetLabel(self): return self.value
    def SetEscapeId(self, identity): pass
    def EndModal(self, code): self.modal_result = code
    def Destroy(self): self.destroyed = True


class Layout:
    def __init__(self, *args): self.items = []
    def Add(self, *args): self.items.append(args)
    def Insert(self, *args): self.items.append(args)


class Timer:
    def __init__(self, owner): self.running = False; self.stops = 0
    def Start(self, interval): self.running = True; self.interval = interval
    def Stop(self): self.running = False; self.stops += 1


class Notifications:
    def __init__(self):
        self.preferences = None
        self.events = ()
        self.error = ''
        self.closes = 0
        self.environments = []
        self.quiescent = True
        self.registry = None

    def configure(self, value): self.preferences = value
    def clear(self): self.events = ()
    def set_registry(self, value): self.registry = value
    def environment(self, **values): self.environments.append(values)
    def offer(self, events): self.events = tuple(events)
    def close(self): self.closes += 1


@pytest.fixture
def native_boundary(tmp_path, monkeypatch):
    # Real preference files, registry, domain events and renderer methods; only
    # wx window/audio-device boundaries are doubles in this headless suite.
    fake = SimpleNamespace(Panel=Control, Choice=Control, Gauge=Control, TextCtrl=Control,
        Button=Control, CheckBox=Control, SpinCtrlDouble=Control, Slider=Control, Dialog=Control,
        BoxSizer=Layout, WrapSizer=Layout, Timer=Timer, Menu=_keyboard.Menu,
        Window=SimpleNamespace(FindFocus=lambda: Control.focus), CallAfter=lambda fn,*a: fn(*a),
        ID_ANY=-1, ID_OK=1, ID_CANCEL=2, VERTICAL=1, HORIZONTAL=2, GA_HORIZONTAL=4,
        TE_READONLY=8, TE_MULTILINE=16, EXPAND=32, ALL=64, ALIGN_RIGHT=128,
        SL_HORIZONTAL=256, SL_LABELS=512, LEFT=1024, TOP=2048,
        EVT_CHOICE=object(), EVT_BUTTON=object(), EVT_TIMER=object(), EVT_WINDOW_DESTROY=object(),
        EVT_MENU=object(), EVT_CHECKBOX=object())
    monkeypatch.setitem(sys.modules, 'wx', fake)
    monkeypatch.setattr(_keyboard.MenuItem, 'Enable', lambda self, enabled=True: setattr(self, 'enabled', enabled), raising=False)
    monkeypatch.setattr(_keyboard.DynamicMenuBar, 'EnableTop',
        lambda self, index, enabled: self.entries.__setitem__(index, (*self.entries[index][:2], enabled)), raising=False)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda key, identity: (0, 0, identity))
    frame = _keyboard.Frame()
    frame.menu = _keyboard.DynamicMenuBar([('&Library', _keyboard.Menu(frame.items), True)])
    frame.menu_bar = frame.menu
    frame.host, frame.layout = Control(), Layout()
    frame.library = SimpleNamespace(root=tmp_path)
    frame.Bind = Mock()
    frame.Layout = Mock()
    frame.report = Mock()
    frame.IsActive = lambda: True
    frame.working = False
    frame.progress_hub = ProgressHub()
    frame.keyboard_commands = _keyboard.install_keyboard_settings(frame)
    notices = Notifications()
    view = native_progress.OperationProgress(frame, notifications=notices)
    yield frame, view, notices
    view.close()


def test_real_measured_events_drive_bar_and_verified_only_completion_without_focus_moves(native_boundary):
    frame, view, notices = native_boundary
    focus = Control()
    focus.SetFocus()
    task = frame.progress_hub.start('Retaining files', total=4, unit='files')
    for done, percent in ((0, 0), (1, 25), (2, 50), (3, 75), (4, 99)):
        task.update(completed=done, phase='Writing verified bytes')
        view.refresh()
        assert view.gauge.GetValue() == percent
        assert f'{done} of 4 files' in view.text.GetValue()
        assert Control.focus is focus
    task.complete(verified=True)
    view.refresh()
    assert view.gauge.GetValue() == 100 and 'complete' in view.text.GetValue()
    assert notices.events == frame.progress_hub.snapshot()
    view.query()
    assert Control.focus is view.text


def test_indeterminate_cancellation_never_invents_a_percentage(native_boundary):
    frame, view, _ = native_boundary
    task = frame.progress_hub.start('Awaiting an external tool')
    view.refresh()
    assert view.gauge.pulses and 'indeterminate' in view.gauge.GetName()
    assert 'total unknown' in view.text.GetValue() and task.event.percent is None
    pulses = view.gauge.pulses
    task.cancel('Child process stopped.')
    view.refresh()
    assert view.gauge.pulses == pulses and view.gauge.GetValue() == 0
    assert 'cancelled' in view.text.GetValue() and '100%' not in view.text.GetValue()


def test_concurrent_parent_and_subtask_selection_keeps_separate_measurements(native_boundary):
    frame, view, _ = native_boundary
    first = frame.progress_hub.start('First operation', total=10)
    child = first.child('Decode', total=2)
    second = frame.progress_hub.start('Second operation', total=4)
    first.update(completed=2)
    child.update(completed=1)
    second.update(completed=3)
    view.refresh()
    view.operations.SetSelection(view._ids.index(child.operation_id))
    view.select()
    assert view.gauge.GetValue() == 50 and 'Decode' in view.text.GetValue()
    second.update(completed=4)
    view.refresh()
    assert view.gauge.GetValue() == 50 and view.selected_event().operation_id == child.operation_id
    assert first.event.percent == 20 and second.event.percent == 99
    assert any(label.startswith('Subtask: Decode') for label in view.operations.items)


def test_preferences_roundtrip_and_changed_root_leave_both_settings_safe(native_boundary, tmp_path):
    frame, view, notices = native_boundary
    chosen = ProgressPreferences(mode='beep_speech', interval_seconds=.5, volume=.37, muted=True, background=True,
        voice_id='synthetic:chosen', voice_language='uk', voice_fingerprint='a' * 64)
    view.configure(chosen)
    assert load_preferences(tmp_path) == chosen and notices.preferences == chosen
    before = (tmp_path / 'progress-preferences.json').read_bytes()
    other = tmp_path / 'other'
    other.mkdir()
    save_preferences(other, ProgressPreferences(quiet=True))
    frame.library = SimpleNamespace(root=other)
    with pytest.raises(ValueError, match='folder changed'):
        view.configure(ProgressPreferences(), expected_root=tmp_path)
    view.refresh()
    assert view.preferences.quiet and notices.preferences.quiet
    assert (tmp_path / 'progress-preferences.json').read_bytes() == before
    assert load_preferences(other).quiet


def test_failed_persistence_still_mutes_immediately_but_does_not_unmute(native_boundary, monkeypatch):
    _, view, notices = native_boundary
    view.configure(ProgressPreferences(mode='beep'))
    before = (view.root / 'progress-preferences.json').read_bytes()
    def full(*args, **kwargs):
        assert set(kwargs) == {'expected_sha256'}
        raise OSError('Synthetic full disk')
    monkeypatch.setattr(native_progress, 'save_preferences', full)
    view.toggle_mute()
    assert view.preferences.muted and notices.preferences.muted
    assert 'Muted for this session' in view.notice
    view.toggle_mute()
    assert view.preferences.muted and notices.preferences.muted
    assert 'remains muted' in view.notice
    assert (view.root / 'progress-preferences.json').read_bytes() == before


def test_invalid_saved_settings_stay_untouched_and_audio_is_off(native_boundary):
    _, view, notices = native_boundary
    path = view.root / 'progress-preferences.json'
    path.write_bytes(b'{"mode":"unreviewed","version":999}\n')
    view.preferences = view._read_preferences()
    assert view.preferences == ProgressPreferences()
    assert path.read_bytes() == b'{"mode":"unreviewed","version":999}\n'
    assert 'kept' in view.notice


def test_notification_environment_uses_same_shared_media_scope_and_close_is_idempotent(native_boundary):
    frame, view, notices = native_boundary
    with frame.progress_hub.media_activity():
        view.refresh()
        assert notices.environments[-1] == {'foreground': True, 'media_active': True}
    frame.IsActive = lambda: False
    view.refresh()
    assert notices.environments[-1] == {'foreground': False, 'media_active': False}
    view.close()
    view.close()
    assert notices.closes == 1 and view.timer.stops == 1
    before = list(notices.environments)
    view.refresh()
    assert notices.environments == before


def test_off_default_does_not_create_an_audio_thread(native_boundary):
    from carr_library.progress_audio import NotificationWorker
    frame, view, _ = native_boundary
    worker = NotificationWorker()
    task = frame.progress_hub.start('Silent default')
    worker.offer(frame.progress_hub.snapshot())
    assert worker._thread is None and view.preferences.mode == 'off'
    task.cancel()
    worker.close()


def test_semantic_commands_preserve_custom_binding_and_busy_menu_states(native_boundary, monkeypatch):
    frame, existing, _ = native_boundary
    existing.close()
    notices = Notifications()
    def worker(preferences, *, library_root):
        assert library_root == frame.library.root
        return notices
    monkeypatch.setattr(native_progress, 'NotificationWorker', worker)
    view = native_progress.install_operation_progress(frame)
    assert native_progress.install_operation_progress(frame) is view
    frame.keyboard_commands.apply_overrides({'progress.query': 'Ctrl+Alt+Shift+F8'})
    assert frame.keyboard_commands.shortcut('progress.query') == 'Ctrl+Alt+Shift+F8'
    assert all(frame.keyboard_commands.registry.get(spec.command_id) == spec for spec in native_progress.COMMANDS)
    assert len(view.command_items) == 3
    assert frame.progress_view is frame.operation_progress is view
    assert view.hub is frame.progress_hub
    assert all(not spec.default_shortcut for spec in native_progress.COMMANDS)
    options = frame.menu.GetMenu(1)
    original_item = options.items[0]
    original_item.Enable(False)
    before = [item.IsEnabled() for item in options.items]
    view.set_busy(True)
    view.set_busy(True)
    assert not frame.menu.IsEnabledTop(0) and frame.menu.IsEnabledTop(1)
    assert all(frame.keyboard_commands.available(spec.command_id) for spec in native_progress.COMMANDS)
    assert all(not item.IsEnabled() for item in options.items if item not in view.command_items)
    view.set_busy(False)
    assert frame.menu.IsEnabledTop(0)
    assert [item.IsEnabled() for item in options.items] == before
    assert frame.keyboard_commands.shortcut('progress.query') == 'Ctrl+Alt+Shift+F8'
    view.close()


def _host_frame():
    """Real installed DesktopFrame job methods with explicit non-native controls."""
    source = Path(progress_view.__file__).with_name('desktop_app.py')
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    desktop = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    methods = [node for node in desktop.body if isinstance(node, ast.FunctionDef)
               and node.name in ('run_job', '_finish_job_controls')]
    shell = ast.ClassDef(name='Host', bases=[], keywords=[], body=methods, decorator_list=[])
    callbacks = []
    namespace = {'__name__': 'carr_library.ui._progress_host_probe', '__package__': 'carr_library.ui',
                 'wx': SimpleNamespace(CallAfter=callbacks.append)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[shell], type_ignores=[])), str(source), 'exec'), namespace)
    frame = namespace['Host']()
    frame.working = frame.busy = frame._closing = False
    frame.cancel_event = threading.Event()
    frame.cancel_button, frame.choice, frame.book, frame.status = Control(), Control(), Control(), Control()
    frame.menu_bar = SimpleNamespace(GetMenuCount=lambda: 0)
    frame.report = Mock()
    frame.progress_view = SimpleNamespace(set_busy=Mock())
    frame.executor = ThreadPoolExecutor(max_workers=1)
    return frame, callbacks


def _drain(frame, callbacks):
    deadline = time.monotonic() + 40
    while not callbacks and time.monotonic() < deadline:
        time.sleep(.005)
    assert callbacks, 'Synthetic host did not deliver its existing completion callback.'
    while callbacks:
        callbacks.pop(0)()


@pytest.mark.parametrize('kind', ['failed', 'cancelled', 'audio-cancelled'])
def test_actual_background_exceptions_are_distinct_and_context_is_restored(kind):
    from carr_library.audio.jobs import RenderCancelled
    frame, callbacks = _host_frame()
    failures = []
    error = {'failed': ValueError('Synthetic service failure'), 'cancelled': InterruptedError('Stopped'),
             'audio-cancelled': RenderCancelled('Render stopped')}[kind]
    observed = []
    def operation():
        observed.append(current_progress())
        raise error
    try:
        frame.run_job('Synthetic operation', operation, failed=failures.append)
        _drain(frame, callbacks)
        event, = frame.progress_hub.snapshot()
        assert event.state == ('failed' if kind == 'failed' else 'cancelled')
        assert observed[0].operation_id == event.operation_id
        assert failures == [error] and current_progress() is None and not frame.working
        assert frame.executor.submit(current_progress).result(timeout=2) is None
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_actual_returned_commit_survives_late_cancel_and_late_ui_failure(tmp_path):
    frame, callbacks = _host_frame()
    committed, release = threading.Event(), threading.Event()
    path = tmp_path / 'actual-result.txt'
    result = {'status': 'completed'}
    error = ValueError('Synthetic detached UI callback')
    failures = []
    def operation():
        report = current_progress()
        report.update(total=1, completed=0)
        with path.open('x', encoding='utf-8') as output:
            output.write('Verified synthetic result\n')
            output.flush()
            os.fsync(output.fileno())
        report.update(completed=1)
        committed.set()
        assert release.wait(5)
        return result
    def done(value):
        assert value is result and path.read_text() == 'Verified synthetic result\n'
        raise error
    try:
        frame.run_job('Actual publication', operation, done=done, failed=failures.append)
        assert committed.wait(5)
        event, = frame.progress_hub.snapshot()
        assert event.percent == 99
        frame.cancel_event.set()
        release.set()
        _drain(frame, callbacks)
        event, = frame.progress_hub.snapshot()
        assert event.state == 'complete' and event.verified and event.percent == 100
        assert failures == [error] and path.exists()
    finally:
        release.set()
        frame.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('status', ['partial', 'quarantined', 'paused', 'unrecognized-service-state'])
def test_existing_service_pending_outcomes_do_not_announce_completion(status):
    frame, callbacks = _host_frame()
    try:
        frame.run_job('Pending result', lambda: {'status': status})
        _drain(frame, callbacks)
        event, = frame.progress_hub.snapshot()
        assert event.state == 'paused' and not event.verified and event.percent is None
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_actual_submit_failure_closes_reporter_and_restores_controls(monkeypatch):
    frame, callbacks = _host_frame()
    error = RuntimeError('Synthetic executor rejection')
    def rejected(*args):
        raise error
    monkeypatch.setattr(frame.executor, 'submit', rejected)
    failed = Mock()
    try:
        frame.run_job('Rejected operation', Mock(), failed=failed)
        event, = frame.progress_hub.snapshot()
        assert event.state == 'failed' and not frame.working
        failed.assert_called_once_with(error)
        assert frame.choice.IsEnabled() and not frame.cancel_button.IsEnabled()
        assert not callbacks
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_cancelled_queued_future_closes_progress_even_during_host_shutdown(monkeypatch):
    frame, callbacks = _host_frame()
    future = Future()
    monkeypatch.setattr(frame.executor, 'submit', lambda task: future)
    try:
        frame.run_job('Queued operation', Mock())
        frame._closing = True
        future.cancel()
        event, = frame.progress_hub.snapshot()
        assert event.state == 'cancelled' and not callbacks
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_observer_failure_cannot_retry_or_undo_the_returned_result(monkeypatch, tmp_path):
    reporter = ProgressHub().start('Saved result')
    path = tmp_path / 'already-saved'
    def operation():
        path.write_text('retained')
        return {'status': 'completed'}
    def broken(*args):
        raise RuntimeError('Synthetic observer failure')
    monkeypatch.setattr(progress_view, 'finish_result', broken)
    assert progress_view.run_observed(reporter, operation) == {'status': 'completed'}
    assert path.read_text() == 'retained'
    assert reporter.event.state == 'paused' and not reporter.event.verified
    assert current_progress() is None


def test_explicit_failed_service_result_is_not_labeled_complete():
    frame, callbacks = _host_frame()
    try:
        frame.run_job('Rejected domain result', lambda: {'status': 'failed'})
        _drain(frame, callbacks)
        event, = frame.progress_hub.snapshot()
        assert event.state == 'failed' and not event.verified
        assert 'failed' in frame.report.call_args.args[0]
        assert ': complete.' not in frame.report.call_args.args[0]
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_existing_void_commit_contract_uses_domain_completion_after_real_publication(tmp_path):
    frame, callbacks = _host_frame()
    path = tmp_path / 'committed-with-no-return-value.txt'
    def operation():
        with path.open('x', encoding='utf-8') as stream:
            stream.write('Retained user change\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Existing save operations may return None after their commit.
    try:
        frame.run_job('Save local state', operation)
        _drain(frame, callbacks)
        event, = frame.progress_hub.snapshot()
        assert path.read_text() == 'Retained user change\n'
        assert event.state == 'complete' and event.verified
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_operation_terminal_receipt_is_not_completed_twice(monkeypatch, tmp_path):
    reporter = ProgressHub().start('Domain-owned terminal result')
    path = tmp_path / 'terminal-result'
    finish = Mock(side_effect=AssertionError('A terminal reporter must not be finished again.'))
    monkeypatch.setattr(progress_view, 'finish_result', finish)
    def operation():
        path.write_text('Committed result')
        reporter.complete(verified=True)
        return {'status': 'completed'}
    assert progress_view.run_observed(reporter, operation) == {'status': 'completed'}
    assert reporter.event.verified and path.read_text() == 'Committed result'
    finish.assert_not_called()


@pytest.mark.parametrize('observer_error', [KeyError('Evicted terminal event'), RuntimeError('Detached observer')])
def test_actual_worker_result_callback_survives_an_unavailable_progress_observer(tmp_path, monkeypatch, observer_error):
    frame, callbacks = _host_frame()
    path = tmp_path / 'durable-observer-result'
    result = {'status': 'completed', 'artifact': str(path)}
    done, failed = Mock(), Mock()
    def operation():
        with path.open('x', encoding='utf-8') as stream:
            stream.write('Committed before UI observation\n')
            stream.flush()
            os.fsync(stream.fileno())
        return result
    def unavailable(identity):
        raise observer_error
    try:
        frame.run_job('Durable result', operation, done=done, failed=failed)
        # A real queued barrier establishes that the preceding actual worker
        # finished and its UI callback is waiting, without invoking that callback.
        frame.executor.submit(lambda: None).result(timeout=5)
        assert callbacks and path.exists()
        monkeypatch.setattr(frame.progress_hub, 'get', unavailable)
        _drain(frame, callbacks)
        done.assert_called_once_with(result)
        failed.assert_not_called()
        assert path.read_text() == 'Committed before UI observation\n'
        assert frame.status.GetValue() == 'Durable result: operation returned; progress status is unavailable.'
        assert not frame.working
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)
