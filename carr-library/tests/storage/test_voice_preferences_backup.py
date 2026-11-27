"""Real complete and supported flat backups retain passive voice preferences."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_compat, runtime_settings as settings
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup
from carr_library.keybindings import Command, KeybindingStore
from carr_library.state_recovery import _prepared
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION


NAME = 'state/audio/engines.json'
BINDINGS = {'fixture.preferences': 'Ctrl+F9'}
FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))


def preferences():
    return {'favorites': ['missing:uk'], 'defaults': {'uk': 'missing:uk'},
            'profiles': {'Тихий голос': {'voice_id': 'missing:uk', 'language': 'uk',
                                        'rate': -1, 'volume': 60}}}


def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Voice settings')
    app.add_text(workspace, 'Original', 'Незмінний оригінал. Voice backup original.')
    KeybindingStore(app.root).save(
        [Command('fixture.preferences', 'Synthetic preference fixture command')], BINDINGS)
    settings.save_voice_preferences(app.root / 'state', preferences(),
        expected=settings.load_voice_preferences(app.root / 'state'))
    return app, workspace


def backup(app, folder, flat):
    if flat:
        return FLAT['_flat_backup'](app, folder)
    digest = create_backup(app, folder)
    return read_json(folder / 'manifest.json'), digest


def reseal(folder, manifest, raw, flat):
    (folder / NAME).write_bytes(raw)
    collection = manifest['sidecars'] if flat else manifest['state']
    expected_name = NAME if flat else 'audio/engines.json'
    entry, = [row for row in collection if row['filename'] == expected_name]
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(manifest_raw)
    return sha256(manifest_raw).hexdigest(), manifest_raw


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
def test_backup_restore_keeps_exact_preferences_and_revokes_engine_authority(tmp_path, monkeypatch, flat):
    app, workspace = library(tmp_path)
    before = (app.root / NAME).read_bytes()
    keymap_before = KeybindingStore(app.root).path.read_bytes()
    monkeypatch.setattr(subprocess, 'Popen',
                        lambda *_a, **_k: pytest.fail('Backup launched a voice engine.'))
    folder = tmp_path / 'backup'
    manifest, digest = backup(app, folder, flat)
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    assert manifest['version'] == (4 if flat else 5)
    assert (folder / NAME).read_bytes() == before
    target = restore_backup(folder, tmp_path / 'restored', digest)
    expected = json.loads(before)
    assert read_json(target / NAME) == {**expected, 'restore_requires_revalidation': True}
    assert (app.root / NAME).read_bytes() == (folder / NAME).read_bytes() == before
    assert settings.load_voice_preferences(target / 'state') == preferences()
    assert KeybindingStore(target).load() == BINDINGS
    assert KeybindingStore(target).path.read_bytes() == keymap_before
    assert LibraryApp(target).search(workspace, 'backup')
    with pytest.raises(ValueError, match='explicit revalidation'):
        settings.voice_registry(target / 'state')
    with pytest.raises(ValueError, match='no substitute'):
        settings.resolve_voice_preference((), preferences(), language='uk')
    # Editing passive data on the restored machine cannot renew execution.
    settings.save_voice_preferences(target / 'state',
        {**preferences(), 'favorites': []}, expected=preferences())
    assert read_json(target / NAME)['restore_requires_revalidation'] is True
    repeated = tmp_path / 'repeated'
    repeated_digest = create_backup(LibraryApp(target), repeated)
    reopened = restore_backup(repeated, tmp_path / 'reopened', repeated_digest)
    assert settings.load_voice_preferences(reopened / 'state')['favorites'] == []
    assert read_json(reopened / NAME)['restore_requires_revalidation'] is True


def test_complete_backup_preserves_the_full_runtime_seal_without_external_reads(tmp_path, monkeypatch):
    app, _ = library(tmp_path)
    value = read_json(app.root / NAME)
    value.update(ffmpeg=r'C:\reviewed-audio\bin\ffmpeg.exe',
                 ffprobe=r'C:\reviewed-audio\bin\ffprobe.exe',
                 audio_runtime_manifest_sha256='a' * 64)
    atomic_json(app.root / NAME, value, replace=True)
    before = (app.root / NAME).read_bytes()
    monkeypatch.setattr(settings, 'verify_configured_audio_runtime',
                        lambda *_a, **_k: pytest.fail('Backup inspected an external runtime.'))
    folder = tmp_path / 'sealed-backup'
    digest = create_backup(app, folder)
    target = restore_backup(folder, tmp_path / 'sealed-restored', digest)
    assert read_json(target / NAME) == {**value, 'restore_requires_revalidation': True}
    assert (app.root / NAME).read_bytes() == (folder / NAME).read_bytes() == before
    settings.save_voice_preferences(target / 'state', preferences(), expected=preferences())
    restored = read_json(target / NAME)
    assert restored['audio_runtime_manifest_sha256'] == 'a' * 64
    assert restored['ffmpeg'] == value['ffmpeg'] and restored['ffprobe'] == value['ffprobe']
    assert restored['restore_requires_revalidation'] is True


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('change', ['duplicate-language', 'profile-boolean', 'unknown-authority'])
def test_rehashed_invalid_preferences_reject_restore_and_preserve_backup(tmp_path, flat, change):
    app, _ = library(tmp_path)
    folder = tmp_path / 'invalid'
    manifest, _ = backup(app, folder, flat)
    value = read_json(folder / NAME)
    if change == 'duplicate-language':
        value['voice_preferences']['defaults'] = {'en-US': 'one', 'EN-us': 'another'}
    elif change == 'profile-boolean':
        value['voice_preferences']['profiles']['Тихий голос']['rate'] = True
    else:
        value['voice_preferences']['activate_without_review'] = True
    raw = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
    digest, manifest_raw = reseal(folder, manifest, raw, flat)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, destination, digest)
    assert not destination.exists()
    assert (folder / NAME).read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == manifest_raw


def test_foreign_flat_engine_budget_still_applies_before_preferences_are_decoded():
    with pytest.raises(RestoreError, match='budget'):
        backup_compat.validate_sidecar(None, NAME, b'x' * (backup_compat.ENGINE_SETTINGS_BYTES + 1))


def test_passive_recovery_validates_optional_preferences_without_requiring_them():
    old = {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
           'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    assert _prepared('audio/engines.json', old) == {**old, 'restore_requires_revalidation': True}
    invalid = deepcopy(preferences())
    invalid['profiles']['Тихий голос']['volume'] = -1
    with pytest.raises(ValueError):
        _prepared('audio/engines.json', {**old, 'voice_preferences': invalid})
