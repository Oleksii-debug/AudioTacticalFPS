"""Pinned native-call and nested-loop probe boundaries; not Windows execution."""
import ast
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[2] / 'scripts/windows_command_palette_smoke.py'
_spec = importlib.util.spec_from_file_location('native_palette_probe_driver', SOURCE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def driver_tick(namespace):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    function, = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'tick']
    # Execute the actual callback, without importing native wx on this host.
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['tick']


def test_actual_filter_typing_call_satisfies_pinned_wx_bytes_contract():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    call, = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'Text']
    received = []
    def native_text(value):
        if not isinstance(value, bytes):
            raise TypeError("UIActionSimulator.Text(): argument 1 has unexpected type 'str'")
        assert value.isascii()
        received.append(value)
        return True
    assert eval(compile(ast.Expression(call), str(SOURCE), 'eval'),
                {'simulator': SimpleNamespace(Text=native_text)}) is True
    assert received == [b'file']


class Loop:
    def __init__(self): self.exits = []
    def ScheduleExit(self, code=0): self.exits.append(code)
    def Exit(self, code=0): self.exits.append(code)


class Dialog:
    def __init__(self, parent, active, *, failure=False):
        self.parent, self.active, self.failure, self.closed = parent, active, failure, []
    def GetParent(self): return self.parent
    def IsModal(self): return True
    def EndModal(self, code):
        self.closed.append(code)
        if self.failure: raise RuntimeError('Synthetic modal teardown failure')
        # EndModal schedules unwinding. The nested loop remains active until
        # control returns from the current callback, as in the observed boundary.
        self.active.ScheduleExit(code)


@pytest.mark.parametrize('captured', [True, False])
def test_abort_schedules_the_outer_owner_while_modal_loop_is_still_active(captured):
    main, modal = Loop(), Loop()
    frame = SimpleNamespace(cancel_event=Event())
    window, foreign = Dialog(frame, modal), Dialog(object(), Loop())
    calls, errors = [], []
    application = SimpleNamespace(GetMainLoop=lambda: main, ExitMainLoop=lambda: modal.Exit())
    wx = SimpleNamespace(Dialog=Dialog, ID_CANCEL=2, GetTopLevelWindows=lambda: [window, foreign],
                         CallAfter=lambda callback: callback())
    # Show the precise former API pitfall before invoking the actual correction.
    application.ExitMainLoop()
    assert modal.exits == [0] and main.exits == []
    probe._abort_native_probe(wx, application, frame, main if captured else None,
        SimpleNamespace(Stop=lambda: calls.append('timer stopped')),
        lambda: calls.append('key released'), errors, 'original failure')
    assert frame.cancel_event.is_set() and calls == ['timer stopped', 'key released']
    assert window.closed == [2] and foreign.closed == []
    assert modal.exits == [0, 2] and main.exits == [1]
    assert errors == ['original failure']


@pytest.mark.parametrize('failure', ['timer', 'release', 'dialog'])
def test_abort_preserves_original_failure_and_still_schedules_owner_after_cleanup_error(failure):
    main, modal = Loop(), Loop()
    frame = SimpleNamespace(cancel_event=Event())
    window, errors, called = Dialog(frame, modal, failure=failure == 'dialog'), [], []
    def stop():
        called.append('timer')
        if failure == 'timer': raise RuntimeError('Synthetic timer stop failure')
    def release():
        called.append('release')
        if failure == 'release': raise RuntimeError('Synthetic key release failure')
    wx = SimpleNamespace(Dialog=Dialog, ID_CANCEL=2, GetTopLevelWindows=lambda: [window])
    probe._abort_native_probe(wx, SimpleNamespace(GetMainLoop=lambda: main), frame, main,
                              SimpleNamespace(Stop=stop), release, errors, 'original TypeError')
    assert called == ['timer', 'release'] and frame.cancel_event.is_set()
    assert window.closed == [2] and main.exits == [1]
    assert errors[0] == 'original TypeError' and len(errors) == 2
    assert 'Synthetic' in errors[1]


def test_actual_timer_callback_rejects_nested_delivery_and_retains_outer_loop():
    main, nested, calls = Loop(), Loop(), []
    active_owner = [main]
    namespace = {'in_tick': [False], 'owner_loop': [None], 'errors': [],
                 'application': SimpleNamespace(GetMainLoop=lambda: active_owner[0])}
    tick = driver_tick(namespace)
    def step():
        calls.append('stage once')
        active_owner[0] = nested
        tick()  # Native message delivery during the current stage.
    namespace['advance_stage'] = step
    tick()
    assert calls == ['stage once'] and namespace['owner_loop'] == [main]
    assert namespace['in_tick'] == [False]
    tick()
    assert calls == ['stage once', 'stage once'] and namespace['owner_loop'] == [main]


def test_actual_timer_guard_is_released_even_when_stage_raises():
    def failure(): raise RuntimeError('Synthetic callback failure')
    namespace = {'in_tick': [False], 'owner_loop': [Loop()], 'errors': [],
                 'application': None, 'advance_stage': failure}
    tick = driver_tick(namespace)
    with pytest.raises(RuntimeError, match='callback failure'):
        tick()
    assert namespace['in_tick'] == [False]


def test_actual_queued_timer_callback_cannot_continue_after_abort():
    namespace = {'in_tick': [False], 'owner_loop': [None], 'errors': ['original failure']}
    # Missing application/advance_stage is intentional: neither may be touched.
    driver_tick(namespace)()
    assert namespace['owner_loop'] == [None] and namespace['in_tick'] == [False]
