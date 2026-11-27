"""Execute the native probe's failure/loop callbacks; not Windows audio proof."""
import ast
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace

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


def owner(loop, calls, name, *, fail=''):
    def action(label):
        def run():
            calls.append(label)
            if label == fail:
                raise RuntimeError('Synthetic failure: ' + label)
        return run
    return SimpleNamespace(cancelled=Event(), dialog=Dialog(loop),
        timer=SimpleNamespace(Stop=action(name + ' timer')),
        close=action(name + ' close'))


@pytest.mark.parametrize('captured', [True, False])
def test_abort_retains_outer_loop_and_original_error_before_owned_modal_unwind(captured, capsys):
    main, nested, foreign_loop = Loop(), Loop(), Loop()
    errors, calls = [], []
    frame = SimpleNamespace(cancel_event=Event())
    overlay, reader = owner(nested, calls, 'overlay'), owner(nested, calls, 'reader')
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
    probe._abort_native_probe(SimpleNamespace(ID_CANCEL=2), application,
        main if captured else None, frame, (overlay, reader), release, errors,
        'original Unicode assertion')
    assert errors == ['original Unicode assertion']
    assert frame.cancel_event.is_set() and overlay.cancelled.is_set() and reader.cancelled.is_set()
    assert calls == ['release', 'overlay timer', 'overlay close', 'reader timer', 'reader close']
    assert overlay.dialog.closed == reader.dialog.closed == [2]
    assert nested.exits == [0, 2, 2] and main.exits == [1]
    assert foreign.dialog.closed == [] and not foreign.cancelled.is_set()


@pytest.mark.parametrize('failure', ['release', 'overlay timer', 'overlay close', 'reader close', 'modal'])
def test_abort_cleanup_failure_does_not_lose_original_error_or_outer_exit(failure):
    main, nested, errors, calls = Loop(), Loop(), [], []
    overlay, reader = owner(nested, calls, 'overlay', fail=failure), owner(nested, calls, 'reader', fail=failure)
    def release():
        calls.append('release')
        if failure == 'release': raise RuntimeError('Synthetic key release failure')
    if failure == 'modal':
        def fail_modal(code): raise RuntimeError('Synthetic modal teardown failure')
        overlay.dialog.EndModal = fail_modal
    probe._abort_native_probe(SimpleNamespace(ID_CANCEL=2), None, main, None,
                              (overlay, reader), release, errors, 'original failure')
    assert errors[0] == 'original failure' and len(errors) == 2
    assert 'Synthetic' in errors[1]
    assert 'overlay close' in calls and 'reader close' in calls
    assert reader.dialog.closed == [2] and main.exits == [1]


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


@pytest.mark.parametrize('failure', ['key', 'overlay close', 'overlay worker', 'service',
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
    assert calls == ['key', 'overlay close', 'overlay worker', 'reader close', 'reader worker',
                     'service', 'frame worker', 'progress', 'destroy', 'yield', 'native']
    assert pressed == set() and namespace['frame'].cancel_event.is_set()
    assert len(errors) == 1 and 'Synthetic cleanup failure: ' + failure in errors[0]
