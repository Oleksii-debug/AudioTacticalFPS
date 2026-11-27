"""Real bounded preference files, atomic failure and inert backup validation."""
from dataclasses import asdict, replace
import json
import os

import pytest

from carr_library.progress_preferences import (
    MAX_BYTES, ProgressPreferenceError, ProgressPreferences, ProgressPreferenceStore,
    validate_progress_preferences,
)


def test_first_run_is_silent_and_read_only_then_preferences_roundtrip(tmp_path):
    path = tmp_path / 'new-library' / 'progress-preferences.json'
    store = ProgressPreferenceStore(path)
    assert store.load() == ProgressPreferences()
    assert store.load().mode == 'off'
    assert not path.parent.exists()
    preferences = ProgressPreferences('beep', 3.0, 45, False, True, True)
    assert store.save(preferences) == preferences
    assert ProgressPreferenceStore(path).load() == preferences
    assert validate_progress_preferences(path.read_bytes()) == preferences
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('change', [
    {'mode': 'remote'}, {'mode': True}, {'interval_seconds': True},
    {'interval_seconds': 0}, {'interval_seconds': 61}, {'interval_seconds': float('nan')},
    {'interval_seconds': float('inf')}, {'volume': True}, {'volume': -1},
    {'volume': 101}, {'volume': 30.5}, {'muted': 1}, {'allow_background': 0},
    {'quiet_during_media': 'yes'},
])
def test_invalid_preference_values_cannot_be_constructed(change):
    with pytest.raises(ProgressPreferenceError):
        replace(ProgressPreferences(), **change)


@pytest.mark.parametrize('raw', [
    b'', b'{', b'[]', b'\xff', b'{"version":true,"preferences":{}}',
    b'{"version":1,"version":1,"preferences":{}}',
    b'{"version":2,"preferences":{}}', b'{"version":1,"preferences":{},"run":"script"}',
    b' ' * (MAX_BYTES + 1),
])
def test_corrupt_preferences_are_preserved_and_can_never_enable_audio(tmp_path, raw):
    path = tmp_path / 'progress-preferences.json'
    path.write_bytes(raw)
    with pytest.raises(ProgressPreferenceError):
        ProgressPreferenceStore(path).load()
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('interval', [10 ** 400, -(10 ** 400)], ids=['huge-positive', 'huge-negative'])
def test_oversized_json_integer_keeps_native_startup_silent_and_preserves_file(tmp_path, interval):
    from carr_library.ui.progress_panel import NativeProgressPanel
    values = asdict(ProgressPreferences(mode='beep'))
    values['interval_seconds'] = interval
    raw = json.dumps({'version': 1, 'preferences': values}).encode('utf-8')
    assert len(raw) < MAX_BYTES
    path = tmp_path / 'progress-preferences.json'
    path.write_bytes(raw)
    panel = object.__new__(NativeProgressPanel)
    panel.store = ProgressPreferenceStore(path)
    panel.notice = ''
    assert panel._read_preferences() == ProgressPreferences()
    assert 'could not be loaded' in panel.notice
    assert path.read_bytes() == raw
    with pytest.raises(ProgressPreferenceError):
        validate_progress_preferences(raw)


def test_backup_validation_retains_supported_inert_speech_preference_without_provider_calls():
    preferences = ProgressPreferences(mode='beep_speech', volume=0, muted=True)
    raw = json.dumps({'version': 1, 'preferences': asdict(preferences)}).encode()
    assert validate_progress_preferences(raw) == preferences
    for bad in (raw.decode(), bytearray(raw), None):
        with pytest.raises(ProgressPreferenceError):
            validate_progress_preferences(bad)


def test_caller_and_file_are_unchanged_after_failed_atomic_replacement(tmp_path, monkeypatch):
    path = tmp_path / 'progress-preferences.json'
    store = ProgressPreferenceStore(path)
    previous = ProgressPreferences(mode='beep', volume=20)
    store.save(previous)
    raw = path.read_bytes()

    def fail(*args):
        raise OSError('Synthetic disk full')
    monkeypatch.setattr('carr_library.progress_preferences.os.replace', fail)
    with pytest.raises(ProgressPreferenceError, match='disk full'):
        store.save(replace(previous, volume=80))
    assert path.read_bytes() == raw
    assert previous.volume == 20
    assert not list(tmp_path.glob('.*.tmp'))


def test_other_window_cannot_be_overwritten_by_stale_preferences(tmp_path):
    path = tmp_path / 'progress-preferences.json'
    first, second = ProgressPreferenceStore(path), ProgressPreferenceStore(path)
    first.load()
    second.load()
    newer = ProgressPreferences(mode='beep', muted=True)
    first.save(newer)
    with pytest.raises(ProgressPreferenceError, match='another window'):
        second.save(ProgressPreferences(mode='beep', volume=100))
    assert ProgressPreferenceStore(path).load() == newer


def test_corrupt_preferences_repair_detects_an_intervening_external_repair(tmp_path):
    path = tmp_path / 'progress-preferences.json'
    path.write_bytes(b'broken')
    stale = ProgressPreferenceStore(path)
    with pytest.raises(ProgressPreferenceError):
        stale.load()
    repaired = ProgressPreferences(mode='off', muted=True)
    ProgressPreferenceStore(path).save(repaired)
    with pytest.raises(ProgressPreferenceError, match='another window'):
        stale.save(ProgressPreferences(mode='beep'))
    assert ProgressPreferenceStore(path).load() == repaired


def test_hardlinked_target_and_lock_cannot_be_used_for_settings(tmp_path):
    original = tmp_path / 'original.json'
    original.write_bytes(b'Preserve unrelated original')
    path = tmp_path / 'progress-preferences.json'
    os.link(original, path)
    with pytest.raises(ProgressPreferenceError, match='hard links'):
        ProgressPreferenceStore(path).load()
    with pytest.raises(ProgressPreferenceError, match='hard links'):
        ProgressPreferenceStore(path).save(ProgressPreferences())
    path.unlink()
    os.link(original, path.with_name(path.name + '.lock'))
    with pytest.raises(ProgressPreferenceError, match='hard links'):
        ProgressPreferenceStore(path).save(ProgressPreferences())
    assert original.read_bytes() == b'Preserve unrelated original'
    assert not path.exists()


def test_external_edit_during_flush_survives_the_settings_attempt(tmp_path, monkeypatch):
    path = tmp_path / 'progress-preferences.json'
    store = ProgressPreferenceStore(path)
    store.save(ProgressPreferences())
    newer = json.dumps({'version': 1, 'preferences': asdict(ProgressPreferences(muted=True))}).encode()
    actual_fsync = os.fsync
    changed = False

    def change_while_flushing(descriptor):
        nonlocal changed
        actual_fsync(descriptor)
        if not changed:
            changed = True
            path.write_bytes(newer)
    monkeypatch.setattr('carr_library.progress_preferences.os.fsync', change_while_flushing)
    with pytest.raises(ProgressPreferenceError, match='before saving'):
        store.save(ProgressPreferences(mode='beep'))
    assert path.read_bytes() == newer
