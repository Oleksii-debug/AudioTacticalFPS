from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import socket
import sys

import pytest

from carr_library.audio.music import (MusicGenConfig, MusicSpec, generate_music,
                                      music_runtime_identity, validate_musicgen_config)
from carr_library.audio.render import RenderCancelled, file_sha256, run_process


def model_fixture(tmp_path):
    root = tmp_path/'model'
    root.mkdir()
    (root/'config.json').write_text('{}', encoding='utf-8')
    (root/'model.safetensors').write_bytes(b'passive invalid test fixture, not model weights')
    manifest = {'version': 1, 'engine': 'musicgen-transformers', 'model_id': 'facebook/musicgen-small',
                'revision': 'a'*40, 'weights_license': 'CC-BY-NC-4.0', 'code_license': 'Apache-2.0',
                'transformers_version': '4.57.6', 'torch_version': '2.6.0',
                'files': [{'path': p.name, 'size_bytes': p.stat().st_size, 'sha256': file_sha256(p)}
                          for p in sorted(root.iterdir())]}
    path = tmp_path/'model-manifest.json'
    raw = json.dumps(manifest).encode('utf-8')
    path.write_bytes(raw)
    return MusicGenConfig(root, path, sha256(raw).hexdigest())


def test_genuine_local_procedural_generation_and_seed_reproducibility(tmp_path):
    specification = MusicSpec(3, mood='suspense', seed=42)
    first = generate_music(specification, tmp_path/'first.wav')
    second = generate_music(specification, tmp_path/'second.wav')
    third = generate_music(replace(specification, seed=43), tmp_path/'third.wav')
    assert first.frames == 72000 and first.channels == 2
    assert first.sha256 == second.sha256
    assert first.sha256 != third.sha256
    assert -40 < first.peak_dbfs < -5
    assert first.metadata['engine'] == 'procedural'
    assert first.metadata['original_neural_generation'] is False
    assert first.metadata['motif_recurrence'] is True
    with pytest.raises(FileExistsError):
        generate_music(specification, first.path)


def test_selected_neural_engine_never_silently_falls_back(tmp_path):
    with pytest.raises(ValueError, match='Configure'):
        generate_music(MusicSpec(1, engine='musicgen'), tmp_path/'no-weights.wav')
    assert not (tmp_path/'no-weights.wav').exists()
    with pytest.raises(ValueError, match='chunks'):
        generate_music(MusicSpec(3600, engine='musicgen'), tmp_path/'too-long-chunk.wav')


@pytest.mark.parametrize('values', [dict(duration_seconds=-1), dict(duration_seconds=True),
    dict(tempo_bpm=0), dict(mood='x; amovie=http://bad'), dict(seed=-1),
    dict(key='url'), dict(engine='remote-paid'), dict(prompt='bad\0')])
def test_music_controls_are_typed(values):
    with pytest.raises(ValueError):
        MusicSpec(**({'duration_seconds': 1} | values))


def test_music_cancellation_and_disk_budget(tmp_path):
    with pytest.raises(RenderCancelled):
        generate_music(MusicSpec(1), tmp_path/'cancel.wav', cancelled=lambda: True)
    with pytest.raises(ValueError, match='budget'):
        generate_music(MusicSpec(20), tmp_path/'budget.wav', max_output_bytes=1)
    assert not list(tmp_path.glob('*.wav'))


def test_local_model_inventory_identity_and_version_binding(tmp_path):
    config = model_fixture(tmp_path)
    root, manifest, python = validate_musicgen_config(config)
    assert root == config.model_dir
    assert manifest['weights_license'] == 'CC-BY-NC-4.0'
    assert Path(python).is_file()
    identity = music_runtime_identity({'musicgen_config': config})
    assert identity['musicgen']['manifest_sha256'] == config.manifest_sha256
    assert len(identity['audio_runtime']['ffmpeg']['executable_sha256']) == 64
    assert len(identity['audio_runtime']['ffprobe']['executable_sha256']) == 64
    (root/'model.safetensors').write_bytes(b'changed')
    with pytest.raises(ValueError, match='verification'):
        validate_musicgen_config(config)


def test_unknown_files_and_pickle_weights_are_not_loaded(tmp_path):
    config = model_fixture(tmp_path)
    (config.model_dir/'injected.py').write_text('raise RuntimeError("never execute")', encoding='utf-8')
    with pytest.raises(ValueError, match='inventory'):
        validate_musicgen_config(config)
    (config.model_dir/'injected.py').unlink()
    data = json.loads(config.manifest.read_text(encoding='utf-8'))
    data['files'][1]['path'] = 'pytorch_model.bin'
    raw = json.dumps(data).encode('utf-8')
    config.manifest.write_bytes(raw)
    config = replace(config, manifest_sha256=sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='passive'):
        validate_musicgen_config(config)


def test_model_manifest_must_match_explicit_approved_digest(tmp_path):
    config = model_fixture(tmp_path)
    with pytest.raises(ValueError, match='checksum'):
        validate_musicgen_config(replace(config, manifest_sha256='0'*64))
    raw = json.loads(config.manifest.read_text(encoding='utf-8'))
    raw['weights_license'] = 'commercial-permitted'
    content = json.dumps(raw).encode('utf-8')
    config.manifest.write_bytes(content)
    with pytest.raises(ValueError, match='rights'):
        validate_musicgen_config(replace(config, manifest_sha256=sha256(content).hexdigest()))


def test_worker_network_deny_is_exercised_without_network_access():
    from carr_library.audio import musicgen_worker
    code = ('import runpy,socket; m=runpy.run_path(' + repr(str(Path(musicgen_worker.__file__))) + '); '
            'm["deny_network"]();\n'
            'for attempt in (lambda:socket.create_connection(("127.0.0.1",9)), '
            'lambda:socket.getaddrinfo("example.invalid",443), '
            'lambda:socket.socket().connect(("127.0.0.1",9)), '
            'lambda:socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b"x",("127.0.0.1",9))):\n'
            ' try: attempt()\n'
            ' except PermissionError: pass\n'
            ' else: raise AssertionError("network was not denied")\n'
            'print("four Internet operations denied before transport")')
    out, _ = run_process([sys.executable, '-I', '-c', code], timeout_seconds=20)
    assert 'four Internet operations denied' in out


def test_fluidsynth_requires_explicit_soundfont_rights(tmp_path):
    with pytest.raises(ValueError, match='SoundFont'):
        generate_music(MusicSpec(1, engine='fluidsynth'), tmp_path/'midi.wav')
    assert not (tmp_path/'midi.wav').exists()


def test_missing_model_python_or_environment_is_an_error(tmp_path):
    config = model_fixture(tmp_path)
    with pytest.raises(RuntimeError, match='unavailable'):
        validate_musicgen_config(replace(config, python_executable=str(tmp_path/'missing-python')))


@pytest.mark.parametrize('payload', ['{"version":1,"version":1}', '{"version":true}'])
def test_ambiguous_model_manifest_is_rejected(tmp_path, payload):
    config = model_fixture(tmp_path)
    raw = payload.encode('utf-8')
    config.manifest.write_bytes(raw)
    with pytest.raises(ValueError, match='manifest'):
        validate_musicgen_config(replace(config, manifest_sha256=sha256(raw).hexdigest()))
