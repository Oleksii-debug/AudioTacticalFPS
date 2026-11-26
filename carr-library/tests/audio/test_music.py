"""Local music contracts and actual optional FluidSynth synthesis evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket

import pytest

from carr_library.audio import AudioCancelled
from carr_library.audio.music import MusicRequest, _manifest, available_music_engines, music_engine_fingerprint, render_music


def manifest(tmp_path, **updates):
    model = tmp_path / 'model'
    model.mkdir()
    config = model / 'config.json'
    config.write_text('{"model_type":"musicgen"}')
    weights = model / 'model.safetensors'
    weights.write_bytes(b'synthetic contract fixture, never represented as inference weights')
    value = {'schema': 1, 'engine': 'musicgen', 'license': 'CC-BY-NC-4.0',
             'revision': '1' * 40, 'source': 'https://huggingface.co/facebook/musicgen-small',
             'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (config, weights)}}
    value.update(updates)
    path = tmp_path / 'approved.json'
    path.write_text(json.dumps(value))
    return model, path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_optional_musicgen_reports_missing_assets_without_network(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('No music operation may attempt a network call.')
    monkeypatch.setattr(socket, 'create_connection', deny)
    with pytest.raises(RuntimeError, match='local model bundle'):
        render_music(MusicRequest('quiet instrumental', 2), tmp_path / 'missing.wav')
    assert not (tmp_path / 'missing.wav').exists()


def test_musicgen_per_inference_window_is_explicit(tmp_path):
    with pytest.raises(ValueError, match='checkpointed chunks'):
        render_music(MusicRequest('long original score', 36000), tmp_path / 'long.wav')


def test_manifest_requires_independent_pin(tmp_path):
    model, path, digest = manifest(tmp_path)
    with pytest.raises(ValueError, match='independently recorded'):
        _manifest(model, path, None)
    with pytest.raises(ValueError, match='checksum'):
        _manifest(model, path, '0' * 64)
    files, actual = _manifest(model, path, digest)
    assert actual == digest
    assert set(files) == {'config.json', 'model.safetensors'}


@pytest.mark.parametrize('filename', ['../unsafe.safetensors', '/unsafe.safetensors',
                                     'sub/../unsafe.safetensors', 'model.bin', 'model.py', 'model.pkl'])
def test_untrusted_model_inventory_paths_and_pickle_rejected(tmp_path, filename):
    model, path, _ = manifest(tmp_path)
    value = json.loads(path.read_text())
    value['files'][filename] = '0' * 64
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        _manifest(model, path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_musicgen_license_is_explicit(tmp_path):
    model, path, digest = manifest(tmp_path, license='unreviewed')
    with pytest.raises(ValueError, match='license'):
        _manifest(model, path, digest)
    with pytest.raises(ValueError, match='noncommercial'):
        MusicRequest('music', 2, usage='commercial')


def test_changed_model_bytes_rejected_before_import_or_execution(tmp_path, monkeypatch):
    model, path, digest = manifest(tmp_path)
    (model / 'model.safetensors').write_bytes(b'changed')
    monkeypatch.setattr('carr_library.audio.music.available_music_engines',
                        lambda: {'musicgen': {'runtime_available': True}})
    with pytest.raises(ValueError, match='approved SHA256'):
        render_music(MusicRequest('private instrumental', 2), tmp_path / 'bad.wav',
                     model_dir=model, manifest_path=path, expected_manifest_sha256=digest)
    assert not (tmp_path / 'bad.wav').exists()


def test_fluidsynth_requires_soundfont_rights_and_hash(tmp_path):
    request = MusicRequest('piano pattern', 2, engine='fluidsynth')
    with pytest.raises(ValueError, match='license'):
        render_music(request, tmp_path / 'bad.wav')
    with pytest.raises(ValueError, match='SHA256'):
        render_music(request, tmp_path / 'bad.wav', soundfont=tmp_path / 'font.sf2', soundfont_license='GPL-2.0-only')


def test_cancelled_music_does_not_publish(tmp_path):
    with pytest.raises(AudioCancelled):
        render_music(MusicRequest('music', 2), tmp_path / 'no.wav', cancelled=lambda: True)
    assert not (tmp_path / 'no.wav').exists()


def test_discovery_does_not_claim_asset_acceptance():
    status = available_music_engines()
    assert status['musicgen']['requires'].startswith('Pinned local')
    assert 'no prose interpretation' in status['fluidsynth']['generation']


def test_musicgen_fingerprint_tracks_request_manifest_and_runtime(tmp_path, monkeypatch):
    model, path, digest = manifest(tmp_path)
    request = MusicRequest('soft music', 2)
    kwargs = dict(model_dir=model, manifest_path=path, expected_manifest_sha256=digest)
    monkeypatch.setattr('importlib.metadata.version', lambda name: 'test-runtime-1')
    original = music_engine_fingerprint(request, **kwargs)
    assert original == music_engine_fingerprint(request, **kwargs)
    assert original != music_engine_fingerprint(replace(request, seed=1), **kwargs)
    monkeypatch.setattr('importlib.metadata.version', lambda name: 'different-runtime-1')
    assert original != music_engine_fingerprint(request, **kwargs)


def test_actual_fluidsynth_if_installed_or_truthful_unavailability(tmp_path):
    # Windows standalone packages do not implicitly include third-party
    # SoundFonts. Either assert the exact unavailable state or run real synthesis
    # when the deliberately selected local test asset exists. No skip/xfail.
    font = Path('/usr/share/sounds/sf2/TimGM6mb.sf2')
    status = available_music_engines()
    if not status['fluidsynth']['runtime_available'] or not font.is_file():
        request = MusicRequest('piano pattern', 2, engine='fluidsynth')
        with pytest.raises((ValueError, RuntimeError, FileNotFoundError)):
            render_music(request, tmp_path / 'unavailable.wav', soundfont=font,
                         soundfont_sha256='0' * 64, soundfont_license='GPL-2.0-only')
        return
    digest = hashlib.sha256(font.read_bytes()).hexdigest()
    request = MusicRequest('deterministic minor piano pattern', 3, engine='fluidsynth', seed=19, tempo=100)
    assert len(music_engine_fingerprint(request, soundfont=font, soundfont_sha256=digest,
                                       soundfont_license='GPL-2.0-only')) == 64
    report = render_music(request, tmp_path / 'actual.wav', soundfont=font,
                          soundfont_sha256=digest, soundfont_license='GPL-2.0-only',
                          allowed_roots=[tmp_path, font.parent])
    assert report.frames == 72000
    assert report.sample_rate == 24000
    assert report.channels == 2
    assert report.duration_seconds == 3
    assert .005 < report.peak < .95
    assert report.source_sha256 == (digest,)
    assert hashlib.sha256(font.read_bytes()).hexdigest() == digest
    with pytest.raises(FileExistsError):
        render_music(request, report.path, soundfont=font,
                     soundfont_sha256=digest, soundfont_license='GPL-2.0-only')
