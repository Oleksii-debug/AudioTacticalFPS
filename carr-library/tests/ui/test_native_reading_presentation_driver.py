"""Portable failure-path checks for the native probe; no wx execution claim."""
import importlib.util
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
