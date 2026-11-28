"""Portable regressions for the native saved-audio probe's owned cleanup.

These execute the exact scoped probe helpers. The real PlaybackOwner and shared
DesktopExecutor are used without opening an audio device; native HWND and wx
loop acknowledgements remain the separate Windows probe's responsibility.
"""
import ast
from concurrent.futures import Future
import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
import traceback

import pytest

from carr_library.application import LibraryApp
from carr_library.speech import _wave_output
from carr_library.speech.player import WavePlayer
from carr_library.ui.audio_playback import PlaybackOwner
from carr_library.ui.worker_lifetime import DesktopExecutor


def helpers(**values):
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_audio_player_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    main, = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    names = ('cleanup_step', 'unwind', 'observe_stage', 'observe_close', 'close_host', 'finish_host', 'result_report')
    functions = []
    for name in names:
        function, = [node for node in main.body if isinstance(node, ast.FunctionDef) and node.name == name]
        functions.append(function)
    env = {'traceback': traceback, 'json': json, 'errors': [],
           'cleanup_state': {'started': False, 'worker_drained': False},
           'scheduled': [], 'owned_modals': [], 'playback_owners': [], 'destroyed': []}
    env.update(values)
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 str(path), 'exec'), env)
    return env


@pytest.mark.parametrize('close_result', ['acknowledged', 'vetoed', 'raised'])
def test_native_saved_queue_failure_always_drains_real_owner_and_shared_executor(tmp_path, close_result):
    library = LibraryApp(tmp_path / 'library')
    cancel, started = Event(), Event()
    events, native_alive, running = [], [True], [True]
    frame = SimpleNamespace(library=library, working=True, busy=False, _closing=False,
                            cancel_event=cancel, status=SimpleNamespace(GetValue=lambda: 'Controlled failure'))
    executor = DesktopExecutor()
    frame.executor = executor
    owner = PlaybackOwner(frame, schedule=lambda *_args: None)
    other = WavePlayer()
    frame._playback_owner = owner
    stream = (tmp_path / 'owned-worker-file.txt').open('w', encoding='utf-8')
    def finalize():
        events.append('worker-finalizer')
        stream.close()
    executor.register_cleanup(finalize)
    def work():
        started.set()
        try:
            assert cancel.wait(5), 'Cleanup did not cancel the real owned worker.'
        finally:
            frame.working = False
    future = executor.submit(work)
    assert started.wait(5)
    def close():
        events.append('close')
        assert running[0] and cancel.is_set() and not frame.working
        assert owner.closed and owner.silence_confirmed
        if close_result == 'raised':
            raise RuntimeError('Controlled actual Close handler failure.')
        if close_result == 'vetoed':
            return False
        frame._closing = True
        executor.shutdown(wait=False, cancel_futures=True)
        native_alive[0] = False
        return True
    frame.Close = close
    def pump(predicate, label, timeout=12, *, allow_errors=False):
        assert allow_errors is True
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, label
            time.sleep(.001)
    def exit_loop():
        assert future.done() and stream.closed
        assert owner.closed and owner.silence_confirmed
        events.append('exit')
        running[0] = False
    checked = []
    def check(label, value):
        checked.append((label, value))
        assert value, label
    original_failure = 'Original required waveOut advancement failed.'
    loop = SimpleNamespace(IsMain=lambda: True, IsRunning=lambda: running[0])
    env = helpers(frame=frame, executor=executor, other=other, host_cancel=cancel,
                  host_handle=77, pump=pump, check=check,
                  app=SimpleNamespace(IsMainLoopRunning=lambda: running[0], ExitMainLoop=exit_loop),
                  user32=SimpleNamespace(IsWindow=lambda handle: native_alive[0] if handle == 77 else False),
                  wx=SimpleNamespace(EventLoopBase=SimpleNamespace(GetActive=lambda: loop)),
                  _wave_output=_wave_output,
                  playback_owners=[owner], errors=[original_failure])
    try:
        env['finish_host'](exit_loop=True)
        assert future.result(timeout=5) is None
        assert stream.closed and not executor.cleanup_failures
        assert owner.closed and owner.silence_confirmed and owner.player.path is None
        assert owner.player._wave is None and other._wave is None
        assert env['cleanup_state']['worker_drained'] is True
        assert events.index('close') < events.index('worker-finalizer') < events.index('exit')
        assert env['errors'][0] == original_failure
        assert checked == [('actual host Close drains saved playback owners and the existing worker',
                            close_result == 'acknowledged')]
        if close_result == 'acknowledged':
            assert env['errors'] == [original_failure] and not native_alive[0]
        else:
            assert len(env['errors']) == 3 and native_alive[0]
            expected = 'refused ordinary Close' if close_result == 'vetoed' else 'Close handler failure'
            assert expected in env['errors'][1]
        before = list(events)
        env['finish_host']()
        assert events == before, 'Repeated outer cleanup must not repeat owner or loop disposal.'
    finally:
        cancel.set()
        owner.close()
        other.close()
        executor.shutdown(wait=True, cancel_futures=True)
        stream.close()


def test_native_saved_queue_unwind_closes_only_one_recorded_running_modal_per_turn():
    calls, pending = [], []
    parent_running, child_running = [False], [True]
    main = SimpleNamespace(IsMain=lambda: True, IsRunning=lambda: False)
    def window(name, handle):
        # Native EndModal requests exit, but its modal/running observations
        # stay true until ShowModal can return after this event batch.
        def end(result):
            calls.append((name, result))
        return SimpleNamespace(GetHandle=lambda: handle, IsModal=lambda: True, EndModal=end)
    parent, child = window('parent', 1), window('child', 2)
    parent_loop = SimpleNamespace(IsMain=lambda: False, IsRunning=lambda: parent_running[0])
    child_loop = SimpleNamespace(IsMain=lambda: False, IsRunning=lambda: child_running[0])
    def immediate(_callback):
        raise AssertionError('An immediate pending event can starve the requested native loop exit.')
    def schedule(delay, callback):
        assert delay > 0, 'The native event batch must get a chance to return.'
        pending.append(callback)
    env = helpers(user32=SimpleNamespace(IsWindow=lambda handle: handle in (1, 2)),
                  wx=SimpleNamespace(ID_CANCEL=5101, CallAfter=immediate), schedule=schedule,
                  owned_modals=[(parent, 1, parent_loop), (child, 2, child_loop)])
    env['unwind']()
    assert calls == [('child', 5101)] and len(pending) == 1
    assert child_running[0] and not parent_running[0] and not main.IsRunning()
    env['unwind']()
    assert calls == [('child', 5101)] and len(pending) == 1
    # A delayed observation before exit must neither repeat EndModal nor
    # terminate the still-suspended recorded parent.
    pending.pop(0)()
    assert calls == [('child', 5101)] and len(pending) == 1
    child_running[0], parent_running[0] = False, True
    pending.pop(0)()
    assert calls == [('child', 5101), ('parent', 5101)] and len(pending) == 1
    env['unwind']()
    assert calls == [('child', 5101), ('parent', 5101)] and len(pending) == 1
    parent_running[0] = False
    pending.pop(0)()
    assert pending == [] and env['errors'] == []
    # A later owned dialog can reuse a Windows HWND after the first closes.
    # Its distinct recorded window/loop must receive its own EndModal request.
    reopened = window('reopened', 2)
    reopened_running = [True]
    reopened_loop = SimpleNamespace(IsMain=lambda: False, IsRunning=lambda: reopened_running[0])
    env['owned_modals'].append((reopened, 2, reopened_loop))
    env['unwind']()
    assert calls == [('child', 5101), ('parent', 5101), ('reopened', 5101)]
    assert len(pending) == 1
    reopened_running[0] = False
    pending.pop(0)()
    assert pending == [] and env['errors'] == []


@pytest.mark.parametrize('mismatch', ['dead-window', 'changed-handle', 'suspended-loop', 'main-loop'])
def test_native_saved_queue_unwind_refuses_unowned_or_inactive_native_state(mismatch):
    calls, pending = [], []
    window = SimpleNamespace(GetHandle=lambda: 2 if mismatch == 'changed-handle' else 1,
                             IsModal=lambda: True, EndModal=calls.append)
    loop = SimpleNamespace(IsMain=lambda: mismatch == 'main-loop',
                           IsRunning=lambda: mismatch != 'suspended-loop')
    env = helpers(user32=SimpleNamespace(IsWindow=lambda handle: mismatch != 'dead-window'),
                  wx=SimpleNamespace(ID_CANCEL=5101, CallAfter=pending.append),
                  owned_modals=[(window, 1, loop)])
    env['unwind']()
    assert calls == [] and pending == [] and env['errors'] == []


@pytest.mark.parametrize('kind', [RuntimeError, InterruptedError, KeyboardInterrupt])
def test_native_saved_queue_cleanup_keeps_primary_failure_and_continues_all_obligations(kind):
    original = 'Original endpoint-dependent assertion failed.'
    observed = []
    failure = kind('Controlled cleanup failure.')
    def broken_display(*_args, **_kwargs):
        raise OSError('Controlled diagnostic stream failure.')
    env = helpers(errors=[original], print=broken_display)
    def broken():
        observed.append('first')
        raise failure
    def remaining():
        observed.append('remaining')
    assert env['cleanup_step']('first owner', broken) is False
    assert env['cleanup_step']('remaining owner', remaining) is True
    assert observed == ['first', 'remaining'] and env['errors'][0] == original
    assert len(env['errors']) == 2 and kind.__name__ in env['errors'][1]


@pytest.mark.parametrize('cleanup_only', [False, True])
def test_native_saved_queue_cleanup_only_does_not_qualify_or_bypass_default_playback(cleanup_only):
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_audio_player_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    main, = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    scope, = [node for node in main.body if isinstance(node, ast.With)
              and any(isinstance(item.context_expr, ast.Call)
                      and isinstance(item.context_expr.func, ast.Name)
                      and item.context_expr.func.id == 'TemporaryDirectory' for item in node.items)]
    scenario, = [node for node in scope.body if isinstance(node, ast.FunctionDef) and node.name == 'scenario']
    requested, closed, checked = [], [], []
    controlled_error = RuntimeError('Exact controlled failure.')
    future = Future()
    future.set_exception(controlled_error)
    owner = SimpleNamespace(closed=True, silence_confirmed=True, player=SimpleNamespace(path=None))
    failure_state = {'owner': owner, 'error': controlled_error,
                     'observed': controlled_error, 'future': future}
    failure_controls, controls = object(), object()
    executor = object()
    frame = SimpleNamespace(_playback_owner=None, working=False, executor=executor, _closing=False,
        keyboard_commands=SimpleNamespace(
            registry=SimpleNamespace(get=lambda key: SimpleNamespace(
                command_id=key, context='main', default_shortcut='')),
            bindings={'audio.playback': SimpleNamespace(item=SimpleNamespace(GetId=lambda: 100))}))
    def dispatch(_event):
        if requested[-1] is controls:
            raise RuntimeError('The unchanged endpoint-dependent playback scenario is still required.')
        assert requested[-1] is failure_controls
    frame.GetEventHandler = lambda: SimpleNamespace(ProcessEvent=dispatch)
    def check(label, value):
        assert value, label
        checked.append(label)
    env = helpers(frame=frame, executor=executor, controlled_failure=failure_state,
                  failure_controls=failure_controls, controls=controls,
                  schedule=lambda delay, callback: requested.append(callback),
                  options=SimpleNamespace(failure_cleanup_only=cleanup_only, installed=True),
                  check=check, checks=checked, identities={'controlled': 'exact'},
                  picker=object(), host_handle=77,
                  user32=SimpleNamespace(IsWindow=lambda handle: handle == 77),
                  wx=SimpleNamespace(EVT_MENU=SimpleNamespace(typeId=1),
                                     CommandEvent=lambda *_args: object()),
                  _wave_output=_wave_output)
    env['finish_host'] = lambda **kwargs: closed.append(kwargs)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[scenario], type_ignores=[])),
                 str(path), 'exec'), env)
    env['scenario']()
    assert requested == ([failure_controls] if cleanup_only else [failure_controls, controls])
    assert closed == [{'exit_loop': True}], 'The cleanup-only return must execute the same host finally.'
    report = env['result_report']()
    assert report['failure_cleanup'] == 'PASS'
    assert report['mode'] == ('failure-cleanup-only' if cleanup_only else 'full-playback')
    for field in ('actual_windows_waveout', 'actual_native_queue_controls', 'instance_volume_isolation'):
        assert report[field] == ('NOT_RUN' if cleanup_only else 'PASS')
    assert report['physical_device'] == report['audible_listening'] == report['nvda'] == 'NOT_RUN'
    if cleanup_only:
        assert env['errors'] == []
    else:
        assert len(env['errors']) == 1
        assert 'endpoint-dependent playback scenario is still required' in env['errors'][0]
