"""Sealed private audio bundle boundaries; synthetic PE fixtures never execute."""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from zipfile import ZipFile

import pytest

from carr_library.audio import runtime


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


packager = module('add_audio_runtime')
installer = module('install_windows')


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_outer(bundle, value=None):
    value = value or json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    (bundle / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    return value


@pytest.fixture
def audio(tmp_path, monkeypatch):
    root = tmp_path / 'reviewed audio Ω'
    root.mkdir()
    pe = bytearray(134)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 60, 128)
    pe[128:] = b'PE\0\0\x64\x86'
    pins = {}
    for name in runtime.FILES:
        path = root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(bytes(pe) if name.endswith('.exe') else b'Synthetic license/README test fixture\n')
        pins[name] = dict(size=path.stat().st_size, sha256=digest(path))
    monkeypatch.setattr(runtime, 'FILES', pins)
    (root / runtime.MANIFEST).write_text(json.dumps(runtime.manifest()), encoding='utf-8')
    return root


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / 'sealed bundle Ω'
    root.mkdir()
    for name in ('install.lock', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd', 'README.txt'):
        (root / name).write_text('Synthetic sealed installation fixture.\n', encoding='utf-8')
    shutil.copyfile(ROOT / 'packaging/install_windows.py', root / 'install_windows.py')
    wheel = root / 'wheelhouse/universal_research_library-0.3.0.dev1-py3-none-any.whl'
    wheel.parent.mkdir()
    with ZipFile(wheel, 'w') as out:
        out.writestr('carr_library/audio/runtime.py', '# Synthetic test application entry.\n')
        out.writestr('carr_library/audio/ffmpeg.py', '# Synthetic test audio module.\n')
    value = dict(schema=1, target='Windows x64 / CPython 3.11', bundled_python=False,
                 bundled_model=False, bundled_ocr=False, bundled_native_runtime=False,
                 files={p.relative_to(root).as_posix(): digest(p) for p in root.rglob('*') if p.is_file()})
    write_outer(root, value)
    return root


@pytest.fixture
def attached(bundle, audio, tmp_path):
    output = tmp_path / 'with audio Ω'
    report = packager.attach_runtime(bundle, audio, output)
    return output, report


def test_pinned_runtime_metadata_and_packaged_inventory_verify_without_execution(attached, audio):
    output, report = attached
    installer.verify(output)
    actual = runtime.verify_runtime(output / runtime.DIRECTORY)
    assert actual['status'] == 'PASS' and actual['files'] == 4
    assert report['native_windows_execution'] == 'NOT_RUN'
    assert report['manifest_sha256'] == digest(output / 'bundle-manifest.json')
    assert report['audio_runtime']['root'] == str(output / runtime.DIRECTORY)
    for name in (*runtime.FILES, runtime.MANIFEST):
        assert (output / runtime.DIRECTORY / name).read_bytes() == (audio / name).read_bytes()


def test_only_sealed_inventory_copied_and_sources_remain_unchanged(bundle, audio, tmp_path):
    (bundle / 'private.sqlite').write_bytes(b'Unlisted private research data.')
    (bundle / '.venv').mkdir()
    (bundle / '.venv/private-cache').write_bytes(b'Unlisted environment cache.')
    before = (bundle / 'bundle-manifest.json').read_bytes()
    output = tmp_path / 'private-excluded'
    packager.attach_runtime(bundle, audio, output)
    assert not (output / 'private.sqlite').exists() and not (output / '.venv').exists()
    assert (bundle / 'bundle-manifest.json').read_bytes() == before
    assert (bundle / 'private.sqlite').read_bytes() == b'Unlisted private research data.'


def test_existing_native_model_ocr_flags_and_inventory_are_preserved(bundle, audio, tmp_path):
    value = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    extras = ['runtime/python.exe', 'native-runtime/native-manifest.json',
              'native-runtime/bin/msvcp140.dll', 'native-runtime/bin/msvcp140_1.dll',
              'ocr/ocr-manifest.json', 'ocr/tesseract.exe', 'ocr/tessdata/eng.traineddata',
              'model/model-manifest.json', 'model/model.onnx']
    for name in extras:
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic preserved supplement\n')
        value['files'][name] = digest(path)
    value.update(bundled_python=True, bundled_native_runtime=True, bundled_ocr=True, bundled_model=True)
    write_outer(bundle, value)
    output = tmp_path / 'all-existing-supplements'
    packager.attach_runtime(bundle, audio, output)
    result = json.loads((output / 'bundle-manifest.json').read_text(encoding='utf-8'))
    for name in extras:
        assert result['files'][name] == value['files'][name]
        assert (output / name).read_bytes() == (bundle / name).read_bytes()
    assert all(result[k] for k in ('bundled_python', 'bundled_native_runtime', 'bundled_ocr', 'bundled_model'))


@pytest.mark.parametrize('flag', [1, 'yes', None, [], {}])
def test_audio_flag_must_be_a_boolean(bundle, flag):
    value = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    value['bundled_audio_runtime'] = flag
    write_outer(bundle, value)
    with pytest.raises(ValueError, match='audio runtime installation flag'):
        installer.verify(bundle)


def test_legacy_and_explicit_absence_work_without_changing_other_flags(bundle):
    installer.verify(bundle)
    value = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    value['bundled_audio_runtime'] = False
    write_outer(bundle, value)
    installer.verify(bundle)
    (bundle / 'audio-runtime').mkdir()
    with pytest.raises(ValueError, match='unannounced'):
        installer.verify(bundle)


@pytest.mark.parametrize('mutation', ['missing', 'extra_file', 'extra_directory', 'false_flag', 'missing_flag'])
def test_installer_rejects_unannounced_or_incomplete_audio_inventory(attached, mutation):
    output, _ = attached
    value = json.loads((output / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if mutation == 'missing':
        del value['files']['audio-runtime/LICENSE']
        (output / 'audio-runtime/LICENSE').unlink()
    elif mutation == 'extra_file':
        (output / 'audio-runtime/private.txt').write_text('Unlisted', encoding='utf-8')
    elif mutation == 'extra_directory':
        (output / 'audio-runtime/empty-extra').mkdir()
    elif mutation == 'false_flag':
        value['bundled_audio_runtime'] = False
    else:
        del value['bundled_audio_runtime']
    write_outer(output, value)
    with pytest.raises(ValueError, match='audio runtime'):
        installer.verify(output)


@pytest.mark.parametrize('mutation', ['boolean_schema', 'boolean_size', 'wrong_digest', 'wrong_scope', 'duplicate'])
def test_installer_binds_inner_manifest_to_exact_sealed_inventory(attached, mutation):
    output, _ = attached
    path = output / 'audio-runtime/audio-runtime.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if mutation == 'boolean_schema':
        value['schema'] = True
    elif mutation == 'boolean_size':
        value['files']['LICENSE']['size'] = True
    elif mutation == 'wrong_digest':
        value['files']['LICENSE']['sha256'] = '0' * 64
    elif mutation == 'wrong_scope':
        value['use_scope'] = 'unknown-source'
    raw = json.dumps(value)
    if mutation == 'duplicate':
        raw = '{"schema":1,' + raw[1:]
    path.write_text(raw, encoding='utf-8')
    outer = json.loads((output / 'bundle-manifest.json').read_text(encoding='utf-8'))
    outer['files']['audio-runtime/audio-runtime.json'] = digest(path)
    write_outer(output, outer)
    with pytest.raises(ValueError, match='audio runtime'):
        installer.verify(output)


def test_runtime_tamper_is_rejected_even_with_resealed_inner_manifest(audio, bundle, tmp_path):
    executable = audio / 'bin/ffmpeg.exe'
    raw = bytearray(executable.read_bytes())
    raw[75] ^= 1
    executable.write_bytes(raw)
    value = runtime.manifest()
    value['files']['bin/ffmpeg.exe']['sha256'] = digest(executable)
    (audio / runtime.MANIFEST).write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='reviewed package'):
        packager.attach_runtime(bundle, audio, tmp_path / 'tampered-runtime')
    assert not (tmp_path / 'tampered-runtime').exists()


def test_runtime_unknown_file_cannot_be_collected(audio, bundle, tmp_path):
    (audio / 'private.sqlite').write_bytes(b'Unapproved adjacent private database.')
    with pytest.raises(ValueError, match='inventory'):
        packager.attach_runtime(bundle, audio, tmp_path / 'unexpected-runtime')
    assert not (tmp_path / 'unexpected-runtime').exists()


def test_source_changed_during_copy_is_rejected(bundle, audio, tmp_path, monkeypatch):
    original_copy = packager.shutil.copyfile
    changed = False
    def change_before_copy(source, target, *args, **kwargs):
        nonlocal changed
        if Path(source) == bundle / 'README.txt' and not changed:
            changed = True
            Path(source).write_text('Changed after initial verification.', encoding='utf-8')
        return original_copy(source, target, *args, **kwargs)
    monkeypatch.setattr(packager.shutil, 'copyfile', change_before_copy)
    with pytest.raises(ValueError, match='changed while'):
        packager.attach_runtime(bundle, audio, tmp_path / 'raced-source')
    assert changed and not (tmp_path / 'raced-source').exists()


def test_old_application_wheel_is_rejected_before_output(bundle, audio, tmp_path):
    wheel, = (bundle / 'wheelhouse').iterdir()
    with ZipFile(wheel, 'w') as out:
        out.writestr('carr_library/__main__.py', '# Old synthetic application.\n')
    value = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    value['files'][wheel.relative_to(bundle).as_posix()] = digest(wheel)
    write_outer(bundle, value)
    with pytest.raises(ValueError, match='Rebuild'):
        packager.attach_runtime(bundle, audio, tmp_path / 'old-app')
    assert not (tmp_path / 'old-app').exists()


def test_duplicate_attachment_existing_output_and_nested_output_are_refused(bundle, audio, attached, tmp_path):
    attached_bundle, _ = attached
    with pytest.raises(ValueError, match='already contains'):
        packager.attach_runtime(attached_bundle, audio, tmp_path / 'second-audio')
    with pytest.raises(FileExistsError):
        packager.attach_runtime(bundle, audio, attached_bundle)
    for output in (bundle / 'nested', audio / 'nested'):
        with pytest.raises(ValueError, match='outside'):
            packager.attach_runtime(bundle, audio, output)


def test_cli_contract_reports_explicit_nonexecution(bundle, audio, tmp_path, capsys):
    output = tmp_path / 'cli-output'
    assert packager.main(['--bundle', str(bundle), '--audio-runtime', str(audio), '--output', str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['output'] == str(output) and report['native_windows_execution'] == 'NOT_RUN'


def test_symlinked_runtime_or_unavailable_host_privilege_is_explicit(bundle, audio, tmp_path):
    alias = tmp_path / 'audio-alias'
    try:
        alias.symlink_to(audio, target_is_directory=True)
    except OSError as exc:
        assert os.name == 'nt' and exc.winerror == 1314
        with pytest.raises(ValueError, match='Network|device'):
            packager.attach_runtime(bundle, '\\\\server\\audio', tmp_path / 'denied')
        return
    with pytest.raises(ValueError, match='ordinary'):
        packager.attach_runtime(bundle, alias, tmp_path / 'linked-runtime')


def test_prefix_discovery_does_not_use_cwd(audio, tmp_path, monkeypatch):
    root = tmp_path / 'installed'
    root.mkdir()
    shutil.copytree(audio, root / 'audio-runtime')
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(sys, 'prefix', str(root / '.venv'))
    monkeypatch.setattr(sys, 'base_prefix', str(root / 'runtime'))
    # Linux cannot invoke a real Windows drive API. Only path-to-runtime
    # selection is mocked; the copied fixed inventory is verified for real.
    monkeypatch.setattr('carr_library.audio.ffmpeg._local_path', lambda value: Path(value))
    assert runtime.discover_runtime() == root / 'audio-runtime/bin'
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / 'another-python'))
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path / 'another-python'))
    monkeypatch.chdir(root)
    assert runtime.discover_runtime() is None
