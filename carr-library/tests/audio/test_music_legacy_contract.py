"""Actual local composition, MIDI interchange and model installation gates."""
from array import array
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import wave

import pytest

from carr_library.audio import (AudioCancelled, AudioRuntime, AudioUnavailable,
                               LocalModelConfig, MusicSpec)
from carr_library.audio.music import MUSICGEN_REVISION, validate_model


HAS_FFMPEG = bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason='Actual local FFmpeg and ffprobe are required.')


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        assert stream.getsampwidth() == 2
        values = array('h')
        values.frombytes(stream.readframes(stream.getnframes()))
        if sys.byteorder != 'little':
            values.byteswap()
        return values, stream.getframerate(), stream.getnchannels()


@requires_ffmpeg
def test_real_local_instrumental_is_non_silent_reproducible_and_seeded(tmp_path):
    runtime = AudioRuntime(tmp_path)
    spec = MusicSpec(duration=4, mood='suspense', tempo=100, seed=42)
    first = runtime.render_music(spec, 'first.wav')
    second = runtime.render_music(spec, 'second.wav')
    changed = runtime.render_music(replace(spec, seed=43), 'changed.wav')
    assert first.sha256 == second.sha256 and changed.sha256 != first.sha256
    values, rate, channels = pcm(first.path)
    assert (rate, channels, len(values)) == (24000, 2, 4 * 24000 * 2)
    assert max(map(abs, values)) > 3000
    assert max(map(abs, values)) < 30000
    assert values[0] == values[1] == 0
    assert max(map(abs, values[-100:])) < 30  # measured fade to silence
    assert runtime.measure(first.path).integrated_lufs is not None


@requires_ffmpeg
@pytest.mark.parametrize('mood', ['calm', 'suspense', 'hopeful', 'sad', 'energetic'])
def test_all_documented_local_music_moods_render_actual_pcm(tmp_path, mood):
    runtime = AudioRuntime(tmp_path)
    result = runtime.render_music(MusicSpec(duration=1, mood=mood, key='F#', seed=7), 'music.wav')
    values, _, _ = pcm(result.path)
    assert result.frames == 24000
    assert math.sqrt(sum(value * value for value in values) / len(values)) > 500


@requires_ffmpeg
@pytest.mark.parametrize('name', ['door', 'click', 'chime', 'noise'])
def test_procedural_sound_cues_produce_real_pcm_and_exact_duration(tmp_path, name):
    runtime = AudioRuntime(tmp_path)
    result = runtime.render_sound(name, name + '.wav', duration=.75, seed=123)
    values, _, _ = pcm(result.path)
    assert result.frames == 18000
    assert max(map(abs, values)) > 500
    assert max(map(abs, values)) < 30000


@requires_ffmpeg
def test_midi_export_contains_tempo_instruments_notes_and_exact_end(tmp_path):
    runtime = AudioRuntime(tmp_path)
    spec = MusicSpec(duration=4, tempo=120, seed=10)
    path = runtime.export_midi(spec, 'композиція.mid')
    data = path.read_bytes()
    assert data.startswith(b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk')
    assert int.from_bytes(data[18:22], 'big') == len(data) - 22
    assert b'\xff\x51\x03\x07\xa1\x20' in data  # 120 BPM
    assert b'\xc0\x0b' in data and b'\xc1\x20' in data
    assert b'\x90' in data and b'\x80' in data and data.endswith(b'\xff\x2f\x00')
    with pytest.raises(FileExistsError):
        runtime.export_midi(spec, path)


@requires_ffmpeg
def test_music_cancel_never_publishes_and_unknown_sounds_never_execute(tmp_path):
    runtime = AudioRuntime(tmp_path)
    with pytest.raises(AudioCancelled):
        runtime.render_music(MusicSpec(duration=2), 'never.wav', cancelled=lambda: True)
    with pytest.raises(ValueError, match='Supported'):
        runtime.render_sound('amovie=http://private', 'never.wav')
    with pytest.raises(AudioCancelled):
        runtime.export_midi(MusicSpec(duration=20), 'never.mid', cancelled=lambda: True)
    assert not list(tmp_path.iterdir())


@requires_ffmpeg
@pytest.mark.parametrize('engine', ['midi', 'musicgen', 'ace-step'])
def test_optional_music_engines_never_download_or_silently_fall_back(tmp_path, engine):
    runtime = AudioRuntime(tmp_path)
    with pytest.raises(AudioUnavailable):
        runtime.render_music(MusicSpec(duration=2, engine=engine), 'never.wav')
    assert not (tmp_path / 'never.wav').exists()
    assert not next(item for item in runtime.music_capabilities() if item['engine'] == engine)['available']


def model_fixture(tmp_path):
    root = tmp_path / 'weights'
    root.mkdir()
    # Synthetic manifest-validation fixtures only: never inference weights.
    files = {'state_dict.bin': b'synthetic state', 'compression_state_dict.bin': b'synthetic compression'}
    for name, value in files.items():
        (root / name).write_bytes(value)
    data = {'schema': 1, 'engine': 'musicgen', 'code_revision': MUSICGEN_REVISION,
            'weights_license': 'CC-BY-NC-4.0', 'noncommercial_only': True,
            'output_rights_notice': 'Synthetic manifest fixture; no actual model or rights assertion.',
            'files': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
    manifest = tmp_path / 'reviewed-manifest.json'
    manifest.write_text(json.dumps(data), encoding='utf-8')
    return LocalModelConfig('musicgen', Path(sys.executable).resolve(), root, manifest), data


def test_model_manifest_uses_exact_complete_file_hashes(tmp_path):
    config, data = model_fixture(tmp_path)
    assert validate_model(config)['engine'] == 'musicgen'
    (config.directory / 'state_dict.bin').write_bytes(b'changed model')
    with pytest.raises(ValueError, match='SHA256'):
        validate_model(config)


def test_model_inventory_rejects_additional_unreviewed_assets(tmp_path):
    config, data = model_fixture(tmp_path)
    (config.directory / 'unreviewed.py').write_text('raise SystemExit(9)', encoding='utf-8')
    with pytest.raises(ValueError, match='inventory'):
        validate_model(config)


def test_musicgen_noncommercial_profile_is_enforced(tmp_path):
    config, data = model_fixture(tmp_path)
    with pytest.raises(ValueError, match='noncommercial'):
        validate_model(replace(config, private_noncommercial=False))
    data['weights_license'] = 'unreviewed'
    config.manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='noncommercial'):
        validate_model(config)


def test_model_revision_and_output_rights_are_required(tmp_path):
    config, data = model_fixture(tmp_path)
    data['code_revision'] = 'latest'
    config.manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='revision'):
        validate_model(config)
    data['code_revision'] = MUSICGEN_REVISION
    data.pop('output_rights_notice')
    config.manifest.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='rights'):
        validate_model(config)


def test_neural_chunk_budget_is_distinct_from_project_duration():
    # A long project is legal. Runtime neural chunks have explicit independent
    # engine/resource budgets and are scheduled by the persistent director.
    assert MusicSpec(duration=10 * 3600, engine='musicgen').duration == 36000
    with pytest.raises(ValueError):
        MusicSpec(duration=float('inf'))
    with pytest.raises(ValueError):
        MusicSpec(duration=1, tempo='120;http://private')


def test_model_verification_is_cancellable(tmp_path):
    config, data = model_fixture(tmp_path)
    with pytest.raises(AudioCancelled):
        validate_model(config, cancelled=lambda: True)


@requires_ffmpeg
def test_configured_model_assets_are_not_claimed_as_verified_inference(tmp_path):
    config, data = model_fixture(tmp_path)
    runtime = AudioRuntime(tmp_path)
    runtime.configure_music_model(config)
    state = next(row for row in runtime.music_capabilities() if row['engine'] == 'musicgen')
    assert state['configured'] and state['assets_verified']
    assert state['runtime_probed'] is False
    assert state['runtime_code_verified'] is False
    assert state['inference_tested'] is False
    assert state['available'] is False
    with pytest.raises(AudioUnavailable, match='runtime_code_files'):
        runtime.probe_music_model('musicgen')
    with pytest.raises(AudioUnavailable, match='runtime_code_files'):
        runtime.render_music(MusicSpec(duration=1, engine='musicgen'), 'never.wav')
    assert not (tmp_path / 'never.wav').exists()


def test_model_inventory_uses_fresh_file_identity_not_cached_windows_entries(tmp_path, monkeypatch):
    """Windows scandir metadata can omit link counts for ordinary files."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    import carr_library.audio.music_runtime as runtime

    config, data = model_fixture(tmp_path)
    original_scandir = runtime.os.scandir

    class CachedEntry:
        def __init__(self, entry):
            self.entry, self.path = entry, entry.path

        def stat(self, *, follow_symlinks=True):
            info = self.entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size,
                st_nlink=0, st_file_attributes=getattr(info, 'st_file_attributes', 0))

    @contextmanager
    def cached_scandir(path):
        with original_scandir(path) as entries:
            yield (CachedEntry(entry) for entry in entries)

    with monkeypatch.context() as context:
        context.setattr(runtime.os, 'scandir', cached_scandir)
        assert validate_model(config) == data


def test_model_inventory_still_rejects_actual_hardlinks(tmp_path):
    import os

    config, _data = model_fixture(tmp_path)
    source = config.directory / 'state_dict.bin'
    os.link(source, tmp_path / 'other-weight-name.bin')
    assert source.lstat().st_nlink == 2
    with pytest.raises(ValueError, match='ordinary materialized'):
        validate_model(config)
