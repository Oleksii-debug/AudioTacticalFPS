"""Actual data/worker boundaries; these fixtures never impersonate model inference."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest

from carr_library.audio import musicgen_worker as worker
from carr_library.audio.music import MusicGenConfig, _worker_environment, validate_musicgen_config
from carr_library.audio.render import RenderCancelled


def fixture(tmp_path, documents=None, extra=None):
    root = tmp_path / 'approved model'
    root.mkdir()
    for name, document in ({'config.json': {}} | (documents or {})).items():
        (root / name).write_text(json.dumps(document), encoding='utf-8')
    (root / 'model.safetensors').write_bytes(b'Inert boundary fixture: not tensor weights.')
    for name, content in (extra or {}).items():
        (root / name).write_bytes(content)
    manifest = {'version': 1, 'engine': 'musicgen-transformers',
                'model_id': 'facebook/musicgen-small', 'revision': 'a' * 40,
                'weights_license': 'CC-BY-NC-4.0', 'code_license': 'Apache-2.0',
                'transformers_version': '4.57.6', 'torch_version': '2.6.0',
                'files': [{'path': path.name, 'size_bytes': path.stat().st_size,
                           'sha256': sha256(path.read_bytes()).hexdigest()}
                          for path in sorted(root.iterdir())]}
    location = tmp_path / 'approved-manifest.json'
    raw = json.dumps(manifest).encode('utf-8')
    location.write_bytes(raw)
    return MusicGenConfig(root, location, sha256(raw).hexdigest()), manifest


@pytest.mark.parametrize('reference', ['../unlisted.safetensors', '/tmp/unlisted.safetensors',
                                     'https://invalid.example/weights', 'C:\\weights.safetensors',
                                     'missing.safetensors', None])
def test_shard_index_cannot_read_outside_approved_inventory(tmp_path, monkeypatch, reference):
    config, _ = fixture(tmp_path, {'model.safetensors.index.json':
                                  {'metadata': {}, 'weight_map': {'layer.weight': reference}}})
    outside = tmp_path / 'unlisted.safetensors'
    outside.write_bytes(b'Private file must never be opened by model validation.')
    original = Path.open
    def guarded(path, *args, **kwargs):
        assert path != outside, 'An unlisted asset was read before rejecting its reference.'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    with pytest.raises(ValueError, match='reference|inventory'):
        validate_musicgen_config(config)


def test_all_shards_and_local_tokenizer_file_references_must_be_approved(tmp_path):
    config, _ = fixture(tmp_path, {
        'model.safetensors.index.json': {'metadata': {}, 'weight_map': {'x': 'model.safetensors'}},
        'tokenizer_config.json': {'tokenizer_class': 'T5Tokenizer', 'vocab_file': 'spiece.model'},
        'tokenizer.json': {'model': {'type': 'Unigram', 'vocab': [['../literal token', -1],
                                                              ['https://literal.token', -2]]}},
    }, {'spiece.model': b'Inert vocabulary-boundary fixture.'})
    root, _, _ = validate_musicgen_config(config)
    assert root == config.model_dir


def test_selected_python_environment_is_preserved_for_actual_worker_launch(tmp_path):
    config, _ = fixture(tmp_path)
    environment = tmp_path / 'installed model environment'
    venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(environment)
    executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    _, _, selected = validate_musicgen_config(replace(config, python_executable=str(executable)))
    result = subprocess.run([selected, '-I', '-B', '-c', 'import sys; print(sys.prefix)'],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert Path(selected) == executable.absolute()
    assert Path(result.stdout.strip()) == environment


def test_mapped_model_volume_is_rejected_before_any_path_metadata(tmp_path, monkeypatch):
    # This exercises the Win32 API boundary, not an actual mapped network drive.
    calls = []
    def drive_type(anchor):
        calls.append(anchor)
        return 4
    monkeypatch.setattr(worker, 'os', SimpleNamespace(name='nt', fspath=os.fspath))
    monkeypatch.setattr(worker.ctypes, 'WinDLL', lambda *args, **kwargs:
                        SimpleNamespace(GetDriveTypeW=drive_type), raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('Mapped model paths must be rejected before path metadata I/O.')
    monkeypatch.setattr(Path, 'lstat', forbidden)
    with pytest.raises(ValueError, match='Mapped network'):
        worker.plain_local(tmp_path / 'model.safetensors')
    assert calls == [tmp_path.anchor]


@pytest.mark.parametrize('config_data', [
    {'tokenizer_file': '../unlisted.json'}, {'vocab_file': 'https://invalid.example/vocab'},
    {'tokenizer_files': ['missing.json']}, {'cache_dir': '/tmp/global-cache'},
    {'auto_map': {'AutoProcessor': 'external.CustomCode'}},
    {'compression_model_checkpoint': 'facebook/encodec_32khz'},
    {'pretrained': 'facebook/encodec_32khz'}, {'trust_remote_code': False},
    {'custom_generate': 'external/code'}, {'processor_class': 'UnreviewedProcessor'},
    {'tokenizer_class': ['T5Tokenizer']}, {'quantization_config': {}},
])
def test_model_loader_references_and_unreviewed_hooks_are_rejected(tmp_path, config_data):
    config, _ = fixture(tmp_path, {'tokenizer_config.json': config_data})
    with pytest.raises(ValueError):
        validate_musicgen_config(config)


@pytest.mark.parametrize('name', ['pytorch_model.bin', 'model.py', 'model.dll',
                                 'adapter_config.json', 'model.pkl'])
def test_executable_pickle_and_unreviewed_adapter_assets_are_rejected(tmp_path, name):
    config, _ = fixture(tmp_path, extra={name: b'{}'})
    with pytest.raises(ValueError, match='passive|unsupported'):
        validate_musicgen_config(config)


def test_ambiguous_configuration_is_not_reinterpreted(tmp_path):
    config, _ = fixture(tmp_path, extra={'tokenizer_config.json': b'{"vocab_file":null,"vocab_file":"outside"}'})
    with pytest.raises(ValueError, match='Duplicate'):
        validate_musicgen_config(config)


def test_manifest_only_validation_never_means_inference_assets_are_complete(tmp_path):
    config, manifest = fixture(tmp_path)
    validate_musicgen_config(config)
    with pytest.raises(ValueError, match='unavailable'):
        worker.validate_model_assets(config.model_dir, manifest, require_inference_assets=True)


def test_verification_cancellation_keeps_original_assets(tmp_path):
    config, _ = fixture(tmp_path)
    before = config.manifest.read_bytes()
    with pytest.raises(RenderCancelled):
        validate_musicgen_config(config, cancelled=lambda: True)
    assert config.manifest.read_bytes() == before


def test_model_revision_is_exact_not_an_intermediate_hash_length(tmp_path):
    config, manifest = fixture(tmp_path)
    manifest['revision'] = 'a' * 41
    raw = json.dumps(manifest).encode()
    config.manifest.write_bytes(raw)
    with pytest.raises(ValueError, match='rights/version'):
        validate_musicgen_config(replace(config, manifest_sha256=sha256(raw).hexdigest()))


def test_python_guard_blocks_raw_saved_and_existing_internet_sockets():
    code = '''
import _socket, runpy, socket, subprocess, sys
saved = _socket.socket
existing = saved(socket.AF_INET, socket.SOCK_STREAM)
module = runpy.run_path(sys.argv[1])
module['deny_network']()
attempts = (
    lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
    lambda: _socket.socket(socket.AF_INET6, socket.SOCK_STREAM),
    lambda: saved(socket.AF_INET, socket.SOCK_DGRAM),
    lambda: existing.connect(('127.0.0.1', 9)),
    lambda: _socket.getaddrinfo('example.invalid', 443),
    lambda: socket.gethostbyname('example.invalid'),
    lambda: subprocess.Popen([sys.executable, '-c', 'raise SystemExit(99)']),
)
for attempt in attempts:
    try:
        attempt()
    except PermissionError:
        pass
    else:
        raise AssertionError('Python worker guard was bypassed')
existing.close()
print('PASS: seven Python operations denied before transport/child execution; no native isolation claim')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(Path(worker.__file__))],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'seven Python operations denied' in result.stdout


def test_actual_kernel_network_state_must_justify_isolation(monkeypatch):
    # This is a kernel-state check, not neural inference or a claimed sandbox.
    monkeypatch.setenv('CARR_MUSICGEN_ISOLATED', '1')
    if sys.platform.startswith('linux'):
        import socket
        isolated = (os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net')
                    and {n for _, n in socket.if_nameindex()} == {'lo'})
    else:
        isolated = False
    if isolated:
        assert worker.require_network_isolation()['external_interfaces'] == 0
    else:
        with pytest.raises(RuntimeError, match='unavailable'):
            worker.require_network_isolation()


def test_worker_environment_has_private_cache_and_no_loader_or_account_credentials(tmp_path, monkeypatch):
    for key in ('LD_PRELOAD', 'DYLD_INSERT_LIBRARIES', 'PYTHONPATH', 'HF_TOKEN',
                'AWS_ACCESS_KEY_ID', 'GOOGLE_APPLICATION_CREDENTIALS', 'HTTPS_PROXY'):
        monkeypatch.setenv(key, 'must-not-enter-worker')
    environment = _worker_environment(tmp_path)
    assert all(value != 'must-not-enter-worker' for value in environment.values())
    assert environment['HF_HOME'] == str(tmp_path / 'private-cache')
    assert environment['HF_HUB_OFFLINE'] == environment['TRANSFORMERS_OFFLINE'] == '1'


def test_standalone_fixed_worker_rejects_duplicate_request_before_optional_imports(tmp_path):
    request = tmp_path / 'request.json'
    request.write_text('{"version":1,"version":1}', encoding='utf-8')
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(worker.__file__)), str(request)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and 'Duplicate' in result.stderr
