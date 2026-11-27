"""Installed runtime identities; synthetic bytes are never native acceptance."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library import bundled_engines as engines, native_checks


def seal(root, component):
    flag, required = engines.COMPONENTS[component]
    files = {}
    records = {}
    for name in required:
        path = root / component / name
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ('Inert synthetic bytes: ' + name).encode()
        path.write_bytes(payload)
        identity = hashlib.sha256(payload).hexdigest()
        files[component + '/' + name] = identity
        records[name] = {'sha256': identity, 'size': len(payload)}
    if component == 'audio':
        lock = {'schema': 1, 'engine': 'FFmpeg', 'engine_version': '9.1-test', 'output_files': records}
    else:
        lock = {'version': '24.22.3', 'files': {name: entry['sha256'] for name, entry in records.items()}}
    path = root / component / 'provenance.json'
    raw = json.dumps(lock).encode()
    path.write_bytes(raw)
    files[component + '/provenance.json'] = hashlib.sha256(raw).hexdigest()
    manifest = {'schema': 1, 'target': native_checks.TARGET, 'bundled_python': True,
                'bundled_model': False, 'bundled_ocr': False, 'files': files, flag: True}
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize('component', engines.COMPONENTS)
def test_complete_component_is_verified_without_execution_or_path_dependency(tmp_path, component):
    manifest = seal(tmp_path, component)
    result = engines.verify_optional_runtime(tmp_path, manifest, component)
    assert result['status'] == 'PASS' and result['execution'] == 'NOT_RUN'
    assert all(Path(value).is_relative_to(tmp_path / component) for value in result['programs'].values())
    assert set(result['programs']) == ({'ffmpeg', 'ffprobe'} if component == 'audio' else {'node'})


@pytest.mark.parametrize('component', engines.COMPONENTS)
@pytest.mark.parametrize('damage', ('same_size_payload', 'outer_identity', 'missing_file', 'undeclared', 'extra_file'))
def test_damaged_included_engine_never_becomes_an_available_runtime(tmp_path, component, damage):
    manifest = seal(tmp_path, component)
    target = tmp_path / component / ('bin/ffmpeg.exe' if component == 'audio' else 'node.exe')
    if damage in ('same_size_payload', 'outer_identity'):
        target.write_bytes(b'X' * target.stat().st_size)
        if damage == 'outer_identity':
            manifest['files'][target.relative_to(tmp_path).as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
    elif damage == 'missing_file':
        target.unlink()
    elif damage == 'undeclared':
        manifest[engines.COMPONENTS[component][0]] = False
    else:
        manifest['files'][component + '/unreviewed.exe'] = 'a' * 64
    with pytest.raises((ValueError, FileNotFoundError)):
        engines.verify_optional_runtime(tmp_path, manifest, component)


def test_bundled_discovery_uses_installed_prefix_and_rejects_corruption(tmp_path, monkeypatch):
    seal(tmp_path, 'citation-runtime')
    monkeypatch.setattr(engines.sys, 'prefix', str(tmp_path / '.venv'))
    assert engines.bundled_executable('node') == str(tmp_path / 'citation-runtime/node.exe')
    assert engines.bundled_executable('ffmpeg') is None
    (tmp_path / 'citation-runtime/node.exe').write_bytes(b'changed')
    with pytest.raises(ValueError, match='sealed identity'):
        engines.bundled_executable('node')


def test_version_probe_uses_exact_absolute_executable_and_bounded_shared_process(tmp_path, monkeypatch):
    from carr_library.audio import process
    manifest = seal(tmp_path, 'audio')
    calls = []
    def run(args, *, budget):
        calls.append(args)
        assert budget.timeout == 30 and budget.max_log_bytes == 65536
        name = Path(args[0]).stem
        return SimpleNamespace(stdout=(name + ' version 9.1-test\n').encode())
    monkeypatch.setattr(process, 'run_process', run)
    result = engines.verify_optional_runtime(tmp_path, manifest, 'audio', execute=True)
    assert result['execution'] == 'PASS' and len(calls) == 2
    assert {call[0] for call in calls} == {str(tmp_path / 'audio/bin/ffmpeg.exe'), str(tmp_path / 'audio/bin/ffprobe.exe')}
    monkeypatch.setattr(process, 'run_process', lambda *args, **kwargs: SimpleNamespace(stdout=b'ffmpeg version different\n'))
    with pytest.raises(ValueError, match='different version'):
        engines.verify_optional_runtime(tmp_path, manifest, 'audio', execute=True)


def test_csl_explicit_runtime_and_bundled_failure_are_not_silently_replaced(tmp_path, monkeypatch):
    from carr_library.academic_integration import formatting
    executable = tmp_path / 'selected-node'
    executable.write_bytes(b'inert')
    def damaged(_):
        raise ValueError('Included runtime is corrupt')
    monkeypatch.setattr(engines, 'bundled_executable', damaged)
    monkeypatch.setattr(formatting.shutil, 'which', lambda _: pytest.fail('A damaged included runtime cannot fall back to PATH'))
    assert formatting._node_binary(executable) == str(executable)
    with pytest.raises(ValueError, match='corrupt'):
        formatting._node_binary(None)
