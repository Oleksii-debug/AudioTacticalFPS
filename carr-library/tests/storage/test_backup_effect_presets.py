"""Real backup boundaries for inert effect presets, with no codec execution."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime, Effect
from carr_library.audio.effect_presets import EffectPreset, EffectPresetStore
from carr_library import backup_effect_presets, backup_sidecars
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_backup import restore_state, snapshot_state

RELATIVE = 'studio/effect-presets'
ROUTES = ('library', 'state')


@pytest.fixture
def preset(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    store = EffectPresetStore(app.root)
    value = store.save('Збережені числові налаштування',
                       (Effect('gain', gain_db=-6), Effect('denoise', enabled=False)))
    return app, value


def _snapshot(app, folder, route):
    if route == 'library':
        create_backup(app, folder)
        return folder, json.loads((folder / 'manifest.json').read_bytes()), route
    folder.mkdir()
    return folder, snapshot_state(app.root / 'state', folder), route


def _entries(snapshot):
    return snapshot[1]['sidecars'] if snapshot[2] == 'library' else snapshot[1]


def _name(snapshot, relative):
    return ('state/' if snapshot[2] == 'library' else '') + relative


def _write_snapshot(snapshot, relative, raw):
    name = _name(snapshot, relative)
    path = snapshot[0] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    entries = _entries(snapshot)
    entries[:] = [entry for entry in entries if entry['filename'] != name]
    entries.append({'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})


def _restore(snapshot, target):
    folder, manifest, route = snapshot
    if route == 'library':
        raw = encode(manifest)
        (folder / 'manifest.json').write_bytes(raw)
        return restore_backup(folder, target, sha256(raw).hexdigest())
    restore_state(folder, target / 'state', manifest)
    return target


def _forbid_runtime(*args, **kwargs):
    pytest.fail('Passive preset backup opened a codec, producer or child process.')


@pytest.mark.parametrize('route', ROUTES)
def test_effect_settings_roundtrip_after_original_deletion_without_runtime(
        preset, tmp_path, monkeypatch, route):
    app, value = preset
    relative = RELATIVE + '/' + value.id + '.json'
    original = (app.root / 'state' / relative).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(AudioRuntime, '__init__', _forbid_runtime)
        patch.setattr(EffectPresetStore, '__init__', _forbid_runtime)
        patch.setattr(subprocess, 'Popen', _forbid_runtime)
        saved = _snapshot(app, tmp_path / 'backup', route)
        assert all(not entry['filename'].endswith('.lock') for entry in _entries(saved))
        shutil.rmtree(app.root)
        restored = _restore(saved, tmp_path / 'restored')
    assert (restored / 'state' / relative).read_bytes() == original
    current = EffectPresetStore(restored).get(value.id)
    assert current == value and current.fingerprint == value.fingerprint
    assert current.effects[1].enabled is False
    assert not list(restored.rglob('*.wav'))
    assert not (restored / 'state/studio/.local-origin.json').exists()


@pytest.mark.parametrize('route', ROUTES)
def test_empty_preset_editor_and_its_exact_writer_temporaries_are_valid(tmp_path, route):
    app = LibraryApp(tmp_path / 'library')
    store = EffectPresetStore(app.root)
    # Opening the editor may save no preset; its ordinary writer temporaries
    # must not turn into restored settings or invented records.
    (store.root / '.effect-presets.lock').write_bytes(b'')
    (store.root / '.pending-interrupted').write_bytes(b'not complete JSON')
    saved = _snapshot(app, tmp_path / 'backup', route)
    assert not any('effect-presets/' in entry['filename'] for entry in _entries(saved))
    restored = _restore(saved, tmp_path / 'restored')
    assert EffectPresetStore(restored).list() == ()
    assert not list((restored / 'state/studio/effect-presets').iterdir())


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('phase', ('snapshot', 'restore'))
@pytest.mark.parametrize('damage', ('identity', 'executable-field', 'bypassed-filtergraph'))
def test_rehashed_preset_cannot_bypass_pure_identity_or_numeric_validation(
        preset, tmp_path, monkeypatch, route, phase, damage):
    app, value = preset
    relative = RELATIVE + '/' + value.id + '.json'
    forged = value.to_dict()
    if damage == 'identity':
        forged['id'] = '1' * 32 if value.id != '1' * 32 else '2' * 32
    elif damage == 'executable-field':
        forged['executable'] = str(tmp_path / 'unselected-engine')
    else:
        forged['effects'][1]['parameters'] = {'filtergraph': 'amovie=/unselected/source.wav'}
        assert forged['effects'][1]['enabled'] is False
    raw = encode(forged)
    with monkeypatch.context() as patch:
        patch.setattr(AudioRuntime, '__init__', _forbid_runtime)
        patch.setattr(subprocess, 'Popen', _forbid_runtime)
        if phase == 'snapshot':
            (app.root / 'state' / relative).write_bytes(raw)
            folder = tmp_path / 'bad-backup'
            with pytest.raises(ValueError):
                _snapshot(app, folder, route)
            if route == 'library':
                assert not folder.exists()
        else:
            saved = _snapshot(app, tmp_path / 'backup', route)
            # Supply a matching manifest digest and record hash deliberately:
            # those authenticate the bytes, not a valid or executable preset.
            _write_snapshot(saved, relative, raw)
            target = tmp_path / 'rejected'
            with pytest.raises(ValueError):
                _restore(saved, target)
            assert not target.exists()


@pytest.mark.parametrize('route', ROUTES)
def test_resigned_duplicate_json_keys_are_not_accepted_as_a_preset(preset, tmp_path, route):
    app, value = preset
    saved = _snapshot(app, tmp_path / 'backup', route)
    original = encode(value.to_dict())
    assert original.startswith(b'{')
    _write_snapshot(saved, RELATIVE + '/' + value.id + '.json', b'{"schema":1,' + original[1:])
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        _restore(saved, target)
    assert not target.exists()


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('leaf', ('unlisted.json', 'A' * 32 + '.json',
                                   '.state.lock', '.other.tmp', 'nested/item.json'))
def test_unknown_reserved_files_are_not_silently_omitted_or_restored(
        preset, tmp_path, route, leaf):
    app, value = preset
    relative = RELATIVE + '/' + leaf
    unknown = app.root / 'state' / relative
    unknown.parent.mkdir(parents=True, exist_ok=True)
    raw = encode(value.to_dict())
    unknown.write_bytes(raw)
    with pytest.raises(ValueError):
        _snapshot(app, tmp_path / 'bad-backup', route)
    unknown.unlink()
    if '/' in leaf:
        unknown.parent.rmdir()
    saved = _snapshot(app, tmp_path / 'backup', route)
    _write_snapshot(saved, relative, raw)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        _restore(saved, target)
    assert not target.exists()


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('leaf', ('.effect-presets.lock', '.pending-interrupted'))
def test_writer_temporary_cannot_be_forged_into_manifest_payload(preset, tmp_path, route, leaf):
    app, value = preset
    saved = _snapshot(app, tmp_path / 'backup', route)
    _write_snapshot(saved, RELATIVE + '/' + leaf, encode(value.to_dict()))
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        _restore(saved, target)
    assert not target.exists()


@pytest.mark.parametrize('route', ROUTES)
def test_unlisted_empty_subdirectory_is_rejected_at_both_boundaries(preset, tmp_path, route):
    app, value = preset
    unknown = app.root / 'state' / RELATIVE / 'unlisted'
    unknown.mkdir()
    with pytest.raises(ValueError):
        _snapshot(app, tmp_path / 'bad-backup', route)
    unknown.rmdir()
    saved = _snapshot(app, tmp_path / 'backup', route)
    (saved[0] / _name(saved, RELATIVE) / 'unlisted').mkdir()
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        _restore(saved, target)
    assert not target.exists()


def _fill(app, count):
    folder = app.root / 'state' / RELATIVE
    for path in folder.glob('*.json'):
        path.unlink()
    for number in range(count):
        value = EffectPreset(f'{number:032x}', 'Bounded preset', 24000, ())
        (folder / (value.id + '.json')).write_bytes(encode(value.to_dict()))


@pytest.mark.parametrize('route', ROUTES)
def test_real_collection_boundary_and_257th_record_refusal_before_reads(
        preset, tmp_path, monkeypatch, route):
    app, original = preset
    _fill(app, 256)
    saved = _snapshot(app, tmp_path / 'backup', route)
    restored = _restore(saved, tmp_path / 'restored')
    assert len(EffectPresetStore(restored).list()) == 256
    extra = EffectPreset(f'{256:032x}', 'One too many', 24000, ())
    raw = encode(extra.to_dict())
    (app.root / 'state' / RELATIVE / (extra.id + '.json')).write_bytes(raw)
    real = backup_sidecars.read_regular
    reads = []
    def checked_read(path, maximum, **options):
        if Path(path).parent.name == 'effect-presets':
            reads.append(Path(path))
            pytest.fail('An over-budget collection was read before admission.')
        return real(path, maximum, **options)
    with monkeypatch.context() as patch:
        patch.setattr(backup_sidecars, 'read_regular', checked_read)
        with pytest.raises(ValueError):
            _snapshot(app, tmp_path / 'bad-backup', route)
        _write_snapshot(saved, RELATIVE + '/' + extra.id + '.json', raw)
        target = tmp_path / 'rejected'
        with pytest.raises(ValueError):
            _restore(saved, target)
        assert not target.exists()
    assert reads == []


@pytest.mark.parametrize('route', ROUTES)
def test_exact_64kib_is_preserved_but_growth_is_refused_before_any_record_read(
        preset, tmp_path, monkeypatch, route):
    app, value = preset
    relative = RELATIVE + '/' + value.id + '.json'
    raw = encode(value.to_dict())
    at_limit = raw + b' ' * (65536 - len(raw))
    source = app.root / 'state' / relative
    source.write_bytes(at_limit)
    saved = _snapshot(app, tmp_path / 'backup', route)
    restored = _restore(saved, tmp_path / 'restored')
    assert (restored / 'state' / relative).read_bytes() == at_limit
    source.write_bytes(at_limit + b' ')
    real = backup_sidecars.read_regular
    reads = []
    def checked_read(path, maximum, **options):
        if Path(path).parent.name == 'effect-presets':
            reads.append(Path(path))
            pytest.fail('An oversized preset was opened before admission.')
        return real(path, maximum, **options)
    with monkeypatch.context() as patch:
        patch.setattr(backup_sidecars, 'read_regular', checked_read)
        with pytest.raises(ValueError):
            _snapshot(app, tmp_path / 'bad-backup', route)
        _write_snapshot(saved, relative, at_limit + b' ')
        target = tmp_path / 'rejected'
        with pytest.raises(ValueError):
            _restore(saved, target)
        assert not target.exists()
    assert reads == []


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('phase', ('snapshot', 'restore'))
def test_hard_linked_preset_refusal_preserves_unrelated_original(
        preset, tmp_path, route, phase):
    import os
    app, value = preset
    relative = RELATIVE + '/' + value.id + '.json'
    raw = encode(value.to_dict())
    outside = tmp_path / 'unrelated-original.json'
    outside.write_bytes(raw)
    saved = _snapshot(app, tmp_path / 'backup', route) if phase == 'restore' else None
    path = (saved[0] / _name(saved, relative) if saved else app.root / 'state' / relative)
    path.unlink()
    os.link(outside, path)
    with pytest.raises(ValueError):
        if saved:
            _restore(saved, tmp_path / 'rejected')
        else:
            _snapshot(app, tmp_path / 'bad-backup', route)
    assert outside.read_bytes() == raw
    assert not (tmp_path / 'rejected').exists()


def test_copied_record_hash_is_rechecked_even_for_another_valid_preset(preset, tmp_path):
    app, value = preset
    folder, entries, _ = _snapshot(app, tmp_path / 'backup', 'state')
    changed = EffectPreset(value.id, 'Different valid settings', 24000, ())
    (folder / RELATIVE / (value.id + '.json')).write_bytes(encode(changed.to_dict()))
    with pytest.raises(ValueError, match='verified manifest'):
        backup_effect_presets.validate_collection(folder, entries)


def test_copied_validation_observes_stop_after_the_bounded_read(preset, tmp_path, monkeypatch):
    app, value = preset
    folder, entries, _ = _snapshot(app, tmp_path / 'backup', 'state')
    real = backup_sidecars.read_regular
    reads, stopped = [], [False]
    def stop_after_read(path, maximum, **options):
        raw = real(path, maximum, **options)
        reads.append((Path(path), maximum))
        stopped[0] = True
        return raw
    monkeypatch.setattr(backup_sidecars, 'read_regular', stop_after_read)
    with pytest.raises(InterruptedError, match='cancelled'):
        backup_effect_presets.validate_collection(folder, entries, cancelled=lambda: stopped[0])
    assert len(reads) == 1 and reads[0][1] == 65536
    assert (folder / RELATIVE / (value.id + '.json')).read_bytes() == encode(value.to_dict())


def test_standalone_unrelated_opaque_state_keeps_its_existing_archive_contract(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'unrelated-settings.json').write_bytes(b'opaque historical state')
    (state / '.private-note').write_bytes(b'ordinary dotfile')
    backup = tmp_path / 'backup'
    entries = snapshot_state(state, backup)
    target = tmp_path / 'restored'
    restore_state(backup, target, entries)
    assert (target / 'unrelated-settings.json').read_bytes() == b'opaque historical state'
    assert (target / '.private-note').read_bytes() == b'ordinary dotfile'
