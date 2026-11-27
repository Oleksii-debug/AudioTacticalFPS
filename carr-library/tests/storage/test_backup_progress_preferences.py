"""Actual durable backup/restore for the shared progress preference producer."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.progress_preferences import (
    FILENAME, MAX_BYTES, ProgressPreferences, ProgressPreferenceStore,
)


@pytest.fixture
def state(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека')
    app.create_workspace('Progress recovery')
    preferences = ProgressPreferences(mode='beep', interval_seconds=7.5, volume=31,
                                      allow_background=True)
    store = ProgressPreferenceStore(app.root / FILENAME)
    store.load()
    store.save(preferences)
    return app, preferences, store


def test_common_backup_restores_exact_progress_preferences_without_starting_feedback(state, tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    app, preferences, store = state
    original = store.path.read_bytes()
    loaded = []
    real_model = backup_sidecars._model

    def model(name):
        loaded.append(name)
        assert name == 'progress_preferences'
        return real_model(name)

    monkeypatch.setattr(backup_sidecars, '_model', model)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    assert manifest['sidecars'] == [dict(filename=FILENAME, size_bytes=len(original),
                                        sha256=sha256(original).hexdigest())]
    restored = restore_backup(backup, tmp_path / 'Відновлена', digest)
    assert loaded
    assert (restored / FILENAME).read_bytes() == original
    assert (backup / FILENAME).read_bytes() == original
    assert store.path.read_bytes() == original
    assert ProgressPreferenceStore(restored / FILENAME).load() == preferences


def test_same_size_progress_preference_tamper_prevents_publishing_restore(state, tmp_path):
    app, _, store = state
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    before = (backup / FILENAME).read_bytes()
    changed = before.replace(b'"volume": 31', b'"volume": 32')
    assert len(changed) == len(before) and changed != before
    (backup / FILENAME).write_bytes(changed)
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='digest'):
        restore_backup(backup, target, digest)
    assert not target.exists()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('invalid', [b'{"version":false}', b' ' * (MAX_BYTES + 1)],
                         ids=['invalid-schema', 'oversized'])
def test_invalid_progress_preferences_are_not_silently_omitted(state, tmp_path, invalid):
    app, _, store = state
    store.path.write_bytes(invalid)
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        create_backup(app, target)
    assert not target.exists()
    assert store.path.read_bytes() == invalid


def test_missing_progress_validator_fails_without_losing_saved_preferences(state, tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    app, _, store = state
    original = store.path.read_bytes()
    real_import = backup_sidecars.import_module

    def missing(name, package):
        if name == '.progress_preferences':
            raise ImportError('Synthetic installation without the progress producer')
        return real_import(name, package)

    monkeypatch.setattr(backup_sidecars, 'import_module', missing)
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='installed data validator'):
        create_backup(app, target)
    assert not target.exists()
    assert store.path.read_bytes() == original
