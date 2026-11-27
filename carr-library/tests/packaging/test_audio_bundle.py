"""Small sealed fixtures exercise publication boundaries without native execution."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import zipfile

import pytest

from carr_library import audio_runtime as runtime, native_checks


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('carr_add_audio_runtime', ROOT / 'packaging/add_audio_runtime.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def binary():
    raw = bytearray(256)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 60, 128)
    raw[128:132] = b'PE\0\0'
    struct.pack_into('<H', raw, 132, 0x8664)
    return bytes(raw)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    bundle = tmp_path / 'original program ž Україна'
    bundle.mkdir()
    for name in ('install.lock', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd', 'README.txt',
                 'runtime/python.exe', 'native-runtime/native-manifest.json',
                 'native-runtime/bin/msvcp140.dll', 'native-runtime/bin/msvcp140_1.dll'):
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic base inventory, never executed.\n')
    (bundle / 'install_windows.py').write_bytes((ROOT / 'packaging/install_windows.py').read_bytes())
    wheel = bundle / 'wheelhouse/universal_research_library-0-py3-none-any.whl'
    wheel.parent.mkdir()
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('carr_library/audio_runtime.py', (ROOT / 'src/carr_library/audio_runtime.py').read_bytes())
    manifest = {'schema': 1, 'target': 'Windows x64 / CPython 3.11', 'source_commit': 'a' * 40,
                'source_tree': 'b' * 40, 'bundled_python': True, 'bundled_native_runtime': True,
                'bundled_model': False, 'bundled_ocr': False,
                'files': {p.relative_to(bundle).as_posix(): digest(p.read_bytes())
                          for p in bundle.rglob('*') if p.is_file()}}
    (bundle / 'bundle-manifest.json').write_bytes(encode(manifest))
    # Unsealed local data must never become part of a later installation.
    (bundle / 'private-database.sqlite').write_bytes(b'Private, unlisted fixture.')
    (bundle / '.venv').mkdir()
    (bundle / '.venv/local.txt').write_bytes(b'Local environment fixture.')
    audio = tmp_path / 'supplied audio'
    audio.mkdir()
    payload = {name: binary() for name in runtime.TOOLS.values()}
    payload['LICENSE.txt'] = b'Synthetic-only license fixture.\n'
    lock = {'schema': 1, 'package_id': runtime.PACKAGE_ID, 'target': 'Windows x64',
            'version': 'synthetic-test-only', 'executables': runtime.TOOLS,
            'archive_files': {name: {'size': len(raw), 'sha256': digest(raw)} for name, raw in payload.items()},
            'extra_files': {}}
    raw = encode(lock)
    monkeypatch.setattr(runtime, 'SOURCE_LOCK_SHA256', digest(raw))
    payload['provenance/source-lock.json'] = raw
    for name, raw in payload.items():
        path = audio / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    inner = {'schema': 1, 'target': 'Windows x64', 'package_id': runtime.PACKAGE_ID,
             'source_lock_sha256': runtime.SOURCE_LOCK_SHA256,
             'files': {name: digest(raw) for name, raw in payload.items()}}
    (audio / runtime.MANIFEST).write_bytes(encode(inner))
    return bundle, audio, tmp_path / 'new program', manifest


def test_attach_preserves_exact_source_and_every_sealed_byte_without_collecting_local_data(inputs):
    bundle, audio, output, source = inputs
    original = (bundle / 'bundle-manifest.json').read_bytes()
    report = helper.attach_runtime(bundle, audio, output)
    actual = json.loads((output / 'bundle-manifest.json').read_bytes())
    assert actual['source_commit'] == source['source_commit'] and actual['source_tree'] == source['source_tree']
    assert actual['bundled_audio_runtime'] is True and actual['bundled_native_runtime'] is True
    assert report['source_manifest_sha256'] == digest(original)
    assert report['manifest_sha256'] == digest((output / 'bundle-manifest.json').read_bytes())
    assert report['base_files'] == len(source['files']) and report['audio_files'] == 6
    assert (bundle / 'bundle-manifest.json').read_bytes() == original
    assert not (output / 'private-database.sqlite').exists() and not (output / '.venv').exists()
    for name, expected in source['files'].items():
        assert actual['files'][name] == expected
        assert (output / name).read_bytes() == (bundle / name).read_bytes()
    assert runtime.bundled_audio_tools(output) == report['audio_runtime']['tools']
    assert report['native_windows_execution'] == report['audio_runtime']['native_execution'] == 'NOT_RUN'
    helper._installer().verify(output)


@pytest.mark.parametrize('change', [None, 'changed_tool', 'unannounced_audio', 'missing_audio'])
def test_produced_audio_bundle_is_consumed_by_installation_check(inputs, monkeypatch, change):
    bundle, audio, output, _ = inputs
    helper.attach_runtime(bundle, audio, output)
    manifest = native_checks._manifest(output)
    assert manifest['source_tree'] == 'b' * 40
    assert manifest['bundled_audio_runtime'] is True
    if change == 'changed_tool':
        (output / 'audio-runtime/bin/ffmpeg.exe').write_bytes(b'Changed executable')
    elif change == 'unannounced_audio':
        manifest['bundled_audio_runtime'] = False
        (output / 'bundle-manifest.json').write_bytes(encode(manifest))
    elif change == 'missing_audio':
        (output / 'audio-runtime/bin/ffprobe.exe').unlink()

    # Native DLLs are synthetic in this fixture. The audio verifier, inventory,
    # files, hashes and builder/consumer round trip are the actual implementation.
    def load(name):
        if name == 'carr_library.native_runtime':
            return SimpleNamespace(activate_bundled_runtime=lambda **kwargs: None)
        if name == 'carr_library.audio_runtime':
            return runtime
        if name in {module for _, module in native_checks.NATIVE_MODULES}:
            return SimpleNamespace()
        pytest.fail('Unexpected dependency import: ' + name)
    monkeypatch.setattr(native_checks, 'import_module', load)
    report = native_checks.check_native_dependencies(output)
    assert report['status'] == ('FAIL' if change else 'PASS')
    if change:
        assert report['failed_component'] == 'bundled audio runtime'
        assert report['audio']['status'] == 'FAIL'
    else:
        assert report['audio'] == {'status': 'PASS', 'integrity': 'sha256', 'native_execution': 'NOT_RUN'}


def test_existing_output_is_preserved(inputs):
    bundle, audio, output, _ = inputs
    output.mkdir()
    (output / 'existing.txt').write_bytes(b'Existing user file')
    with pytest.raises(FileExistsError, match='new output'):
        helper.attach_runtime(bundle, audio, output)
    assert (output / 'existing.txt').read_bytes() == b'Existing user file'


@pytest.mark.parametrize('inside', ['bundle', 'audio'])
def test_output_must_be_outside_both_inputs(inputs, inside):
    bundle, audio, _, _ = inputs
    output = (bundle if inside == 'bundle' else audio) / 'nested'
    with pytest.raises(ValueError, match='outside both'):
        helper.attach_runtime(bundle, audio, output)
    assert not output.exists()


def test_manifest_rebound_after_installer_verification_is_rejected(inputs, monkeypatch):
    bundle, audio, output, manifest = inputs
    installer = helper._installer()
    def rebound(root):
        installer.verify(root)
        manifest['source_commit'] = 'c' * 40
        (root / 'bundle-manifest.json').write_bytes(encode(manifest))
    monkeypatch.setattr(helper, '_installer', lambda: SimpleNamespace(verify=rebound))
    with pytest.raises(ValueError, match='manifest changed during verification'):
        helper.attach_runtime(bundle, audio, output)
    assert not output.exists()


def test_changing_a_previously_verified_file_during_copy_never_publishes(inputs, monkeypatch):
    bundle, audio, output, _ = inputs
    copy = helper._copy_file
    def changed(source, target, expected):
        if source == bundle / 'README.txt':
            source.write_bytes(b'Changed after inventory verification.')
        copy(source, target, expected)
    monkeypatch.setattr(helper, '_copy_file', changed)
    with pytest.raises(ValueError, match='changed while being copied'):
        helper.attach_runtime(bundle, audio, output)
    assert not output.exists()


def test_tampered_audio_runtime_is_rejected_before_output(inputs):
    bundle, audio, output, _ = inputs
    (audio / 'bin/ffmpeg.exe').write_bytes(b'Tampered executable')
    with pytest.raises(ValueError):
        helper.attach_runtime(bundle, audio, output)
    assert not output.exists()


def test_repeat_attachment_is_rejected_without_changing_previous_package(inputs):
    bundle, audio, output, _ = inputs
    helper.attach_runtime(bundle, audio, output)
    original = (output / 'bundle-manifest.json').read_bytes()
    second = output.with_name('second')
    with pytest.raises(ValueError, match='already contains'):
        helper.attach_runtime(output, audio, second)
    assert not second.exists() and (output / 'bundle-manifest.json').read_bytes() == original


def test_interrupted_publication_removes_only_its_incomplete_output(inputs, monkeypatch):
    bundle, audio, output, _ = inputs
    move = helper.shutil.move
    def interrupted(source, destination):
        if Path(source).name == 'bundle-manifest.json':
            assert not (output / 'bundle-manifest.json').exists()
            raise OSError('Synthetic final manifest publication failure')
        return move(source, destination)
    monkeypatch.setattr(helper.shutil, 'move', interrupted)
    with pytest.raises(OSError, match='publication failure'):
        helper.attach_runtime(bundle, audio, output)
    assert not output.exists()
    assert (bundle / 'private-database.sqlite').read_bytes() == b'Private, unlisted fixture.'


@pytest.mark.parametrize('field', ['source_commit', 'source_tree'])
def test_missing_exact_source_identity_fails_before_copy(inputs, field):
    bundle, audio, output, manifest = inputs
    del manifest[field]
    (bundle / 'bundle-manifest.json').write_bytes(encode(manifest))
    with pytest.raises(ValueError, match='exact source identity'):
        helper.attach_runtime(bundle, audio, output)
    assert not output.exists()
