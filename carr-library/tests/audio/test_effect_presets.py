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
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'This gate requires actual FFmpeg and ffprobe.'
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



def test_growing_record_is_consumed_only_within_the_exact_preset_byte_budget(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from carr_library import durable_state
    from carr_library.audio import effect_presets as model

    store = EffectPresetStore(tmp_path)
    saved = store.save('Bounded record', (Effect('gain'),))
    target = store.root / (saved.id + '.json')
    before = target.read_bytes()
    opened, reads = durable_state._open_json_file, []

    @contextmanager
    def grows_after_descriptor_validation(path):
        with opened(path) as stream:
            def read(maximum):
                reads.append(maximum)
                # Only this parent-owned synthetic record is changed. The
                # real descriptor and shared parser still consume the bytes.
                with target.open('ab') as writer:
                    writer.write(b' ' * (2 * model._MAX_PRESET_BYTES))
                return stream.read(maximum)
            yield SimpleNamespace(fileno=stream.fileno, read=read)

    monkeypatch.setattr(durable_state, '_open_json_file', grows_after_descriptor_validation)
    with pytest.raises(ValueError, match='budget'):
        store.get(saved.id)
    assert reads == [model._MAX_PRESET_BYTES + 1]
    assert target.read_bytes().startswith(before)


@pytest.mark.parametrize('update', [False, True])
@pytest.mark.parametrize('interruption', [OSError, KeyboardInterrupt])
def test_postpublication_acknowledgement_keeps_the_exact_checked_preset(
        tmp_path, monkeypatch, update, interruption):
    from carr_library import durable_state

    store = EffectPresetStore(tmp_path)
    previous = store.save('Earlier', (Effect('gain', gain_db=-2),)) if update else None
    def interrupted_directory_sync(path):
        raise interruption('Synthetic directory durability acknowledgement failure')
    monkeypatch.setattr(durable_state, 'sync_directory', interrupted_directory_sync)
    value = store.save('Reviewed current value', (Effect('gain', gain_db=-7),), previous=previous)
    assert store.get(value.id) == value
    assert value.revision == (2 if update else 1)
    assert 'present and checked' in store.warning
    assert 'completion could not be confirmed' in store.warning
    assert 'acknowledgement failure' in store.warning


@pytest.mark.parametrize('update', [False, True])
def test_failure_before_publication_does_not_claim_the_new_preset(tmp_path, monkeypatch, update):
    from carr_library import durable_state

    store = EffectPresetStore(tmp_path)
    previous = store.save('Earlier', (Effect('gain', gain_db=-2),)) if update else None
    before = {path.name: path.read_bytes() for path in store.root.glob('*.json')}
    def interrupted_flush(fd):
        raise OSError('Synthetic staging flush failure')
    monkeypatch.setattr(durable_state.os, 'fsync', interrupted_flush)
    with pytest.raises(OSError, match='staging flush failure'):
        store.save('Unpublished', (Effect('gain', gain_db=-7),), previous=previous)
    assert {path.name: path.read_bytes() for path in store.root.glob('*.json')} == before
    assert store.warning == ''
    assert not list(store.root.glob('.pending-*'))


def test_preset_inventory_has_a_bound_even_for_unrecognized_nonrecord_files(tmp_path, monkeypatch):
    from carr_library.audio import effect_presets as model
    store = EffectPresetStore(tmp_path)
    monkeypatch.setattr(model, '_MAX_INVENTORY_ENTRIES', 3)
    for index in range(4):
        (store.root / f'unrecognized-{index}').write_bytes(b'Explicit synthetic directory entry')
    with pytest.raises(ValueError, match='bounded inventory'):
        store.list()


def test_changed_record_is_not_returned_as_the_previously_inspected_snapshot(tmp_path, monkeypatch):
    from carr_library.audio import effect_presets as model
    store = EffectPresetStore(tmp_path)
    first = store.save('Reviewed', (Effect('gain', gain_db=-2),))
    path = store.root / (first.id + '.json')
    current_read = model.read_json
    def read_then_replace(source, **kwargs):
        value = current_read(source, **kwargs)
        path.write_bytes(path.read_bytes() + b' ')
        return value
    monkeypatch.setattr(model, 'read_json', read_then_replace)
    with pytest.raises(ValueError, match='changed while reading'):
        store.get(first.id)



def test_cancelled_capability_review_does_not_start_a_native_probe(tmp_path, monkeypatch):
    from carr_library.audio import dsp
    runtime = AudioRuntime(tmp_path)
    monkeypatch.setattr(dsp, 'run_process', lambda *args, **kwargs:
                        pytest.fail('Cancelled capability review started a native process.'))
    with pytest.raises(InterruptedError, match='capability inspection cancelled'):
        runtime.capabilities(cancelled=lambda: True)
    assert runtime._capabilities is None


def test_cancellation_between_actual_ffmpeg_capability_queries_is_forwarded(tmp_path, monkeypatch):
    from threading import Event
    from carr_library.audio import dsp
    from carr_library.audio.models import AudioCancelled
    runtime = AudioRuntime(tmp_path)
    cancelled, completed = Event(), []
    run = dsp.run_process
    def stop_after_first_result(arguments, **kwargs):
        assert callable(kwargs.get('cancelled'))
        result = run(arguments, **kwargs)
        completed.append(tuple(arguments))
        cancelled.set()
        return result
    monkeypatch.setattr(dsp, 'run_process', stop_after_first_result)
    with pytest.raises((InterruptedError, AudioCancelled)):
        runtime.capabilities(cancelled=cancelled.is_set)
    assert len(completed) == 1 and completed[0][-1] == '-version'
    assert runtime._capabilities is None


@pytest.mark.parametrize('update', [False, True])
@pytest.mark.parametrize('interruption', [OSError, KeyboardInterrupt])
def test_exact_preset_survives_real_lock_release_acknowledgement_failure(
        tmp_path, monkeypatch, update, interruption):
    from contextlib import contextmanager
    from carr_library.audio import effect_presets as model

    store = EffectPresetStore(tmp_path)
    previous = store.save('Earlier', (Effect('gain', gain_db=-2),)) if update else None
    original = model.state_lock
    released = []
    @contextmanager
    def interrupted_release(*args, **kwargs):
        with original(*args, **kwargs):
            yield
        released.append(True)
        raise interruption('Synthetic lock-release acknowledgement failure')
    monkeypatch.setattr(model, 'state_lock', interrupted_release)
    result = store.save('Reviewed saved value', (Effect('gain', gain_db=-7),), previous=previous)
    assert released == [True]
    assert store.get(result.id) == result
    assert result.revision == (2 if update else 1)
    assert 'present and checked' in store.warning
    assert 'completion could not be confirmed' in store.warning
    assert 'lock-release acknowledgement failure' in store.warning


@pytest.mark.parametrize('interruption', [OSError, KeyboardInterrupt])
def test_failure_entering_real_preset_lock_never_claims_a_publication(tmp_path, monkeypatch, interruption):
    from contextlib import contextmanager
    from carr_library.audio import effect_presets as model

    store = EffectPresetStore(tmp_path)
    previous = store.save('Earlier', (Effect('gain', gain_db=-2),))
    before = (store.root / (previous.id + '.json')).read_bytes()
    original = model.state_lock
    @contextmanager
    def interrupted_entry(*args, **kwargs):
        with original(*args, **kwargs):
            raise interruption('Synthetic failure before publication admission')
            yield  # Preserve the real context-manager entry boundary.
    monkeypatch.setattr(model, 'state_lock', interrupted_entry)
    with pytest.raises(interruption, match='before publication admission'):
        store.save('Unpublished', (Effect('gain', gain_db=-7),), previous=previous)
    assert (store.root / (previous.id + '.json')).read_bytes() == before
    assert store.warning == ''


@pytest.mark.parametrize('damage', ['changed', 'removed'])
def test_postlock_readback_never_claims_a_missing_or_foreign_preset(tmp_path, monkeypatch, damage):
    from contextlib import contextmanager
    from dataclasses import replace
    from carr_library.audio import effect_presets as model

    store = EffectPresetStore(tmp_path)
    previous = store.save('Earlier', (Effect('gain', gain_db=-2),))
    original = model.state_lock
    foreign = []
    @contextmanager
    def interrupted_release(*args, **kwargs):
        with original(*args, **kwargs):
            yield
        path = store.root / (previous.id + '.json')
        if damage == 'changed':
            current = store.get(previous.id)
            other = replace(current, name='Concurrent different value', revision=current.revision + 1)
            model.atomic_json(path, other.to_dict(), replace=True)
            foreign.append(path.read_bytes())
        else:
            path.unlink()
        raise OSError('Synthetic post-release interruption')
    monkeypatch.setattr(model, 'state_lock', interrupted_release)
    with pytest.raises(OSError, match='post-release interruption'):
        store.save('Reviewed saved value', (Effect('gain', gain_db=-7),), previous=previous)
    path = store.root / (previous.id + '.json')
    assert store.warning == ''
    if damage == 'changed':
        assert path.read_bytes() == foreign[0]
        assert store.get(previous.id).name == 'Concurrent different value'
    else:
        assert not path.exists()


def test_unbounded_iterable_is_rejected_before_consumption_or_preset_creation(tmp_path):
    store = EffectPresetStore(tmp_path)
    consumed = []
    def unbounded():
        while True:
            consumed.append(True)
            yield Effect('gain')
    with pytest.raises(ValueError, match='32 typed effects'):
        store.save('Unbounded', unbounded())
    assert consumed == []
    assert store.list() == ()
    assert store.warning == ''
