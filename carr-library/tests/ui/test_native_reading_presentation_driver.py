"""Portable failure-path checks for the native probe; no wx execution claim."""
import ast
import ctypes
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location('reading_presentation_native_driver',
    Path(__file__).resolve().parents[2] / 'scripts/windows_reading_presentation_smoke.py')
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


@pytest.mark.parametrize('failed', [False, OSError('native key-up failed')])
def test_key_release_failure_retains_exact_modifier_tuple_for_final_retry(failed):
    held, calls, results = [(65, 2)], [], [failed, True]
    def key_up(*value):
        calls.append(value)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    simulator = SimpleNamespace(KeyUp=key_up)
    with pytest.raises((AssertionError, OSError)):
        driver._release_key(simulator, held)
    assert held == [(65, 2)]
    driver._release_key(simulator, held)
    driver._release_key(simulator, held)
    assert held == [None] and calls == [(65, 2), (65, 2)]


def test_cleanup_releases_keys_and_restores_fixtures_after_a_failed_timer_stop():
    calls, errors = [], []
    def action(name, fail=False):
        def run():
            calls.append(name)
            if fail:
                raise RuntimeError(name)
        return run
    driver._cleanup((action('timer', True), action('key', True),
                     action('restore pickers'), action('destroy owner')), errors)
    assert calls == ['timer', 'key', 'restore pickers', 'destroy owner']
    assert len(errors) == 2 and 'timer' in errors[0] and 'key' in errors[1]


@pytest.mark.parametrize('error', [KeyboardInterrupt, SystemExit])
def test_cleanup_does_not_swallow_process_control_exceptions(error):
    def interrupt():
        raise error()
    with pytest.raises(error):
        driver._cleanup((interrupt,), [])


class Dialog:
    def __init__(self, parent, name, calls, *, modal=True, fail=False):
        self.parent, self.name, self.calls = parent, name, calls
        self.modal, self.fail = modal, fail

    def GetParent(self):
        return self.parent

    def IsModal(self):
        return self.modal

    def EndModal(self, value):
        self.calls.append((self.name, value))
        if self.fail:
            raise RuntimeError('native modal exit failed')
        assert self.modal
        self.modal = False


def setup_windows(*, child_fail=False):
    frame, calls, queue, errors = SimpleNamespace(_closing=False), [], [], []
    reader = Dialog(frame, 'reader', calls)
    child = Dialog(reader, 'appearance', calls, fail=child_fail)
    foreign = Dialog(SimpleNamespace(GetParent=lambda: None), 'foreign', calls)
    windows = [reader, child, foreign]
    wx = SimpleNamespace(Dialog=Dialog, ID_CANCEL=2, GetTopLevelWindows=lambda: windows[:])
    loop = SimpleNamespace(ScheduleExit=lambda value: calls.append(('outer exit', value)))
    return frame, calls, queue, errors, reader, child, foreign, windows, wx, loop


def test_nested_unwind_waits_for_child_destruction_before_ending_reader():
    frame, calls, queue, errors, reader, child, foreign, windows, wx, loop = setup_windows()
    driver._unwind(wx, frame, loop, lambda: calls.append(('release',)), queue.append, errors)
    assert frame._closing and calls == [('release',), ('appearance', 2)] and reader.modal
    queue.pop(0)()  # Child ShowModal returned; its finally has not destroyed it yet.
    assert calls == [('release',), ('appearance', 2)] and reader.modal
    windows.remove(child)
    queue.pop(0)()
    assert calls[-1] == ('reader', 2) and foreign.modal
    windows.remove(reader)
    queue.pop(0)()
    assert calls[-1] == ('outer exit', 1) and not queue and not errors


def test_unwind_still_closes_owned_modal_when_key_release_raises():
    frame, calls, queue, errors, reader, child, foreign, windows, wx, loop = setup_windows()
    def release():
        raise RuntimeError('native key release failed')
    driver._unwind(wx, frame, loop, release, queue.append, errors)
    assert calls == [('appearance', 2)] and reader.modal and foreign.modal
    assert len(errors) == 1 and 'native key release failed' in errors[0]


def test_failed_modal_exit_still_schedules_the_retained_outer_loop_exit():
    frame, calls, queue, errors, reader, child, foreign, windows, wx, loop = setup_windows(child_fail=True)
    driver._unwind(wx, frame, loop, lambda: None, queue.append, errors)
    assert calls == [('appearance', 2), ('outer exit', 1)] and not queue
    assert reader.modal and foreign.modal and 'native modal exit failed' in errors[0]


def test_unwind_has_a_finite_deadline_for_an_owned_child_that_never_destroys():
    frame, calls, queue, errors, reader, child, foreign, windows, wx, loop = setup_windows()
    child.modal = False
    driver._unwind(wx, frame, loop, lambda: None, queue.append, errors)
    for _ in range(150):
        assert len(queue) == 1
        queue.pop(0)()
    assert not queue and calls == [('outer exit', 1)]
    assert reader.modal and foreign.modal and 'did not finish their cleanup' in errors[0]


FOCUS_CASES = [
    ('windows_reading_presentation_smoke.py', 'appearance_closed',
     'appearance_focus_verified', 'reader_closing',
     'appearance close restores reader keyboard focus'),
    ('windows_reading_tabs_smoke.py', 'bookmark_returned',
     'bookmark_focus_verified', 'split_tools_open',
     'native bookmark navigation restores cursor and reader focus'),
]


def focus_driver(case):
    """Execute the actual probe tick/key functions with a controlled HWND focus."""
    filename, initial, verified, following, label = case
    script = Path(__file__).resolve().parents[2] / 'scripts' / filename
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    functions = []
    for name in ('check', 'advance', 'key', 'tick_body'):
        matches = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        assert len(matches) == 1, (filename, name)
        functions.append(matches[0])
    focused, pressed, callbacks, checks, held = [None], [], [], [], [None]
    stage = [initial, 100.0]

    class Control:
        def __init__(self, name):
            self.name, self.cursor = name, 5

        def GetHandle(self):
            return 731

        def Raise(self):
            pass

        def SetFocus(self):
            focused[0] = self

    reader, editor, target = Control('reader'), Control('editor'), Control('next button')
    notebook = Control('notebook')
    focused[0] = editor

    def owner(handle, pointer):
        assert handle == reader.GetHandle()
        pointer._obj.value = os.getpid()
        return 1

    def foreground(handle):
        assert handle == reader.GetHandle()
        return True

    def key_down(code, modifiers):
        pressed.append((focused[0], code, modifiers))
        return True

    controls = {
        'Reading copy (editable; Ctrl+S saves)': editor,
        'Reading copies; Control+Tab changes reading tab': notebook,
        'Bookmarks and paragraph tools': target,
    }
    wx = SimpleNamespace(
        MOD_NONE=0, WXK_SPACE=32, ID_CANCEL=2,
        GetTopLevelParent=lambda control: reader,
        Window=SimpleNamespace(FindFocus=lambda: focused[0]),
        FindWindowById=lambda identity, parent: target,
        CallLater=lambda delay, callback: callbacks.append((delay, callback)))
    environment = {
        'wx': wx, 'ctypes': ctypes, 'wintypes': SimpleNamespace(DWORD=ctypes.c_uint32),
        'os': os, 'monotonic': lambda: 100.0, 'stage': stage, 'deadline': 200.0,
        'held': held, 'errors': [], 'checks': checks,
        'evidence': {'editor': editor, 'bookmark_cursor': 5},
        'user32': SimpleNamespace(GetForegroundWindow=lambda: reader.GetHandle(),
                                  GetWindowThreadProcessId=owner,
                                  SetForegroundWindow=foreground),
        'simulator': SimpleNamespace(KeyDown=key_down),
        'released': lambda: None,
        'named': lambda name, parent: controls[name],
        'window': lambda title: reader if title == 'Voice reader and reading copy' else None,
        'reader_window': lambda: reader,
        'reader_module': SimpleNamespace(caret_offset=lambda control: control.cursor),
    }
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, str(script), 'exec'), environment)
    return SimpleNamespace(
        tick=environment['tick_body'], focused=focused, pressed=pressed,
        callbacks=callbacks, checks=checks, held=held, stage=stage,
        editor=editor, target=target, label=label, verified=verified, following=following)


@pytest.mark.parametrize('case', FOCUS_CASES, ids=('appearance', 'bookmark'))
def test_verified_reader_focus_advances_before_next_control_focus_settlement(case):
    probe = focus_driver(case)
    probe.tick()  # Observe the actual required editor focus once.
    assert probe.checks == [probe.label] and probe.focused[0] is probe.editor
    assert probe.stage[0] == probe.verified and not probe.pressed

    probe.tick()  # Existing native key helper focuses the next control and returns.
    assert probe.focused[0] is probe.target and probe.stage[0] == probe.verified
    assert probe.checks == [probe.label] and not probe.pressed and not probe.callbacks

    probe.tick()  # Next timer turn sends the same actual key without rechecking old focus.
    assert probe.stage[0] == probe.following
    assert probe.pressed == [(probe.target, 32, 0)] and probe.held == [(32, 0)]
    assert probe.checks == [probe.label] and len(probe.callbacks) == 1


@pytest.mark.parametrize('case', FOCUS_CASES, ids=('appearance', 'bookmark'))
def test_missing_initial_reader_focus_still_refuses_before_any_native_key(case):
    probe = focus_driver(case)
    probe.focused[0] = probe.target
    with pytest.raises(AssertionError, match=probe.label):
        probe.tick()
    assert not probe.checks and not probe.pressed and not probe.callbacks
    assert probe.stage[0] == case[1] and probe.focused[0] is probe.target


def test_wrong_bookmark_cursor_still_refuses_despite_correct_reader_focus():
    probe = focus_driver(FOCUS_CASES[1])
    probe.editor.cursor = 6
    with pytest.raises(AssertionError, match=probe.label):
        probe.tick()
    assert not probe.checks and not probe.pressed and not probe.callbacks
    assert probe.stage[0] == 'bookmark_returned' and probe.focused[0] is probe.editor
