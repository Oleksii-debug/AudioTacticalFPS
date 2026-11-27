"""Execute the native probe's failure/loop callbacks; not Windows audio proof."""
import ast
import importlib.util
from pathlib import Path
from threading import Event
from types import MethodType, SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[2] / 'scripts/windows_epub_reading_smoke.py'
_spec = importlib.util.spec_from_file_location('native_epub_probe_driver', SOURCE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class Loop:
    def __init__(self): self.exits = []
    def ScheduleExit(self, code=0): self.exits.append(code)


class Dialog:
    def __init__(self, loop): self.loop, self.closed = loop, []
    def IsModal(self): return True
    def EndModal(self, code):
        self.closed.append(code)
        # Native modal unwinding completes after this callback returns.
        self.loop.ScheduleExit(code)


class Scheduled:
    def __init__(self, callback):
        self.callback, self.stopped = callback, False

    def Stop(self): self.stopped = True


class NativeQueue:
    ID_CANCEL = 2

    def __init__(self): self.pending = []

    def CallLater(self, delay, callback):
        assert delay == 25
        timer = Scheduled(callback)
        self.pending.append(timer)
        return timer

    def dispatch(self):
        timer = self.pending.pop(0)
        if not timer.stopped:
            timer.callback()


def owner(loop, calls, name, *, fail=''):
    def action(label):
        def run():
            calls.append(label)
            if label == fail:
                raise RuntimeError('Synthetic failure: ' + label)
        return run
    return SimpleNamespace(cancelled=Event(), dialog=Dialog(loop),
        _audio_dialog=None,
        timer=SimpleNamespace(Stop=action(name + ' timer')),
        close=action(name + ' close'))


@pytest.mark.parametrize('captured', [True, False])
def test_abort_retains_outer_loop_and_original_error_before_owned_modal_unwind(captured, capsys):
    main, nested, foreign_loop = Loop(), Loop(), Loop()
    errors, calls = [], []
    frame = SimpleNamespace(cancel_event=Event())
    overlay, reader = owner(nested, calls, 'overlay'), owner(nested, calls, 'reader')
    reader._audio_dialog = overlay
    wx = NativeQueue()
    foreign = owner(foreign_loop, calls, 'foreign')
    application = SimpleNamespace(GetMainLoop=lambda: main,
                                  ExitMainLoop=lambda: nested.ScheduleExit(0))
    # The former API leaves the main loop alive while the modal loop is active.
    application.ExitMainLoop()
    assert nested.exits == [0] and main.exits == []
    def release():
        assert errors == ['original Unicode assertion']
        assert 'FAIL: original Unicode assertion' in capsys.readouterr().out
        calls.append('release')
    stop = probe._abort_native_probe(wx, application,
        main if captured else None, frame, (overlay, reader), release, errors,
        'original Unicode assertion')
    assert errors == ['original Unicode assertion']
    assert frame.cancel_event.is_set() and overlay.cancelled.is_set() and reader.cancelled.is_set()
    assert calls == ['release', 'overlay timer', 'reader timer', 'overlay close']
    assert overlay.dialog.closed == [2] and reader.dialog.closed == []
    assert len(wx.pending) == 1
    wx.dispatch()  # wx can dispatch pending work before the inner loop exits.
    assert reader.dialog.closed == [] and len(wx.pending) == 1
    reader._audio_dialog = None  # Set only after the real overlay.show returns.
    wx.dispatch()
    assert calls == ['release', 'overlay timer', 'reader timer', 'overlay close', 'reader close']
    assert overlay.dialog.closed == reader.dialog.closed == [2]
    assert nested.exits == [0, 2, 2] and main.exits == [1]
    assert foreign.dialog.closed == [] and not foreign.cancelled.is_set()
    stop()


@pytest.mark.parametrize('failure', ['release', 'overlay timer', 'overlay close', 'reader close', 'modal'])
def test_abort_cleanup_failure_does_not_lose_original_error_or_outer_exit(failure):
    main, nested, errors, calls = Loop(), Loop(), [], []
    overlay, reader = owner(nested, calls, 'overlay', fail=failure), owner(nested, calls, 'reader', fail=failure)
    reader._audio_dialog = overlay
    wx = NativeQueue()
    def release():
        calls.append('release')
        if failure == 'release': raise RuntimeError('Synthetic key release failure')
    if failure == 'modal':
        def fail_modal(code): raise RuntimeError('Synthetic modal teardown failure')
        overlay.dialog.EndModal = fail_modal
    probe._abort_native_probe(wx, None, main, None,
                              (overlay, reader), release, errors, 'original failure')
    assert reader.dialog.closed == []
    reader._audio_dialog = None
    wx.dispatch()
    assert errors[0] == 'original failure' and len(errors) == 2
    assert 'Synthetic' in errors[1]
    assert 'overlay close' in calls and 'reader close' in calls
    assert reader.dialog.closed == [2] and main.exits == [1]


def modal_scene():
    """Model wxMSW's active-loop requirement with the actual Reader.close."""
    active, calls = ['overlay'], []

    class NativeDialog:
        def __init__(self, name): self.name, self.requested = name, False
        def IsModal(self): return True
        def EndModal(self, code):
            if active[0] != self.name:
                raise AssertionError('EndModal cannot exit an inactive outer loop.')
            calls.append(self.name + ' EndModal')
            self.requested = True  # Return/clearing ownership happens later.

    overlay = SimpleNamespace(cancelled=Event(), closed=False, dialog=NativeDialog('overlay'))
    def close_overlay():
        if not overlay.closed:
            overlay.closed = True
            overlay.dialog.EndModal(2)
    overlay.close = close_overlay
    reader = SimpleNamespace(cancelled=Event(), closed=False, dialog=NativeDialog('reader'),
        _audio_dialog=overlay, wx=SimpleNamespace(ID_CANCEL=2),
        executor=SimpleNamespace(shutdown=lambda **kwargs: calls.append('reader executor')))
    source = SOURCE.parents[1] / 'src/carr_library/ui/reading_desk.py'
    node = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(node, ast.ClassDef) and node.name == 'ReadingDialog')
    close = next(node for node in node.body if isinstance(node, ast.FunctionDef) and node.name == 'close')
    namespace = {}
    exec(compile(ast.Module(body=[close], type_ignores=[]), str(source), 'exec'), namespace)
    reader.close = MethodType(namespace['close'], reader)
    return active, calls, overlay, reader


def test_real_reader_close_reproduces_inactive_outer_exit_after_child_close_request():
    active, calls, overlay, reader = modal_scene()
    overlay.close()
    assert overlay.closed and overlay.dialog.requested and reader._audio_dialog is overlay
    with pytest.raises(AssertionError, match='inactive outer loop'):
        reader.close()
    # A premature Reader.close has set this flag before native EndModal fails.
    assert reader.closed and 'reader EndModal' not in calls and active == ['overlay']


@pytest.mark.parametrize('observed_overlay', [True, False])
def test_abort_waits_for_actual_show_return_not_closed_flag_or_a_deferred_callback(observed_overlay):
    active, calls, overlay, reader = modal_scene()
    main, wx, errors = Loop(), NativeQueue(), []
    probe._abort_native_probe(wx, None, main, None,
        (overlay if observed_overlay else None, reader), lambda: None, errors, 'original Unicode failure')
    assert overlay.closed and overlay.dialog.requested and not reader.closed
    assert errors == ['original Unicode failure'] and main.exits == [1]
    for _ in range(3):
        wx.dispatch()
        assert not reader.closed and 'reader EndModal' not in calls
        assert len(wx.pending) == 1
    # ReadingDialog.open_audio clears the field only in its show-return finally.
    reader._audio_dialog = None
    active[0] = 'reader'
    wx.dispatch()
    assert reader.closed and reader.dialog.requested and wx.pending == []
    assert errors == ['original Unicode failure']


def test_abort_wait_is_bounded_and_never_forces_an_inactive_parent(monkeypatch):
    active, calls, overlay, reader = modal_scene()
    main, wx, errors, now = Loop(), NativeQueue(), [], [0]
    monkeypatch.setattr(probe, 'monotonic', lambda: now[0])
    probe._abort_native_probe(wx, None, main, None, (overlay, reader),
                              lambda: None, errors, 'original failure')
    now[0] = 10
    wx.dispatch()
    assert len(errors) == 2 and 'modal loop did not return' in errors[1]
    assert not reader.closed and 'reader EndModal' not in calls
    assert active == ['overlay'] and wx.pending == [] and main.exits == [1]


def test_final_cleanup_stops_the_owned_unwind_callback_before_ui_destruction():
    active, calls, overlay, reader = modal_scene()
    main, wx, errors = Loop(), NativeQueue(), []
    stop = probe._abort_native_probe(wx, None, main, None, (overlay, reader),
                                     lambda: None, errors, 'original failure')
    stop()
    reader._audio_dialog = None
    wx.dispatch()
    assert not reader.closed and 'reader EndModal' not in calls
    assert errors == ['original failure'] and wx.pending == []


@pytest.mark.parametrize('failure', ['raise', 'false'])
def test_native_key_release_attempts_every_held_key_after_failure(failure):
    held, calls, errors = {32, 27}, [], []
    def key_up(key):
        calls.append(key)
        if key == 32:
            if failure == 'raise': raise RuntimeError('Synthetic KeyUp exception')
            return False
        return True
    probe._release_native_keys(SimpleNamespace(KeyUp=key_up), held, errors)
    assert set(calls) == {32, 27} and held == set()
    assert len(errors) == 1 and 'Releasing native key 32' in errors[0]


def callbacks(namespace):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name in ('drive', 'guarded')}
    factory = ast.parse('def factory(stage=0, overlay=None, in_drive=False, owner_loop=None):\n    pass').body[0]
    factory.body = [functions['drive'], functions['guarded'],
        *ast.parse('return drive, guarded, lambda: (in_drive, owner_loop)').body]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    return namespace['factory']()


def test_actual_drive_ignores_reentrant_native_delivery_and_captures_owner_once():
    main, nested, scheduled, calls = Loop(), Loop(), [], []
    active_owner = [main]
    namespace = {'errors': [], 'finished': False, 'key_pending': True, 'deadline': 10,
        'gui': SimpleNamespace(GetMainLoop=lambda: active_owner[0]),
        'wx': SimpleNamespace(CallLater=lambda delay, callback: scheduled.append(callback))}
    drive, guarded, state = callbacks(namespace)
    def clock():
        calls.append('stage')
        active_owner[0] = nested
        drive()  # A native call can dispatch another timer callback immediately.
        return 0
    namespace['monotonic'] = clock
    guarded(drive)()
    assert calls == ['stage'] and len(scheduled) == 1
    assert state() == (False, main)
    scheduled.pop()()
    assert calls == ['stage', 'stage'] and len(scheduled) == 1
    assert state() == (False, main)


def test_actual_callback_failure_resets_guard_and_cancels_queued_progress():
    main, scheduled, errors = Loop(), [], []
    namespace = {'errors': errors, 'finished': False, 'key_pending': True, 'deadline': 10,
        'gui': SimpleNamespace(GetMainLoop=lambda: main), 'traceback': probe.traceback,
        'wx': SimpleNamespace(CallLater=lambda delay, callback: scheduled.append(callback))}
    drive, guarded, state = callbacks(namespace)
    def fail_clock(): raise RuntimeError('Synthetic native callback failure')
    def abort(detail):
        probe._abort_native_probe(SimpleNamespace(ID_CANCEL=2), None, state()[1],
                                  None, (), lambda: None, errors, detail)
    namespace.update(monotonic=fail_clock, abort=abort)
    guarded(drive)()
    assert len(errors) == 1 and 'Synthetic native callback failure' in errors[0]
    assert state() == (False, main) and main.exits == [1]
    assert len(scheduled) == 1
    namespace.pop('monotonic')  # A late callback must not touch native/state work.
    scheduled.pop()()
    drive()
    assert errors and scheduled == [] and state() == (False, main)


@pytest.mark.parametrize('failure', ['abort timer', 'key', 'overlay close', 'overlay worker', 'service',
                                    'frame worker', 'progress', 'yield', 'native'])
def test_actual_final_cleanup_reaches_all_owned_resources_after_a_failure(failure):
    calls, errors, pressed = [], [], {32}
    def action(label):
        def run(*args, **kwargs):
            calls.append(label)
            if label == failure: raise RuntimeError('Synthetic cleanup failure: ' + label)
            return True
        return run
    def resource(name):
        return SimpleNamespace(close=action(name + ' close'),
                               executor=SimpleNamespace(shutdown=action(name + ' worker')))
    simulator = SimpleNamespace(KeyUp=action('key'))
    namespace = dict(probe.__dict__, errors=errors,
        abort_cleanup=action('abort timer'),
        release_all=lambda: probe._release_native_keys(simulator, pressed, errors),
        overlay=resource('overlay'), reader=resource('reader'),
        service=SimpleNamespace(close=action('service')),
        frame=SimpleNamespace(cancel_event=Event(), executor=SimpleNamespace(shutdown=action('frame worker')),
                              operation_progress=SimpleNamespace(close=action('progress')), Destroy=action('destroy')),
        gui=SimpleNamespace(Yield=action('yield')),
        native_handle=SimpleNamespace(close=action('native')))
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    final, = [node.finalbody for node in ast.walk(tree) if isinstance(node, ast.Try)
              and node.finalbody and 'PHASE: recorded-reading cleanup returned' in ast.unparse(node.finalbody)]
    exec(compile(ast.Module(body=final, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    assert calls == ['abort timer', 'key', 'overlay close', 'overlay worker', 'reader close', 'reader worker',
                     'service', 'frame worker', 'progress', 'destroy', 'yield', 'native']
    assert pressed == set() and namespace['frame'].cancel_event.is_set()
    assert len(errors) == 1 and 'Synthetic cleanup failure: ' + failure in errors[0]
