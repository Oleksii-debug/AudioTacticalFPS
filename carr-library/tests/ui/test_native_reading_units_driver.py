"""Execute probe cleanup failures; portable evidence, not a Windows key run."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


_path = Path(__file__).resolve().parents[2] / 'scripts/windows_reading_units_smoke.py'
_spec = importlib.util.spec_from_file_location('carr_native_reading_units_probe', _path)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def check(label, condition):
    assert condition, label


@pytest.mark.parametrize('first_failure', ['false', 'exception'])
def test_actual_release_helper_retains_exact_key_and_modifiers_until_success(first_failure):
    held, calls = [(83, 2)], []
    def release(*key):
        calls.append(key)
        if len(calls) == 1:
            if first_failure == 'exception':
                raise RuntimeError('Synthetic native key release failure')
            return False
        return True
    simulator = SimpleNamespace(KeyUp=release)
    with pytest.raises((AssertionError, RuntimeError)):
        probe._release_held_key(held, simulator, check)
    assert held == [(83, 2)]
    probe._release_held_key(held, simulator, check)
    assert calls == [(83, 2), (83, 2)] and held == [None]
    probe._release_held_key(held, simulator, check)
    assert len(calls) == 2


@pytest.mark.parametrize('failure', ['', 'release', 'modal'])
def test_abort_closes_owned_reader_and_schedules_outer_even_after_cleanup_failure(failure):
    events, errors = [], ['original selection failure']
    frame = SimpleNamespace(_closing=False)
    def release():
        events.append('release')
        if failure == 'release':
            raise RuntimeError('Synthetic release failure')
    def end_modal(code):
        events.append(('reader modal', code))
        if failure == 'modal':
            raise RuntimeError('Synthetic modal failure')
    reader = SimpleNamespace(GetParent=lambda: frame, IsModal=lambda: True, EndModal=end_modal)
    outer = SimpleNamespace(ScheduleExit=lambda code: events.append(('outer exit', code)))
    probe._abort_reader(SimpleNamespace(ID_CANCEL=2), outer, frame, reader, release, errors)
    assert frame._closing
    assert events == ['release', ('reader modal', 2), ('outer exit', 1)]
    assert errors[0] == 'original selection failure'
    assert len(errors) == (2 if failure else 1)
    if failure:
        assert 'Synthetic ' + failure in errors[1]


@pytest.mark.parametrize('reader_kind', ['foreign', 'not_modal', 'absent'])
def test_abort_never_ends_foreign_or_inactive_dialog(reader_kind):
    events, errors = [], ['original failure']
    frame = SimpleNamespace(_closing=False)
    reader = SimpleNamespace(GetParent=lambda: object() if reader_kind == 'foreign' else frame,
        IsModal=lambda: reader_kind != 'not_modal', EndModal=lambda code: events.append('forbidden close'))
    outer = SimpleNamespace(ScheduleExit=lambda code: events.append(('outer', code)))
    probe._abort_reader(SimpleNamespace(ID_CANCEL=2), outer, frame,
        None if reader_kind == 'absent' else reader, lambda: events.append('release'), errors)
    assert events == ['release', ('outer', 1)] and errors == ['original failure']
