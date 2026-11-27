"""Canonical saved progress preferences through the current shared owner."""
from dataclasses import replace
import json

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup
from carr_library import progress


def canonical(**changes):
    value = {'mode': 'beep_speech', 'interval_seconds': 17.0, 'volume': 37,
             'muted': True, 'allow_background': True, 'quiet_during_media': False}
    value.update(changes)
    return {'version': 1, 'preferences': value}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


@pytest.mark.parametrize('mode', ['off', 'beep', 'speech', 'beep_speech'])
def test_canonical_progress_choices_load_without_rewriting_or_voice_authority(tmp_path, mode):
    raw = encoded(canonical(mode=mode))
    path = tmp_path / progress.FILENAME
    path.write_bytes(raw)
    actual = progress.load_preferences(tmp_path)
    assert actual == progress.ProgressPreferences(mode=mode, interval_seconds=17.0,
        volume=0.37, muted=True, quiet=False, background=True, suppress_during_media=False)
    assert actual.voice_id == actual.voice_language == actual.voice_fingerprint == ''
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path], 'Reading settings must not start any service or write a migration.'
    current = progress.encode_preferences(actual)
    assert json.loads(current)['kind'] == 'carr-progress'
    assert json.loads(current)['version'] == 2
    assert progress.decode_preferences(current) == actual


@pytest.mark.parametrize('interval,volume', [(1, 0), (60, 100), (1.25, 1), (59.75, 99)])
def test_canonical_boundary_values_survive_lossless_current_save(tmp_path, interval, volume):
    path = tmp_path / progress.FILENAME
    raw = encoded(canonical(interval_seconds=interval, volume=volume, muted=False))
    path.write_bytes(raw)
    selected = progress.load_preferences(tmp_path)
    assert selected.interval_seconds == interval and selected.volume == volume / 100
    progress.save_preferences(tmp_path, selected)
    saved = json.loads(path.read_bytes())
    assert saved['version'] == 2 and saved['kind'] == 'carr-progress'
    assert saved['preferences']['interval_seconds'] == interval
    assert saved['preferences']['volume'] == volume / 100
    assert progress.load_preferences(tmp_path) == selected


@pytest.mark.parametrize('damage', [
    'boolean-version', 'version-two-without-kind', 'hybrid-envelope', 'extra-field',
    'missing-field', 'interval-below-canonical-minimum', 'nan-interval', 'infinite-interval',
    'boolean-interval', 'boolean-volume', 'float-volume', 'negative-volume', 'large-volume',
    'integer-mute', 'integer-background', 'integer-media', 'unknown-mode',
    'duplicate-json', 'oversized-canonical',
])
def test_unrecognized_hybrid_and_invalid_canonical_preferences_are_preserved(tmp_path, damage):
    value = canonical()
    choices = value['preferences']
    if damage == 'boolean-version':
        value['version'] = True
    elif damage == 'version-two-without-kind':
        value['version'] = 2
    elif damage == 'hybrid-envelope':
        value['kind'] = 'carr-progress'
    elif damage == 'extra-field':
        choices['voice_id'] = 'unreviewed:voice'
    elif damage == 'missing-field':
        choices.pop('quiet_during_media')
    elif damage == 'interval-below-canonical-minimum':
        choices['interval_seconds'] = 0.5
    elif damage == 'nan-interval':
        choices['interval_seconds'] = float('nan')
    elif damage == 'infinite-interval':
        choices['interval_seconds'] = float('inf')
    elif damage == 'boolean-interval':
        choices['interval_seconds'] = True
    elif damage == 'boolean-volume':
        choices['volume'] = True
    elif damage == 'float-volume':
        choices['volume'] = 37.0
    elif damage == 'negative-volume':
        choices['volume'] = -1
    elif damage == 'large-volume':
        choices['volume'] = 101
    elif damage == 'integer-mute':
        choices['muted'] = 1
    elif damage == 'integer-background':
        choices['allow_background'] = 1
    elif damage == 'integer-media':
        choices['quiet_during_media'] = 1
    elif damage == 'unknown-mode':
        choices['mode'] = 'automatic-playback'
    raw = encoded(value)
    if damage == 'duplicate-json':
        raw = b'{"version":1,' + raw[1:]
    if damage == 'oversized-canonical':
        raw += b' ' * (8193 - len(raw))
    path = tmp_path / progress.FILENAME
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        progress.load_preferences(tmp_path)
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('mode', ['off', 'beep_speech'])
def test_complete_backup_preserves_exact_canonical_settings_then_explicit_save_uses_current_schema(
        tmp_path, mode):
    app = LibraryApp(tmp_path / 'source-library')
    workspace = app.create_workspace('Canonical progress recovery')
    app.add_text(workspace, 'Unchanged source', 'Synthetic retained evidence. Незмінний текст.')
    before_catalog = app.browse(workspace)
    raw = encoded(canonical(mode=mode))
    (app.root / progress.FILENAME).write_bytes(raw)
    first = tmp_path / 'first-backup'
    first_digest = library_backup.create_backup(app, first)
    library_backup.verify_backup(first, first_digest)
    restored_root = library_backup.restore_backup(first, tmp_path / 'restored-library', first_digest)
    restored = LibraryApp(restored_root)
    assert (restored.root / progress.FILENAME).read_bytes() == raw
    assert progress.load_preferences(restored.root).mode == mode
    assert restored.browse(workspace) == before_catalog
    second = tmp_path / 'second-backup'
    second_digest = library_backup.create_backup(restored, second)
    library_backup.verify_backup(second, second_digest)
    twice = library_backup.restore_backup(second, tmp_path / 'twice-restored', second_digest)
    assert (twice / progress.FILENAME).read_bytes() == raw
    explicit = replace(progress.load_preferences(twice), muted=False)
    progress.save_preferences(twice, explicit)
    assert progress.load_preferences(twice) == explicit
    assert json.loads((twice / progress.FILENAME).read_bytes())['version'] == 2
    assert (app.root / progress.FILENAME).read_bytes() == raw
    assert (restored.root / progress.FILENAME).read_bytes() == raw


def test_failed_explicit_migration_preserves_original_canonical_bytes(tmp_path, monkeypatch):
    path = tmp_path / progress.FILENAME
    raw = encoded(canonical())
    path.write_bytes(raw)
    selected = progress.load_preferences(tmp_path)
    def refuse(*args, **kwargs):
        raise OSError('Synthetic atomic publication failure.')
    monkeypatch.setattr(progress.os, 'replace', refuse)
    with pytest.raises(OSError, match='publication failure'):
        progress.save_preferences(tmp_path, replace(selected, muted=False))
    assert path.read_bytes() == raw and progress.load_preferences(tmp_path) == selected
    assert not list(tmp_path.glob('.progress-*.tmp'))


@pytest.mark.parametrize('version', [1, 2])
def test_current_schema_one_and_two_remain_unambiguous(tmp_path, version):
    expected = progress.ProgressPreferences(mode='beep', interval_seconds=0.5, volume=0.375,
        muted=False, quiet=True, background=True, suppress_during_media=False)
    value = json.loads(progress.encode_preferences(expected))
    value['version'] = version
    if version == 1:
        for field in ('voice_id', 'voice_language', 'voice_fingerprint'):
            value['preferences'].pop(field)
    raw = encoded(value)
    path = tmp_path / progress.FILENAME
    path.write_bytes(raw)
    assert progress.load_preferences(tmp_path) == expected
    assert path.read_bytes() == raw
