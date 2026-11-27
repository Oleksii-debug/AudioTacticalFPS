"""Passively preserve typed effect presets in a real complete common4 backup."""
from dataclasses import replace
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library import backup_sidecars
from carr_library.application import LibraryApp
from carr_library.audio import dsp
from carr_library.audio.effect_presets import EffectPresetStore
from carr_library.audio.models import Effect
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import runtime


PREFIX = 'state/studio/effect-presets/'


def selected_library(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека ефектів')
    app.create_workspace('Exact passive effect settings')
    store = EffectPresetStore(app.root)
    first = store.save('Оповідання', (Effect('highpass', frequency=70),))
    saved = store.save('Оповідання — тихе', (
        Effect('highpass', frequency=80),
        Effect('compressor', {'threshold_db': -20, 'ratio': 2.5, 'makeup_db': 0},
               enabled=False)), previous=first)
    assert saved.revision == 2 and store.get(saved.id).fingerprint == saved.fingerprint
    return app, store, saved


def forbid_activation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Preset backup must not activate audio, a voice or a native process.')
    monkeypatch.setattr(dsp.AudioRuntime, '__init__', forbidden)
    monkeypatch.setattr(runtime.VoiceRegistry, '__init__', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def test_real_effect_preset_update_survives_passive_common4_restore(tmp_path, monkeypatch):
    app, store, saved = selected_library(tmp_path)
    name = PREFIX + saved.id + '.json'
    raw = (app.root / name).read_bytes()
    forbid_activation(monkeypatch)
    backup = tmp_path / 'Повна копія'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    entry, = [row for row in manifest['sidecars'] if row['filename'] == name]
    assert entry == dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    restored = LibraryApp(restore_backup(backup, tmp_path / 'Відновлена бібліотека', checksum))
    actual = EffectPresetStore(restored.root).get(saved.id)
    assert actual.to_dict() == saved.to_dict()
    assert actual.fingerprint == saved.fingerprint and actual.revision == 2
    assert actual.effects[1].enabled is False
    assert (app.root / name).read_bytes() == (restored.root / name).read_bytes() == raw
    assert not any(row['filename'].endswith(('.wav', '.mp3', '.m4b'))
                   for row in manifest['sidecars'])


@pytest.mark.parametrize('kind', (
    'schema', 'unknown-field', 'sample-rate', 'effect-kind',
    'disabled-parameter', 'effect-extra-field', 'filename-identity',
))
def test_invalid_typed_preset_is_not_silently_omitted(tmp_path, monkeypatch, kind):
    app, _, saved = selected_library(tmp_path)
    path = app.root / (PREFIX + saved.id + '.json')
    payload = saved.to_dict()
    if kind == 'schema':
        payload['schema'] = True
    elif kind == 'unknown-field':
        payload['audio_path'] = '/external/untrusted.wav'
    elif kind == 'sample-rate':
        payload['sample_rate'] = 0
    elif kind == 'effect-kind':
        payload['effects'][0]['kind'] = 'arbitrary-ffmpeg-filter'
    elif kind == 'disabled-parameter':
        payload['effects'][1]['parameters']['ratio'] = -1
    elif kind == 'effect-extra-field':
        payload['effects'][0]['script'] = 'untrusted'
    else:
        payload['id'] = ('0' if saved.id[0] != '0' else '1') + saved.id[1:]
    raw = encode(payload)
    path.write_bytes(raw)
    forbid_activation(monkeypatch)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists() and path.read_bytes() == raw


@pytest.mark.parametrize('kind', ('duplicate-key', 'oversize', 'unknown-sibling'))
def test_effect_preset_backup_keeps_exact_bounded_namespace(tmp_path, kind):
    app, _, saved = selected_library(tmp_path)
    path = app.root / (PREFIX + saved.id + '.json')
    raw = path.read_bytes()
    if kind == 'duplicate-key':
        value = saved.to_dict()
        raw = (json.dumps(value, ensure_ascii=False)[:-1] + ', "schema": 1}').encode('utf-8')
    elif kind == 'oversize':
        raw += b' ' * (64 * 1024 + 1 - len(raw))
    else:
        path = path.with_name(saved.id + '.json.bak')
    path.write_bytes(raw)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists() and path.read_bytes() == raw


@pytest.mark.parametrize('count', (256, 257))
def test_complete_backup_enforces_the_actual_effect_collection_limit(tmp_path, count):
    app, store, saved = selected_library(tmp_path)
    (store.root / (saved.id + '.json')).unlink()
    for index in range(count):
        value = replace(saved, id=f'{index:032x}', name=f'Preset {index}')
        (store.root / (value.id + '.json')).write_bytes(encode(value.to_dict()))
    backup = tmp_path / 'backup'
    if count == 257:
        with pytest.raises(ValueError, match='256-record'):
            create_backup(app, backup)
        assert not backup.exists()
        assert len(tuple(store.root.glob('*.json'))) == count
    else:
        checksum = create_backup(app, backup)
        restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
        assert len(EffectPresetStore(restored.root).list()) == count


@pytest.mark.parametrize('resign', (False, True))
def test_tampered_effect_presets_cannot_gain_restore_authority(tmp_path, resign):
    app, _, saved = selected_library(tmp_path)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    name = PREFIX + saved.id + '.json'
    changed = saved.to_dict()
    changed['effects'][0]['parameters']['frequency'] = -1
    raw = encode(changed)
    (backup / name).write_bytes(raw)
    if resign:
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        entry, = [row for row in manifest['sidecars'] if row['filename'] == name]
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        payload = encode(manifest)
        (backup / 'manifest.json').write_bytes(payload)
        checksum = sha256(payload).hexdigest()
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(backup, destination, checksum)
    assert not destination.exists() and (backup / name).read_bytes() == raw


def test_actual_preset_cas_update_invalidates_an_in_progress_snapshot(tmp_path, monkeypatch):
    app, store, saved = selected_library(tmp_path)
    name = PREFIX + saved.id + '.json'
    write, updated = backup_sidecars._write, []

    def update_after_copy(stage, relative, raw):
        write(stage, relative, raw)
        if relative == name:
            assert not updated
            updated.append(store.save('Reviewed update', saved.effects,
                                      sample_rate=saved.sample_rate, previous=saved))

    monkeypatch.setattr(backup_sidecars, '_write', update_after_copy)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='changed during backup'):
        create_backup(app, destination)
    assert not destination.exists() and len(updated) == 1
    assert store.get(saved.id).fingerprint == updated[0].fingerprint
