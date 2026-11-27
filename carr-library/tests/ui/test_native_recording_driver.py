"""Portable controls for the real native recording probe's callback ownership.

These execute the probe helpers with explicit timer/window protocol fixtures.
Actual wx, FFmpeg, waveOut and device qualification remain in the native probe.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_recording_smoke.py'
SPEC = spec_from_file_location('recording_driver_under_test', SCRIPT)
probe = module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class Timer:
    def __init__(self, callback, *, fail_stop=False):
        self.callback = callback
        self.fail_stop = fail_stop
        self.stops = 0

    def Stop(self):
        self.stops += 1
        if self.fail_stop:
            self.fail_stop = False
            raise RuntimeError('Actual Stop refusal fixture.')

    def queued_notification(self):
        # Native event queues may already contain a notification when Stop runs.
        return self.callback()


class Timers:
    def __init__(self):
        self.values = []
        self.fail_next_stop = False

    def __call__(self, milliseconds, callback):
        assert milliseconds == 25
        timer = Timer(callback, fail_stop=self.fail_next_stop)
        self.fail_next_stop = False
        self.values.append(timer)
        return timer


class Window:
    def __init__(self, parent, handle, *, modal=True, alive=True,
                 title='Local microphone recording'):
        self.parent, self.handle = parent, handle
        self.modal, self.alive, self.title = modal, alive, title

    def __bool__(self):
        return self.alive

    def GetTitle(self):
        assert self.alive
        return self.title

    def GetHandle(self):
        assert self.alive
        return self.handle

    def GetParent(self):
        assert self.alive
        return self.parent

    def IsModal(self):
        assert self.alive
        return self.modal


def test_retired_modal_callbacks_cannot_enter_the_next_native_phase():
    timers, errors, calls = Timers(), [], []
    driver = probe._DriverCallbacks(timers, errors.append)
    previous = driver.schedule(25, lambda: calls.append('old dialog'))
    driver.reset()
    current = driver.schedule(25, lambda: calls.append('current dialog'))
    previous.queued_notification()
    assert calls == [] and previous.stops == 1 and errors == []
    assert id(current) in driver._pending
    current.queued_notification()
    assert calls == ['current dialog'] and driver._pending == {}


def test_reset_attempts_every_timer_and_retains_a_failed_stop_for_retry():
    timers, errors, calls = Timers(), [], []
    driver = probe._DriverCallbacks(timers, errors.append)
    timers.fail_next_stop = True
    failed = driver.schedule(25, lambda: calls.append('failed old timer'))
    other = driver.schedule(25, lambda: calls.append('other old timer'))
    with pytest.raises(RuntimeError, match='timer cleanup failed'):
        driver.reset()
    assert failed.stops == other.stops == 1
    assert driver._pending == {id(failed): failed}
    driver.reset()
    assert failed.stops == 2 and driver._pending == {}
    failed.queued_notification()
    other.queued_notification()
    assert calls == errors == []


def test_callback_failure_is_reported_inside_the_timer_callback():
    timers, errors = Timers(), []
    driver = probe._DriverCallbacks(timers, errors.append)
    def fail():
        raise ValueError('Controlled native driver callback failure.')
    timer = driver.schedule(25, fail)
    timer.queued_notification()
    assert len(errors) == 1 and 'Controlled native driver callback failure.' in errors[0]
    assert 'ValueError' in errors[0] and driver._pending == {}


def test_same_title_closing_window_is_not_the_current_modal():
    parent = object()
    first = Window(parent, 1, modal=False)
    second = Window(parent, 2)
    dead = Window(parent, 3, alive=False)
    foreign = Window(object(), 4)
    selected = probe._live_recording_dialog(
        (first, dead, foreign, second), parent, set(), lambda handle: True)
    assert selected is second


@pytest.mark.parametrize('retired,live', [({7}, {7}), (set(), set())])
def test_retired_or_missing_native_handle_never_receives_input(retired, live):
    parent = object()
    window = Window(parent, 7)
    assert probe._live_recording_dialog(
        (window,), parent, retired, lambda handle: handle in live) is None


def test_ambiguous_owned_modals_are_refused_before_any_input():
    parent = object()
    with pytest.raises(RuntimeError, match='More than one owned'):
        probe._live_recording_dialog(
            (Window(parent, 1), Window(parent, 2)), parent, set(), lambda handle: True)


def test_a_new_window_may_reuse_a_handle_after_observed_retirement():
    parent = object()
    previous = Window(parent, 7, alive=False)
    current = Window(parent, 7)
    assert probe._live_recording_dialog(
        (previous, current), parent, set(), lambda handle: handle == 7) is current


@pytest.mark.parametrize('failure', ['close', 'wait'])
def test_actual_cleanup_drains_owned_worker_before_restoring_fixtures_or_dll(failure):
    import ast
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    cleanup = next(node for node in ast.walk(tree)
        if isinstance(node, ast.Try) and node.body
        and isinstance(node.body[0], ast.Assign)
        and isinstance(node.body[0].value, ast.Call)
        and isinstance(node.body[0].value.func, ast.Name)
        and node.body[0].value.func.id == 'DesktopFrame')
    function = ast.FunctionDef(name='run_actual_cleanup',
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[],
                           defaults=[], vararg=None, kwarg=None),
        body=cleanup.finalbody, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))

    entered, release, stopped, cancelled = Event(), Event(), Event(), Event()
    fixture, original = object(), object()
    dialog_module = SimpleNamespace(RecordingService=fixture)
    recording_module = SimpleNamespace(run_process=fixture)
    monitor_module = SimpleNamespace(DryMonitor=fixture)
    wx = SimpleNamespace(FileDialog=fixture)
    order = []
    executor = ThreadPoolExecutor(max_workers=1)
    executor.cleanup_failures = ()
    def owned_work():
        try:
            assert dialog_module.RecordingService is recording_module.run_process is fixture
            assert monitor_module.DryMonitor is wx.FileDialog is fixture
            entered.set()
            assert release.wait(10)
            assert dialog_module.RecordingService is recording_module.run_process is fixture
            assert monitor_module.DryMonitor is wx.FileDialog is fixture
            order.append('worker quiescent')
        finally:
            stopped.set()
    future = executor.submit(owned_work)
    class Frame:
        def Close(self, *, force):
            assert force and cancelled.is_set()
            release.set()
            if failure == 'close':
                raise OSError('Controlled native Close failure.')
    class NativeHandle:
        def close(self):
            assert stopped.is_set() and future.done()
            future.result()
            assert dialog_module.RecordingService is recording_module.run_process is original
            assert monitor_module.DryMonitor is wx.FileDialog is original
            order.append('DLL context released')
    clock = iter((0, 21))
    namespace = dict(driver=SimpleNamespace(reset=lambda: None),
        captures=[{'cancel': cancelled}], host_executor=executor, host_cancel=cancelled,
        user32=SimpleNamespace(IsWindow=lambda handle: True), host_handle=7,
        frame=Frame(), monotonic=lambda: next(clock),
        app=SimpleNamespace(Yield=lambda: None), check=lambda *args: None,
        monitors=[], service=SimpleNamespace(_broker_instance=None),
        dialog_module=dialog_module, recording_module=recording_module,
        monitor_module=monitor_module, wx=wx, original_factory=original,
        original_runner=original, original_monitor=original, original_chooser=original,
        native_handle=NativeHandle())
    exec(compile(module, str(SCRIPT), 'exec'), namespace)
    try:
        assert entered.wait(10)
        error = OSError if failure == 'close' else TimeoutError
        with pytest.raises(error, match='Close|HWND'):
            namespace['run_actual_cleanup']()
        assert order == ['worker quiescent', 'DLL context released']
        assert future.done() and executor._shutdown and stopped.is_set()
    finally:
        # Assertions above observe the real drain before this emergency fallback.
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
