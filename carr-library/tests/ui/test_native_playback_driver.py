"""Execute failure cleanup against an inactive native-parent model.

The actual Windows key/device probe is separate. This catches the previously
observed MSW EndModal-before-inner-unwind error without pretending to run wx.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import traceback
import sys

import pytest


@pytest.mark.parametrize('release_failure', [False, True])
def test_failure_closes_inner_then_reader_and_retained_outer_loop(release_failure):
    script = Path(__file__).resolve().parents[2] / 'scripts/windows_playback_navigation_smoke.py'
    module = ast.parse(script.read_text(encoding='utf-8'))
    run = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == '_run_native')
    fail = next(n for n in run.body if isinstance(n, ast.FunctionDef) and n.name == 'fail')
    pending, events, errors = [], [], ['original native failure']
    host = SimpleNamespace(view=None)
    class Window:
        shown = True
        def __init__(self, name): self.name = name
        def IsModal(self): return True
        def IsShown(self): return self.shown
        def EndModal(self, result):
            if self.name == 'reader':
                assert host.view is None, 'MSW forbids ending an inactive parent modal'
            self.shown = False; events.append(self.name)
    inner, reader = Window('inner'), Window('reader')
    host.view = inner
    observed = {'host': host, 'reader': reader,
                'outer_loop': SimpleNamespace(ScheduleExit=lambda code: events.append('outer'))}
    def schedule(delay, callback):
        pending.append(callback)
        return SimpleNamespace(Stop=lambda: None)
    def release():
        events.append('release')
        if release_failure: raise RuntimeError('native KeyUp failed')
    env = dict(observed=observed, errors=errors, release=release, traceback=traceback,
               wx=SimpleNamespace(ID_CANCEL=99, CallLater=schedule), monotonic=lambda: 1,
               gui=SimpleNamespace(ExitMainLoop=lambda: events.append('app')))
    exec(compile(ast.Module(body=[fail], type_ignores=[]), str(script), 'exec'), env)
    env['fail']()
    assert events == ['release', 'inner'] and errors[0] == 'original native failure'
    pending.pop(0)()
    assert events == ['release', 'inner'] and len(pending) == 1
    host.view = None  # Actual production ShowModal/finally completion boundary.
    pending.pop(0)()
    assert events == ['release', 'inner', 'reader'] and len(pending) == 1
    pending.pop(0)()
    assert events == ['release', 'inner', 'reader', 'outer', 'app']
    env['fail']()
    assert not pending and events.count('reader') == 1
    assert len(errors) == (2 if release_failure else 1)


def test_failed_native_key_release_retains_the_key_for_final_cleanup_retry():
    script = Path(__file__).resolve().parents[2] / 'scripts/windows_playback_navigation_smoke.py'
    module = ast.parse(script.read_text(encoding='utf-8'))
    run = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == '_run_native')
    release = next(n for n in run.body if isinstance(n, ast.FunctionDef) and n.name == 'release')
    held, calls, successes = [(32, 0)], [], [False, True]
    def key_up(*key): calls.append(key); return successes.pop(0)
    def check(label, condition):
        if not condition: raise AssertionError(label)
    env = dict(held=held, simulator=SimpleNamespace(KeyUp=key_up), check=check)
    exec(compile(ast.Module(body=[release], type_ignores=[]), str(script), 'exec'), env)
    with pytest.raises(AssertionError): env['release']()
    assert held == [(32, 0)]
    env['release']()
    assert held == [None] and calls == [(32, 0), (32, 0)]


@pytest.mark.parametrize('failure', [None, 'activate', 'native'])
def test_watchdogs_cover_runtime_activation_and_native_driver_calls(monkeypatch, failure):
    script = Path(__file__).resolve().parents[2] / 'scripts/windows_playback_navigation_smoke.py'
    module = ast.parse(script.read_text(encoding='utf-8'))
    main = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    events = []
    class Watchdog:
        daemon = False
        def __init__(self, seconds, callback):
            assert seconds == 120
        def start(self):
            assert self.daemon
            events.append('armed')
        def cancel(self): events.append('disarmed')
    def trace(seconds, *, repeat):
        assert seconds == 105 and repeat is False
        events.append('trace armed')
    def activate():
        assert events == ['trace armed', 'armed']
        events.append('activate')
        if failure == 'activate': raise RuntimeError('activation failed')
        return SimpleNamespace(close=lambda: events.append('runtime closed'))
    def native():
        assert events == ['trace armed', 'armed', 'activate']
        events.append('native')
        if failure == 'native': raise RuntimeError('native phase failed')
        return 0
    monkeypatch.setitem(sys.modules, 'carr_library.native_runtime',
                        SimpleNamespace(activate_bundled_runtime=activate))
    env = dict(os=SimpleNamespace(name='nt'), threading=SimpleNamespace(Timer=Watchdog),
               faulthandler=SimpleNamespace(dump_traceback_later=trace,
                   cancel_dump_traceback_later=lambda: events.append('trace disarmed')),
               _run_native=native)
    exec(compile(ast.Module(body=[main], type_ignores=[]), str(script), 'exec'), env)
    if failure:
        with pytest.raises(RuntimeError, match='failed'): env['main']()
    else:
        assert env['main']() == 0
    assert events[-2:] == ['disarmed', 'trace disarmed']
    assert ('runtime closed' in events) is (failure != 'activate')
