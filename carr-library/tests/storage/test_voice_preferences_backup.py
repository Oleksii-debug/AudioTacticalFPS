"""Real common4 backup of typed language choices, without engine activation."""
from dataclasses import replace
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library import backup_sidecars, runtime_settings
from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import runtime
from carr_library.speech.voice_preferences import (
    MAX_PREFERENCES_BYTES, SCHEMA, VoicePreferences, VoicePreferenceStore,
)


NAME = 'state/speech/voice-preferences.json'


def selected_library(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека голосів')
    app.create_workspace('Exact saved language choices')
    selected = VoicePreferences(
        defaults=(('uk', 'owner-local:voice-uk'), ('en-us', 'native:voice-en')),
        favorites=('owner-local:voice-uk', 'native:voice-en'))
    store = VoicePreferenceStore(app.root)
    stored, token = store.save(selected, expected_token=None)
    assert stored == selected and store.load() == (selected, token)
    return app, selected, token


def forbid_activation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Preference backup must not activate, discover or probe a voice runtime.')
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    monkeypatch.setattr(runtime.VoiceRegistry, '__init__', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def test_actual_voice_preference_save_common4_backup_restore_is_passive(
        tmp_path, monkeypatch):
    app, selected, token = selected_library(tmp_path)
    raw = (app.root / NAME).read_bytes()
    forbid_activation(monkeypatch)
    backup = tmp_path / 'Повна копія'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    entry, = [row for row in manifest['sidecars'] if row['filename'] == NAME]
    assert entry == dict(filename=NAME, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    assert (backup / NAME).read_bytes() == raw
    restored = LibraryApp(restore_backup(backup, tmp_path / 'Відновлена бібліотека', checksum))
    assert (restored.root / NAME).read_bytes() == raw
    assert (app.root / NAME).read_bytes() == raw
    assert VoicePreferenceStore(restored.root).load() == (selected, token)
    # Historical IDs are inert data. Restoring a choice neither installs a
    # missing voice nor silently substitutes a different declared language.
    with pytest.raises(ValueError, match='unavailable'):
        selected.preferred('uk', ())
    assert not any(row['filename'].startswith(('audio/', 'audio-runtime/', 'models/'))
                   for row in manifest['sidecars'])


@pytest.mark.parametrize('invalid', (
    {'schema': 'unrecognized', 'defaults': {}, 'favorites': []},
    {'schema': SCHEMA, 'defaults': {'UK': 'voice:one'}, 'favorites': []},
    {'schema': SCHEMA, 'defaults': {'uk': 'voice:one'}, 'favorites': ['voice:one', 'voice:one']},
    {'schema': SCHEMA, 'defaults': {}, 'favorites': [], 'runtime_path': '/private/model'},
))
def test_invalid_voice_preferences_block_complete_backup_without_omission(
        tmp_path, monkeypatch, invalid):
    app, _, _ = selected_library(tmp_path)
    raw = encode(invalid)
    (app.root / NAME).write_bytes(raw)
    forbid_activation(monkeypatch)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()
    assert (app.root / NAME).read_bytes() == raw


@pytest.mark.parametrize('malformed', ('duplicate-key', 'oversize'))
def test_voice_preference_backup_keeps_bounded_duplicate_rejecting_json(
        tmp_path, malformed):
    app, _, _ = selected_library(tmp_path)
    if malformed == 'duplicate-key':
        raw = (b'{"schema":"carr-voice-preferences/1","defaults":{},'
               b'"defaults":{"uk":"voice:one"},"favorites":[]}')
    else:
        raw = encode({'schema': SCHEMA, 'defaults': {}, 'favorites': []})
        raw += b' ' * (MAX_PREFERENCES_BYTES + 1 - len(raw))
    (app.root / NAME).write_bytes(raw)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists()
    assert (app.root / NAME).read_bytes() == raw


@pytest.mark.parametrize('sibling', ('voice-preferences-copy.json', 'voice-preferences.json.bak'))
def test_voice_preference_support_does_not_admit_unknown_speech_sidecars(
        tmp_path, sibling):
    app, _, _ = selected_library(tmp_path)
    unexpected = app.root / 'state' / 'speech' / sibling
    unexpected.write_bytes(encode({'schema': SCHEMA, 'defaults': {}, 'favorites': []}))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists() and unexpected.is_file()


def test_voice_preference_same_size_tamper_is_rejected_before_restore(
        tmp_path):
    app, _, _ = selected_library(tmp_path)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    path = backup / NAME
    raw = path.read_bytes()
    changed = raw.replace(b'voice-uk', b'voice-ua')
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(backup, destination, checksum)
    assert not destination.exists() and path.read_bytes() == changed


def test_resigned_invalid_voice_preferences_still_fail_typed_restore(
        tmp_path, monkeypatch):
    app, _, _ = selected_library(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    invalid = encode({'schema': SCHEMA, 'defaults': {'UK': 'voice:one'}, 'favorites': []})
    (backup / NAME).write_bytes(invalid)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    entry, = [row for row in manifest['sidecars'] if row['filename'] == NAME]
    entry.update(size_bytes=len(invalid), sha256=sha256(invalid).hexdigest())
    payload = encode(manifest)
    (backup / 'manifest.json').write_bytes(payload)
    forbid_activation(monkeypatch)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(backup, destination, sha256(payload).hexdigest())
    assert not destination.exists()
    assert (backup / NAME).read_bytes() == invalid


def test_voice_preference_change_under_own_lock_invalidates_common_snapshot(
        tmp_path, monkeypatch):
    app, selected, token = selected_library(tmp_path)
    updated = replace(selected, favorites=tuple(reversed(selected.favorites)))
    write, changed = backup_sidecars._write, []

    def changed_after_copy(stage, name, raw):
        write(stage, name, raw)
        if name == NAME:
            assert not changed
            changed.append(VoicePreferenceStore(app.root).save(
                updated, expected_token=token))

    monkeypatch.setattr(backup_sidecars, '_write', changed_after_copy)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='changed during backup'):
        create_backup(app, destination)
    assert not destination.exists() and len(changed) == 1
    assert VoicePreferenceStore(app.root).load()[0] == updated
