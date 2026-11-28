"""Portable failure-path checks for the native probe; no wx execution claim."""
import ast
import ctypes
from ctypes import wintypes
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



def _reading_tabs_transition():
    """Execute the actual key/tick functions, including their focus-yield branch."""
    from carr_library.speech.navigation import native_insertion_offset

    path = Path(__file__).resolve().parents[2] / 'scripts/windows_reading_tabs_smoke.py'
    module = ast.parse(path.read_text(encoding='utf-8'))
    names = {'check', 'advance', 'key', 'tick_body'}
    functions = [node for node in ast.walk(module)
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names and len(functions) == 4
    focus, scheduled, keys = [None], [], []

    class Top:
        def GetHandle(self): return 7
        def Raise(self): pass

    class Control:
        def __init__(self):
            self.cursor, self.value = 5, 'Original reader text'
            self.selection = (0, 0)
        def GetValue(self): return self.value
        def GetStringSelection(self): return self.value[slice(*self.selection)]
        def GetInsertionPoint(self): return self.cursor
        def GetLastPosition(self): return len(self.value)
        def GetRange(self, first, last): return self.value[first:last]
        def SetFocus(self): focus[0] = self

    top, editor, button, notebook = Top(), Control(), Control(), object()
    focus[0] = editor
    controls = {'Reading copy (editable; Ctrl+S saves)': editor,
                'Reading copies; Control+Tab changes reading tab': notebook,
                'Reading bookmarks and paragraph tools': button}
    def owner(handle, pointer):
        assert handle == 7
        pointer._obj.value = os.getpid()
    wx = SimpleNamespace(MOD_NONE=0, MOD_CONTROL=2, WXK_SPACE=32, WXK_END=312,
        GetTopLevelParent=lambda control: top,
        Window=SimpleNamespace(FindFocus=lambda: focus[0]),
        CallLater=lambda delay, action: scheduled.append((delay, action)))
    context = dict(checks=[], errors=[], held=[None], stage=['bookmark_returned', 0],
        deadline=1000, monotonic=lambda: 0, evidence={'bookmark_cursor': 5},
        reader_window=lambda: top, named=lambda name, parent: controls[name],
        reader_module=SimpleNamespace(native_insertion_offset=native_insertion_offset),
        wx=wx, os=os, ctypes=ctypes, wintypes=wintypes,
        user32=SimpleNamespace(GetForegroundWindow=lambda: 7,
            GetWindowThreadProcessId=owner, SetForegroundWindow=lambda handle: True),
        simulator=SimpleNamespace(KeyDown=lambda code, modifiers: keys.append((code, modifiers)) or True),
        released=lambda: None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), str(path), 'exec'), context)
    return context, focus, editor, button, scheduled, keys


def test_native_tabs_focus_return_is_verified_before_focusing_the_next_command():
    context, focus, editor, button, scheduled, keys = _reading_tabs_transition()
    tick = context['tick_body']
    tick()  # Verify the returned editor, then advance its completed assertion.
    tick()  # Actual key() focuses the next button but has not sent any key.
    assert focus[0] is button and not keys and not scheduled
    tick()  # The retained dispatch stage can now send the actual next command.
    assert context['stage'][0] == 'split_tools_open'
    assert keys == [(32, 0)] and scheduled[0][0] == 40
    assert context['checks'] == ['native bookmark navigation restores cursor and reader focus']


@pytest.mark.parametrize('damage', ['cursor', 'focus'])
def test_native_tabs_does_not_bypass_an_incorrect_bookmark_return(damage):
    context, focus, editor, button, scheduled, keys = _reading_tabs_transition()
    if damage == 'cursor':
        editor.cursor = 6
    else:
        focus[0] = button
    with pytest.raises(AssertionError, match='restores cursor and reader focus'):
        context['tick_body']()
    assert context['stage'][0] == 'bookmark_returned' and not keys and not scheduled


@pytest.mark.parametrize('selection_endpoint', [0, 1])
def test_native_tabs_positions_policy_cursor_before_the_actual_save_dispatch(selection_endpoint):
    context, focus, editor, button, scheduled, keys = _reading_tabs_transition()
    editor.value, editor.cursor, editor.selection = 'b', selection_endpoint, (0, 1)
    context['stage'][0] = 'policy_tools_closed'
    saved = []
    wx = context['wx']

    def press(code, modifiers):
        keys.append((code, modifiers))
        if code == wx.WXK_END:
            assert modifiers == wx.MOD_NONE
            editor.cursor = len(editor.value)
            editor.selection = (editor.cursor, editor.cursor)
        elif code == ord('S'):
            assert modifiers == wx.MOD_CONTROL
            assert editor.GetStringSelection() == ''
            saved.append(editor.GetInsertionPoint())
        else:
            pytest.fail('Unexpected command while establishing the saved cursor.')
        return True

    context['simulator'].KeyDown = press
    context['simulator'].KeyUp = lambda *args: True
    context['released'] = lambda: driver._release_key(context['simulator'], context['held'])
    context['tick_body']()
    assert context['stage'][0] == 'policy_cursor_positioned'
    assert keys == [(wx.WXK_END, wx.MOD_NONE)] and not saved
    scheduled.pop(0)[1]()  # Actual driver releases End before dispatching Save.
    context['tick_body']()
    assert context['stage'][0] == 'policy_saved'
    assert saved == [1]
    assert keys == [(wx.WXK_END, wx.MOD_NONE), (ord('S'), wx.MOD_CONTROL)]
    scheduled.pop(0)[1]()
    assert context['held'] == [None] and not scheduled
    assert context['checks'] == [
        'closing category tools restores actual reader keyboard focus',
        'native End positions the cursor after the reviewed selection',
    ]


@pytest.mark.parametrize('damage', ['cursor', 'selection'])
def test_native_tabs_refuses_save_if_the_policy_cursor_was_not_positioned(damage):
    context, focus, editor, button, scheduled, keys = _reading_tabs_transition()
    context['stage'][0] = 'policy_cursor_positioned'
    editor.value, editor.cursor, editor.selection = 'b', 1, (1, 1)
    if damage == 'cursor':
        editor.cursor = 0
    else:
        editor.selection = (0, 1)
    with pytest.raises(AssertionError, match='positions the cursor after the reviewed selection'):
        context['tick_body']()
    assert context['stage'][0] == 'policy_cursor_positioned'
    assert not keys and not scheduled


def test_native_tabs_refuses_close_when_saved_policy_cursor_does_not_match():
    context, focus, editor, button, scheduled, keys = _reading_tabs_transition()
    context['stage'][0] = 'policy_saved'
    context['evidence'].update(policy_round=1, region_id='reviewed-region')
    document = SimpleNamespace(
        to_dict=lambda: {'schema': 'carr.reading-copy.v4'},
        narration_policy={'regions': [{'id': 'reviewed-region'}]}, source_text='')
    context['drafts'] = lambda: (None, [None, SimpleNamespace(document=document, cursor=0)])
    with pytest.raises(AssertionError, match='explicitly positioned saved cursor'):
        context['tick_body']()
    assert context['stage'][0] == 'policy_saved'
    assert not keys and not scheduled



def _reading_appearance_close_transition():
    """Drive the real polling/key path across its deliberate native focus move."""
    path = Path(SPEC.origin)
    module = ast.parse(path.read_text(encoding='utf-8'))
    names = {'check', 'advance', 'key', 'tick_body'}
    functions = [node for node in ast.walk(module)
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names and len(functions) == 4
    focus, appearance, keys, scheduled = [None], [None], [], []

    class Top:
        def GetHandle(self): return 7
        def Raise(self): pass

    class Control:
        def SetFocus(self): focus[0] = self

    reader, editor, close = Top(), Control(), Control()
    focus[0] = editor

    def window(title):
        if title == 'Voice reader and reading copy':
            return reader
        assert title == 'Reading appearance: font, colors and zoom'
        return appearance[0]

    def find(identifier, parent):
        assert identifier == 2 and parent is reader
        return close

    def owner(handle, pointer):
        assert handle == 7
        pointer._obj.value = os.getpid()

    wx = SimpleNamespace(MOD_NONE=0, WXK_SPACE=32, ID_CANCEL=2,
        GetTopLevelParent=lambda control: reader,
        Window=SimpleNamespace(FindFocus=lambda: focus[0]),
        FindWindowById=find,
        CallLater=lambda delay, action: scheduled.append((delay, action)))
    context = dict(checks=[], errors=[], held=[None], stage=['appearance_closed', 0],
        deadline=1000, monotonic=lambda: 0, evidence={'editor': editor},
        window=window, wx=wx, os=os, ctypes=ctypes, wintypes=wintypes,
        user32=SimpleNamespace(GetForegroundWindow=lambda: 7,
            GetWindowThreadProcessId=owner, SetForegroundWindow=lambda handle: True),
        simulator=SimpleNamespace(
            KeyDown=lambda code, modifiers: keys.append((code, modifiers)) or True,
            KeyUp=lambda code, modifiers: True))
    context['released'] = lambda: driver._release_key(context['simulator'], context['held'])
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 str(path), 'exec'), context)
    return context, focus, appearance, editor, close, keys, scheduled


def test_appearance_focus_return_survives_focusing_the_native_close_command():
    context, focus, appearance, editor, close, keys, scheduled = _reading_appearance_close_transition()
    for _ in range(3):
        context['tick_body']()
    assert focus[0] is close and context['stage'][0] == 'reader_closing'
    assert keys == [(32, 0)] and len(scheduled) == 1 and scheduled[0][0] == 40
    assert context['checks'] == ['appearance close restores reader keyboard focus']
    scheduled.pop(0)[1]()  # The actual driver helper releases the dispatched key.
    context['tick_body']()
    assert context['held'] == [None] and not scheduled and keys == [(32, 0)]


@pytest.mark.parametrize('missing_focus', [False, True], ids=['wrong-control', 'no-focus'])
def test_appearance_close_still_refuses_an_incorrect_reader_focus(missing_focus):
    context, focus, appearance, editor, close, keys, scheduled = _reading_appearance_close_transition()
    focus[0] = None if missing_focus else close
    with pytest.raises(AssertionError, match='appearance close restores reader keyboard focus'):
        context['tick_body']()
    assert context['stage'][0] == 'appearance_closed'
    assert not keys and not scheduled and context['held'] == [None]


def test_appearance_close_waits_for_the_owned_child_before_verifying_reader_focus():
    context, focus, appearance, editor, close, keys, scheduled = _reading_appearance_close_transition()
    appearance[0], focus[0] = object(), close
    context['tick_body']()
    assert context['stage'][0] == 'appearance_closed'
    assert not context['checks'] and not keys and not scheduled
    appearance[0], focus[0] = None, editor
    for _ in range(3):
        context['tick_body']()
    assert context['checks'] == ['appearance close restores reader keyboard focus']
    assert context['stage'][0] == 'reader_closing' and keys == [(32, 0)]
