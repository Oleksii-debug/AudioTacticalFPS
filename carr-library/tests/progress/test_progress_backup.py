"""Full backups retain inert progress preferences; library-only scope omits them."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.progress_preferences import FILENAME, ProgressPreferences, ProgressPreferenceStore


def test_full_backup_restores_exact_explicit_feedback_preferences_without_playback(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Saved preferences')
    preference = ProgressPreferences(mode='beep_speech', volume=43, interval_seconds=7,
                                    muted=True, allow_background=True, quiet_during_media=True)
    path = app.root / FILENAME
    ProgressPreferenceStore(path).save(preference)
    before = path.read_bytes()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['version'] == 3
    assert manifest['sidecars'] == [dict(filename=FILENAME, size_bytes=len(before),
                                         sha256=sha256(before).hexdigest())]
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert (restored / FILENAME).read_bytes() == before == path.read_bytes()
    assert ProgressPreferenceStore(restored / FILENAME).load() == preference


def test_library_only_authorization_does_not_include_global_progress_preferences(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Only this library')
    path = app.root / FILENAME
    ProgressPreferenceStore(path).save(ProgressPreferences(mode='beep'))
    original = path.read_bytes()
    folder = tmp_path / 'scoped'
    digest = create_backup(app, folder, authorized_workspaces={workspace})
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert all(row['filename'] != FILENAME for row in manifest.get('sidecars', ()))
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (restored / FILENAME).exists()
    assert ProgressPreferenceStore(restored / FILENAME).load() == ProgressPreferences()
    assert path.read_bytes() == original


def test_resealed_invalid_feedback_preferences_stop_restore_before_publication(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Preference validation')
    ProgressPreferenceStore(app.root / FILENAME).save(ProgressPreferences())
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    raw = b'{"version":1,"preferences":{"mode":"beep"}}'
    (folder / FILENAME).write_bytes(raw)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['sidecars'][0].update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = json.dumps(manifest).encode('utf-8')
    manifest_path.write_bytes(encoded)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, sha256(encoded).hexdigest())
    assert not destination.exists()
