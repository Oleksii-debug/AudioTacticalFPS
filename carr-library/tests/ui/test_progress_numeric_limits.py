"""Malformed numeric preferences must fall back safely, not overflow Python."""
import json

import pytest

from carr_library.progress import ProgressHub
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
from carr_library.progress_feedback import progress_frequency, Tone


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
        'tone': lambda: Tone(220, volume=huge),
    }
    with pytest.raises(ValueError):
        actions[case]()


def test_extreme_number_in_saved_preferences_is_a_readable_validation_error(tmp_path):
    path = tmp_path / 'progress-preferences.json'
    store = ProgressPreferenceStore(path)
    store.save(ProgressPreferences())
    payload = json.loads(path.read_bytes())
    payload['preferences']['interval_seconds'] = 10**400
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='interval'):
        store.load()
