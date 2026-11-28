"""Canonical finite progress inputs through the current shared progress owner."""
import json
import math

import pytest

from carr_library.progress import (
    FILENAME, MAX_UNITS, ProgressHub, ProgressPreferences,
    load_preferences, save_preferences,
)
from carr_library.progress_audio import cue_wav, progress_frequency


@pytest.mark.parametrize('case', ['total', 'completed', 'interval', 'volume', 'pitch', 'tone'])
def test_extreme_integers_are_validation_errors(case):
    huge = 10**400
    hub = ProgressHub()
    task = hub.start('Real measured work', total=5)
    actions = {
        'total': lambda: hub.start('Invalid total', total=huge),
        'completed': lambda: task.update(completed=huge, total=5),
        'interval': lambda: ProgressPreferences(interval_seconds=huge),
        'volume': lambda: ProgressPreferences(volume=huge),
        'pitch': lambda: progress_frequency(huge),
        'tone': lambda: cue_wav(task.event, volume=huge),
    }
    before = hub.snapshot()
    with pytest.raises(ValueError):
        actions[case]()
    assert hub.snapshot() == before


def test_extreme_number_in_saved_preferences_is_a_readable_validation_error(tmp_path):
    save_preferences(tmp_path, ProgressPreferences())
    path = tmp_path / FILENAME
    payload = json.loads(path.read_bytes())
    payload['preferences']['interval_seconds'] = 10**400
    malformed = json.dumps(payload).encode('utf-8')
    path.write_bytes(malformed)
    with pytest.raises(ValueError, match='interval'):
        load_preferences(tmp_path)
    assert path.read_bytes() == malformed


@pytest.mark.parametrize('total', [0, MAX_UNITS])
def test_count_endpoints_need_verified_completion_before_one_hundred(total):
    hub = ProgressHub()
    task = hub.start('Bounded actual units', total=total)
    task.update(completed=total)
    assert task.event.percent < 100 and not task.event.terminal
    task.complete(verified=True)
    assert task.event.percent == 100 and task.event.completed == total


@pytest.mark.parametrize('field', ['completed', 'total'])
@pytest.mark.parametrize('value', [-1, True, 1.5, MAX_UNITS + 1, 10**400])
def test_invalid_count_preserves_the_existing_event_and_sequence(field, value):
    hub = ProgressHub()
    task = hub.start('Existing measured work', total=MAX_UNITS)
    before = task.event
    with pytest.raises(ValueError):
        task.update(**{field: value}, total_changed_reason='Explicit changed scope.')
    assert task.event is before and hub.snapshot() == (before,)


def test_unknown_total_accepts_the_bound_and_refuses_excess_without_completion():
    task = ProgressHub().start('Measured work with unknown total')
    task.update(completed=MAX_UNITS)
    before = task.event
    assert before.percent is None and before.eta_seconds is None and not before.terminal
    with pytest.raises(ValueError):
        task.update(completed=MAX_UNITS + 1)
    assert task.event is before


def test_largest_supported_total_has_a_finite_measured_eta_and_readable_status():
    now = [0.0]
    hub = ProgressHub(clock=lambda: now[0])
    task = hub.start('Measured upper boundary', total=MAX_UNITS)
    now[0] = 1.0
    event = task.update(completed=1)
    assert math.isfinite(event.eta_seconds)
    assert event.eta_seconds == pytest.approx(MAX_UNITS - 1)
    assert event.percent == 0 and 'estimated' in event.text and not event.terminal
