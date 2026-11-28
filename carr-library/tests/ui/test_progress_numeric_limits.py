"""Malformed numeric preferences must fall back safely, not overflow Python."""
import json

import pytest

from carr_library.ui.operation_progress import (ProgressPreferences, ProgressTracker,
    load_preferences, progress_frequency, progress_tone, save_preferences)


@pytest.mark.parametrize('case', ['total', 'completed', 'interval', 'volume', 'pitch', 'tone'])
def test_extreme_integers_are_validation_errors(case):
    huge = 10**400
    tracker = ProgressTracker()
    tracker.start('Real measured work', 5)
    actions = {
        'total': lambda: tracker.start('Invalid total', huge),
        'completed': lambda: tracker.update(huge, 5),
        'interval': lambda: ProgressPreferences(interval_seconds=huge),
        'volume': lambda: ProgressPreferences(volume=huge),
        'pitch': lambda: progress_frequency(huge),
        'tone': lambda: progress_tone(0, volume=huge),
    }
    with pytest.raises(ValueError):
        actions[case]()


def test_extreme_number_in_saved_preferences_is_a_readable_validation_error(tmp_path):
    save_preferences(tmp_path, ProgressPreferences())
    path = tmp_path / 'state/ui/progress.json'
    payload = json.loads(path.read_bytes())
    payload['preferences']['interval_seconds'] = 10**400
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='interval'):
        load_preferences(tmp_path)
