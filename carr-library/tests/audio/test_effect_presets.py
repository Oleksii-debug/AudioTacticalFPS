"""User-visible effect order, bypass, preset ownership and real audio behavior."""
from array import array
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import sys
import wave

import pytest

from carr_library.audio import AudioRuntime, Effect
from carr_library.audio.effect_presets import (EffectChainSession, EffectPreset,
    EffectPresetStore, PresetSaveCommittedError, builtin_presets, checked_chain, default_effect)


def test_chain_edit_history_restores_order_parameters_and_bypass():
    initial = (Effect('gain', gain_db=-6), Effect('compressor', ratio=3))
    session = EffectChainSession(initial)
    session.move(1, -1)
    session.toggle(0)
    session.edit(1, Effect('gain', gain_db=-9))
    changed = session.effects
    assert [effect.kind for effect in changed] == ['compressor', 'gain']
    assert changed[0].enabled is False and changed[1].parameters['gain_db'] == -9
    for _ in range(3):
        session.history()
    assert session.effects == initial
    for _ in range(3):
        session.history(forward=True)
    assert session.effects == changed
    session.history()
    session.remove(0)
    with pytest.raises(ValueError, match='redo'):
        session.history(forward=True)
    assert initial[0].parameters['gain_db'] == -6
    with pytest.raises(TypeError):
        initial[0].parameters['gain_db'] = 7


@pytest.mark.parametrize('bad', [
    Effect('gain', gain_db='1;amovie=/private'),
    Effect('gain', gain_db=float('nan'), enabled=False),
    Effect('equalizer', frequency=16000, enabled=False),
    Effect('gain', parameters={'filtergraph': 'volume=4'}),
    Effect('unknown'), 'gain', {'kind': 'gain'},
])
def test_invalid_edits_leave_the_previous_chain_and_undo_history_intact(bad):
    session = EffectChainSession((Effect('gain', gain_db=-3),))
    before = session.effects
    with pytest.raises(ValueError):
        session.edit(0, bad)
    assert session.effects == before
    with pytest.raises(ValueError, match='undo'):
        session.history()


def test_local_capability_absence_can_be_preserved_bypassed_but_cannot_be_enabled():
    dry = Effect('compressor', ratio=4, enabled=False)
    session = EffectChainSession((dry,), available={'gain'})
    with pytest.raises(ValueError, match='does not support'):
        session.toggle(0)
    assert session.effects == (dry,)
    with pytest.raises(ValueError, match='supported'):
        session.add('compressor')
    session.add('gain')
    assert len(session.effects) == 2


def test_chain_and_history_allowances_are_enforced_without_losing_current_settings():
    session = EffectChainSession((Effect('gain'),))
    for value in range(40):
        session.edit(0, Effect('gain', gain_db=-value - 1))
    for _ in range(32):
        session.history()
    assert session.effects[0].parameters['gain_db'] == -8
    with pytest.raises(ValueError, match='undo'):
        session.history()
    session.set(tuple(Effect('gain') for _ in range(32)))
    with pytest.raises(ValueError, match='32'):
        session.add('gain')
    assert len(session.effects) == 32


@pytest.mark.parametrize('rate', [8000, 16000, 24000, 48000])
def test_lowpass_default_and_reset_are_valid_at_the_selected_output_rate(rate):
    effect = default_effect('lowpass', rate)
    assert 20 <= effect.parameters['frequency'] < rate / 2
    assert checked_chain((effect,), rate) == (effect,)
    session = EffectChainSession(sample_rate=rate)
    session.add('lowpass')
    assert session.effects == (effect,)


def test_presets_roundtrip_and_stale_updates_do_not_overwrite_the_reviewed_owner(tmp_path):
    first = EffectPresetStore(tmp_path)
    second = EffectPresetStore(tmp_path)
    chain = (Effect('highpass', frequency=70), Effect('gain', gain_db=-4, enabled=False))
    created = first.save('Голос — тихіше', chain)
    reviewed = second.get(created.id)
    assert reviewed == created and reviewed.fingerprint == created.fingerprint
    updated = first.save('Голос — нова версія', (Effect('gain', gain_db=-8),), previous=created)
    assert updated.id == created.id and updated.revision == 2
    with pytest.raises(ValueError, match='changed after review'):
        second.save('Stale overwrite', (), previous=reviewed)
    assert first.get(created.id) == updated
    assert len(first.list()) == 1
    assert json.loads((first.root / (created.id + '.json')).read_text(encoding='utf-8')) == updated.to_dict()


@pytest.mark.parametrize('damage', ['id', 'schema', 'extra', 'effects', 'enabled', 'duplicate'])
def test_saved_preset_damage_is_refused_instead_of_executed(tmp_path, damage):
    store = EffectPresetStore(tmp_path)
    original = store.save('Reviewed', (Effect('gain'),))
    path = store.root / (original.id + '.json')
    value = original.to_dict()
    if damage == 'id':
        value['id'] = 'b' * 32 if original.id != 'b' * 32 else 'a' * 32
    elif damage == 'schema':
        value['schema'] = True
    elif damage == 'extra':
        value['executable'] = '/outside/engine'
    elif damage == 'effects':
        value['effects'][0]['parameters'] = {'gain_db': 'amovie=outside.wav'}
    elif damage == 'enabled':
        value['effects'][0]['enabled'] = 1
    if damage == 'duplicate':
        path.write_text('{"schema":1,"schema":1}', encoding='utf-8')
    else:
        path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        store.get(original.id)


@pytest.mark.parametrize('identity', ['../outside', '/absolute', 'a' * 31, 'A' * 32])
def test_preset_identity_cannot_select_another_file(tmp_path, identity):
    store = EffectPresetStore(tmp_path)
    with pytest.raises(ValueError, match='identity'):
        store.get(identity)


def test_linked_preset_is_rejected_and_outside_bytes_are_preserved(tmp_path):
    store = EffectPresetStore(tmp_path)
    saved = store.save('Safe', (Effect('gain'),))
    outside = tmp_path / 'outside.json'
    outside.write_text('{"outside":"unchanged"}', encoding='utf-8')
    destination = store.root / (saved.id + '.json')
    destination.unlink()
    os.link(outside, destination)
    before = outside.read_bytes()
    with pytest.raises(ValueError, match='regular local files'):
        store.get(saved.id)
    assert outside.read_bytes() == before


def test_complete_library_backup_preserves_user_presets_without_generated_audio(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'library')
    store = EffectPresetStore(app.root)
    chain = (Effect('gain', gain_db=-6), Effect('denoise', enabled=False))
    original = store.save('Передача — вечір', chain)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = EffectPresetStore(destination)
    assert restored.get(original.id) == original
    assert restored.get(original.id).fingerprint == original.fingerprint
    assert restored.list() == (original,)
    assert not list((destination / 'state').rglob('*.wav'))


def test_actual_saved_order_changes_audio_and_bypassed_chain_preserves_source_pcm(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual local FFmpeg/ffprobe required.'
    rate = 24000
    values = array('h', (round(.2 * math.sin(2 * math.pi * 440 * n / rate) * 32767)
                         for n in range(2 * rate)))
    if sys.byteorder != 'little':
        values.byteswap()
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'uncompressed'))
        stream.writeframes(values.tobytes())
    before = sha256(source.read_bytes()).hexdigest()
    runtime = AudioRuntime(tmp_path)
    session = EffectChainSession((Effect('gain', gain_db=-30),
        Effect('gate', threshold_db=-30, ratio=8, attack_ms=1, release_ms=20)),
        available=runtime.capabilities()['effects'])
    store = EffectPresetStore(tmp_path)
    saved = store.save('Gain before gate', session.effects)
    quiet = runtime.convert(source, 'gain-first.wav', channels=1, effects=store.get(saved.id).effects)
    session.move(1, -1)
    loud = runtime.convert(source, 'gate-first.wav', channels=1, effects=session.effects)

    def pcm(path):
        with wave.open(str(path), 'rb') as stream:
            data = array('h')
            data.frombytes(stream.readframes(stream.getnframes()))
        if sys.byteorder != 'little':
            data.byteswap()
        return data

    def rms(data):
        return math.sqrt(math.fsum(value * value for value in data) / len(data))

    assert rms(pcm(quiet.path)[rate:]) < rms(pcm(loud.path)[rate:]) * .05
    session.toggle(0)
    session.toggle(1)
    bypass = runtime.convert(source, 'bypassed.wav', channels=1, effects=session.effects)
    assert pcm(bypass.path) == pcm(source)
    assert sha256(source.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob('.audio-*'))


def test_preset_growth_after_initial_stat_respects_the_actual_read_limit(tmp_path, monkeypatch):
    import carr_library.audio.effect_presets as module
    store = EffectPresetStore(tmp_path)
    saved = store.save('Bounded original', (Effect('gain'),))
    path = store.root / (saved.id + '.json')
    original = module.read_json
    def grow(selected, **options):
        # Still valid JSON with harmless trailing whitespace. A pre-stat-only
        # bound would parse and accept this larger record.
        path.write_bytes(path.read_bytes() + b' ' * (64 * 1024))
        return original(selected, **options)
    monkeypatch.setattr(module, 'read_json', grow)
    with pytest.raises(ValueError):
        store.get(saved.id)


@pytest.mark.parametrize('damage', ['uppercase-name', 'directory', 'unlisted-file'])
def test_preset_inventory_rejects_unknown_or_nonregular_entries(tmp_path, damage):
    store = EffectPresetStore(tmp_path)
    saved = store.save('Reviewed', (Effect('gain'),))
    if damage == 'uppercase-name':
        old = store.root / (saved.id + '.json')
        old.rename(store.root / (saved.id.upper() + '.JSON'))
    elif damage == 'directory':
        (store.root / ('b' * 32 + '.json')).mkdir()
    else:
        (store.root / 'unlisted-settings.txt').write_text('Not an admitted preset.', encoding='utf-8')
    with pytest.raises(ValueError):
        store.list()


def test_incomplete_preset_reservations_are_bounded_too(tmp_path, monkeypatch):
    import carr_library.audio.effect_presets as module
    store = EffectPresetStore(tmp_path)
    monkeypatch.setattr(module, '_MAX_INVENTORY', 4)
    for index in range(5):
        (store.root / ('.pending-' + str(index))).write_bytes(b'')
    with pytest.raises(ValueError, match='too many entries'):
        store.list()


def test_preset_scandir_denial_propagates_instead_of_empty_success(tmp_path, monkeypatch):
    import carr_library.audio.effect_presets as module
    store = EffectPresetStore(tmp_path)
    store.save('Existing', (Effect('gain'),))
    real = module.os.scandir
    def denied(path):
        if Path(path) == store.root:
            raise PermissionError('Synthetic actual directory enumeration denial')
        return real(path)
    monkeypatch.setattr(module.os, 'scandir', denied)
    with pytest.raises(PermissionError, match='enumeration'):
        store.list()


def test_old_preset_owner_cannot_write_into_same_path_replacement(tmp_path):
    library = tmp_path / 'library'; library.mkdir()
    store = EffectPresetStore(library)
    saved = store.save('Before root replacement', (Effect('gain'),))
    old = tmp_path / 'previous-library'
    library.rename(old)
    library.mkdir()
    replacement = EffectPresetStore(library)
    new = replacement.save('Replacement owner', (Effect('gain', gain_db=-9),))
    before = {path.name: path.read_bytes() for path in replacement.root.glob('*.json')}
    with pytest.raises(ValueError, match='library changed'):
        store.save('Stale overwrite', (), previous=saved)
    assert {path.name: path.read_bytes() for path in replacement.root.glob('*.json')} == before
    assert replacement.get(new.id) == new
    assert (old / 'state/studio/effect-presets' / (saved.id + '.json')).is_file()


def test_preset_store_rechecks_explicit_current_view_before_saving(tmp_path):
    selected = [True]
    def guard():
        if not selected[0]:
            raise ValueError('Selected library object changed')
    store = EffectPresetStore(tmp_path, guard=guard)
    original = store.save('Reviewed', (Effect('gain'),))
    selected[0] = False
    with pytest.raises(ValueError, match='object changed'):
        store.save('Must not be admitted', ())
    assert [path.stem for path in store.root.glob('*.json')] == [original.id]


@pytest.mark.parametrize('updating', [False, True])
def test_actual_preset_commit_with_directory_flush_failure_retains_its_identity(tmp_path, monkeypatch, updating):
    import carr_library.durable_state as durable
    store = EffectPresetStore(tmp_path)
    previous = store.save('Previous review', (Effect('gain', gain_db=-2),)) if updating else None
    visible = []
    def fail_directory_sync(path):
        assert Path(path) == store.root
        files = list(store.root.glob('*.json'))
        assert len(files) == 1 and files[0].stat().st_nlink == 1
        # Real atomic_json has already written/fsynced and published these bytes.
        visible.append((files[0], files[0].read_bytes()))
        raise OSError('Synthetic directory flush failure after actual publication')
    monkeypatch.setattr(durable, 'sync_directory', fail_directory_sync)
    with pytest.raises(PresetSaveCommittedError) as caught:
        store.save('Visible committed review', (Effect('gain', gain_db=-7),), previous=previous)
    error = caught.value
    assert error.durability_confirmed is False and isinstance(error.__cause__, OSError)
    assert error.preset.name == 'Visible committed review'
    assert error.preset.revision == (2 if updating else 1)
    if previous is not None:
        assert error.preset.id == previous.id
    assert len(visible) == 1
    path, raw = visible[0]
    assert path.name == error.preset.id + '.json' and path.read_bytes() == raw
    assert json.loads(raw) == error.preset.to_dict()
    assert store.list() == (error.preset,)
    assert error.preset.id in str(error) and 'do not repeat' in str(error)


def test_preset_precommit_flush_failure_never_claims_a_saved_record(tmp_path, monkeypatch):
    import carr_library.durable_state as durable
    store = EffectPresetStore(tmp_path)
    def fail_file_sync(descriptor):
        raise OSError('Synthetic file flush failure before publication')
    monkeypatch.setattr(durable.os, 'fsync', fail_file_sync)
    with pytest.raises(OSError, match='before publication') as caught:
        store.save('Unpublished', (Effect('gain'),))
    assert not isinstance(caught.value, PresetSaveCommittedError)
    assert store.list() == () and not list(store.root.glob('.pending-*'))


def test_preset_same_byte_competitor_remains_a_create_only_conflict(tmp_path, monkeypatch):
    import carr_library.audio.effect_presets as module
    store = EffectPresetStore(tmp_path)
    atomic = module.atomic_json
    competing = []
    def publish_competitor_then_attempt(path, value, *, replace=False):
        assert replace is False
        atomic(path, value, replace=False)
        competing.append((Path(path), Path(path).read_bytes()))
        return atomic(path, value, replace=False)
    monkeypatch.setattr(module, 'atomic_json', publish_competitor_then_attempt)
    with pytest.raises(FileExistsError):
        store.save('Same-byte competing identity', (Effect('gain', gain_db=-5),))
    path, raw = competing[0]
    assert path.read_bytes() == raw and path.stat().st_nlink == 1
    assert len(store.list()) == 1
