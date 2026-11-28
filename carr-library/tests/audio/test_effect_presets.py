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
    EffectPresetStore, builtin_presets, checked_chain, default_effect)


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
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual local FFmpeg and ffprobe are required.'
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



@pytest.mark.parametrize('rate', [8000, 16000, 24000, 48000])
def test_graphic_defaults_keep_all_ten_bands_and_inactive_high_bands_at_low_rates(rate):
    from carr_library.audio.dsp import effect_parameter_label

    effect = default_effect('graphic_equalizer', rate)
    assert len(effect.parameters) == 10 and set(effect.parameters.values()) == {0}
    assert checked_chain((effect,), rate) == (effect,)
    label = effect_parameter_label('graphic_equalizer', 'band_16000_db', sample_rate=rate)
    assert label.startswith('16000 Hz band gain')
    assert ('keep 0 dB' in label) is (rate <= 32000)
    session = EffectChainSession(sample_rate=rate)
    assert session.add('graphic_equalizer') == 0
    assert session.effects == (effect,)


@pytest.mark.parametrize('enabled', [True, False])
def test_graphic_nyquist_edit_preserves_the_previous_session_and_undo_history(enabled):
    session = EffectChainSession((default_effect('graphic_equalizer', 24000),))
    previous = session.effects
    with pytest.raises(ValueError, match='half the selected sample rate'):
        session.edit(0, Effect('graphic_equalizer', band_16000_db=1, enabled=enabled))
    assert session.effects is previous
    with pytest.raises(ValueError, match='undo'):
        session.history()


def test_graphic_settings_roundtrip_through_native_backup_without_audio_authority(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup

    app = LibraryApp(tmp_path / 'graphic-library')
    store = EffectPresetStore(app.root)
    parameters = dict(default_effect('graphic_equalizer', 48000).parameters)
    parameters.update(band_31_5_db=3, band_1000_db=-6, band_16000_db=2)
    effects = (Effect('graphic_equalizer', parameters, enabled=False),
               Effect('notch', frequency=50, q=10))
    saved = store.save('Графічний еквалайзер — огляд', effects, sample_rate=48000)
    original = (store.root / (saved.id + '.json')).read_bytes()
    assert json.loads(original)['schema'] == 1
    backup = tmp_path / 'graphic-backup'
    checksum = create_backup(app, backup)
    destination = restore_backup(backup, tmp_path / 'graphic-restored', checksum)
    restored = EffectPresetStore(destination)
    assert restored.get(saved.id) == saved
    assert (restored.root / (saved.id + '.json')).read_bytes() == original
    assert restored.get(saved.id).effects[0].parameters == parameters
    assert restored.get(saved.id).effects[0].enabled is False
    assert not list(destination.rglob('*.wav'))


@pytest.mark.parametrize('enabled', [True, False])
def test_saved_graphic_settings_are_revalidated_when_output_rate_changes(enabled):
    effect = Effect('graphic_equalizer', band_16000_db=2, enabled=enabled)
    saved = EffectPreset('b' * 32, 'Reviewed rate', 48000, (effect,))
    value = saved.to_dict()
    value['sample_rate'] = 24000
    with pytest.raises(ValueError, match='half the selected sample rate'):
        EffectPreset.from_dict(value)
    assert saved.sample_rate == 48000 and saved.effects == (effect,)
