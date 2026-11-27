"""Native launcher admission and sealing with real Git and synthetic PE bytes.

No native code is executed. The small PE fixture exercises the producer's
structural inspector, exact-source binding and offline bundle boundaries.
"""
import base64
import csv
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('launcher.c', 'launcher_core.h', 'launcher.rc', 'launcher.manifest', 'toolchain.lock.json')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def git(root, *args):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME='Synthetic Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Synthetic Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args],
                                   env=environment, stderr=subprocess.PIPE).decode().strip()


def synthetic_pe():
    data = bytearray(1536)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3C, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0x22)
    optional = 0x98
    struct.pack_into('<H', data, optional, 0x20B)
    struct.pack_into('<HH', data, optional + 68, 2, 0x160)
    struct.pack_into('<II', data, optional + 112 + 8, 0x1000, 40)
    struct.pack_into('<II', data, optional + 112 + 16, 0x1100, 128)
    section = optional + 0xF0
    data[section:section + 8] = b'.rdata\0\0'
    struct.pack_into('<IIII', data, section + 8, 0x400, 0x1000, 0x400, 0x200)
    struct.pack_into('<IIIII', data, 0x200, 0, 0, 0, 0x1080, 0)
    data[0x280:0x28D] = b'KERNEL32.dll\0'
    manifest = b'<requestedExecutionLevel level="asInvoker" uiAccess="false"/><longPathAware>true</longPathAware>'
    data[0x300:0x300 + len(manifest)] = manifest
    return bytes(data)


def write_wheel(path, name, files, *, requires=()):
    prefix = name + '-1.0.dist-info/'
    payload = dict(files)
    payload[prefix + 'METADATA'] = (
        'Metadata-Version: 2.4\nName: ' + name.replace('_', '-') + '\nVersion: 1.0\n'
        + ''.join('Requires-Dist: ' + value + '\n' for value in requires)).encode()
    payload[prefix + 'WHEEL'] = b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    record = prefix + 'RECORD'
    rows = [[name, 'sha256=' + base64.urlsafe_b64encode(sha256(raw).digest()).decode().rstrip('='),
             str(len(raw))] for name, raw in payload.items()]
    rows.append([record, '', ''])
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='\n').writerows(rows)
    payload[record] = stream.getvalue().encode()
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for name, raw in payload.items():
            archive.writestr(name, raw)
    return path


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    repo = tmp_path / 'committed source'
    packaging = repo / 'carr-library/packaging'
    native = packaging / 'native-launcher'
    native.mkdir(parents=True)
    for name in (*SOURCES, 'probe_pe.py'):
        shutil.copyfile(ROOT / 'packaging/native-launcher' / name, native / name)
    for name in ('source_wheel.py', 'install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd'):
        shutil.copyfile(ROOT / 'packaging' / name, packaging / name)
    (packaging / 'wheel-notices').mkdir()
    (packaging / 'wheel-notices/SOURCES.json').write_text('[]\n')
    (packaging / 'runtime-notices').mkdir()
    (packaging / 'runtime-notices/fixture.txt').write_text('Synthetic Python archive; never executed.\n')
    docs = packaging.parent / 'docs'
    docs.mkdir()
    for name in ('RUNNING.md', 'START_UK.txt', 'ACADEMIC_SEARCH.md', 'ADVANCED_SEARCH.md',
                 'ACADEMIC_LIBRARY.md', 'ACADEMIC_WORKFLOWS.md', 'CATALOG_TOOLS.md',
                 'READING_DESK.md', 'RESEARCH_NOTEBOOK.md', 'EPUB_READING.md',
                 'IMPORT_CENTER.md', 'TRANSCRIPTION.md', 'academic_integration.md'):
        (docs / name).write_text('Synthetic packaging instructions.\n')
    package = packaging.parent / 'src/carr_library'
    package.mkdir(parents=True)
    payload = {'carr_library/__init__.py': b'',
               'carr_library/__main__.py': b'raise SystemExit(0)\n'}
    for name, raw in payload.items():
        (package / Path(name).name).write_bytes(raw)
    (packaging.parent / 'pyproject.toml').write_text(
        '[project]\nname="universal-research-library"\nversion="1.0"\n'
        'dependencies=["fixture==1.0"]\n[tool.setuptools]\n'
        'packages=["carr_library"]\npackage-dir={""="src"}\n')
    python = tmp_path / 'python.tar.gz'
    with tarfile.open(python, 'w:gz') as archive:
        raw = b'Synthetic Python executable; never executed.'
        entry = tarfile.TarInfo('python/python.exe')
        entry.size = len(raw)
        archive.addfile(entry, io.BytesIO(raw))
    (packaging / 'windows-python.json').write_text(json.dumps({
        'download': {'sha256': sha256(python.read_bytes()).hexdigest()}}))
    git(repo, 'init', '-q')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Synthetic committed application and native source')
    commit = git(repo, 'rev-parse', 'HEAD')
    exe = tmp_path / 'ResearchLibrary.exe'
    exe.write_bytes(synthetic_pe())
    inspector = module('synthetic_native_pe_probe', native / 'probe_pe.py')
    lock = json.loads((native / 'toolchain.lock.json').read_bytes())
    host = lock['x86_64-linux']
    proof_data = {'schema': 1, 'target': 'x86_64-windows-gnu',
        'release_status': 'unsigned-private-candidate',
        'source_sha256': {name: sha256((native / name).read_bytes()).hexdigest() for name in SOURCES},
        'toolchain': {'version': lock['version'], 'archive_sha256': host['sha256'],
            'archive_bytes': host['bytes'], 'primary_url': host['url'], 'binary_sha256': 'a' * 64},
        'pe': inspector.inspect_pe(exe)}
    proof = tmp_path / 'build-evidence.json'
    proof.write_text(json.dumps(proof_data, sort_keys=True), encoding='utf-8')
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    dep = write_wheel(wheels / 'fixture-1.0-py3-none-any.whl', 'fixture', {})
    app = write_wheel(tmp_path / 'universal_research_library-1.0-py3-none-any.whl',
                      'universal_research_library', payload, requires=('fixture==1.0',))
    runtime_lock = tmp_path / 'windows.lock'
    runtime_lock.write_text('fixture==1.0 --hash=sha256:' + sha256(dep.read_bytes()).hexdigest() + '\n')
    builder = module('native_launcher_bundle_builder', ROOT / 'packaging/build_windows_bundle.py')
    monkeypatch.setattr(builder, 'HERE', packaging)
    monkeypatch.setattr(builder, 'NATIVE_LAUNCHER_SHA256', sha256(exe.read_bytes()).hexdigest())
    installer = module('native_launcher_bundle_installer', ROOT / 'packaging/install_windows.py')
    return SimpleNamespace(repo=repo, native=native, commit=commit, exe=exe, proof=proof,
        proof_data=proof_data, pin=sha256(exe.read_bytes()).hexdigest(), builder=builder,
        installer=installer, app=app, dep=dep, wheels=wheels, lock=runtime_lock,
        python=python, output=tmp_path / 'offline')


def verify(value, **changes):
    arguments = dict(executable=value.exe, proof=value.proof, expected_sha256=value.pin,
                     source_commit=value.commit)
    arguments.update(changes)
    return value.builder.verify_native_launcher(**arguments)


def build(value, **changes):
    arguments = dict(python_archive=value.python, source_commit=value.commit,
                     native_launcher=value.exe, native_launcher_proof=value.proof,
                     native_launcher_sha256=value.pin)
    arguments.update(changes)
    return value.builder.build_bundle(value.app, value.wheels, value.lock, value.output, **arguments)


def test_verifier_captures_exact_bytes_and_reports_no_execution_or_signing_acceptance(candidate):
    evidence = verify(candidate)
    assert evidence['executable_bytes'] == candidate.exe.read_bytes()
    assert evidence['proof_bytes'] == candidate.proof.read_bytes()
    report = evidence['report']
    assert report['source_commit'] == candidate.commit
    assert report['source_tree'] == git(candidate.repo, 'rev-parse', candidate.commit + '^{tree}')
    assert report['source_sha256'] == candidate.proof_data['source_sha256']
    assert report['launcher_sha256'] == candidate.pin
    assert report['native_windows_execution'] == 'NOT_RUN'
    assert report['authenticode_verified'] is False and report['nvda_verified'] is False


@pytest.mark.parametrize('name', SOURCES)
def test_every_build_input_binds_exact_historical_commit_instead_of_checkout(candidate, name):
    path = candidate.native / name
    path.write_bytes(path.read_bytes() + b'\n')
    git(candidate.repo, 'add', '.')
    git(candidate.repo, 'commit', '-qm', 'Changed one native build input')
    newer = git(candidate.repo, 'rev-parse', 'HEAD')
    assert newer != candidate.commit
    assert verify(candidate)['report']['source_commit'] == candidate.commit
    with pytest.raises(ValueError, match='committed source'):
        verify(candidate, source_commit=newer)


@pytest.mark.parametrize('mutation', ['same-size-executable', 'proof-digest', 'proof-size'])
def test_conflicting_executable_identity_is_rejected(candidate, mutation):
    if mutation == 'same-size-executable':
        raw = bytearray(candidate.exe.read_bytes())
        raw[-1] ^= 1
        candidate.exe.write_bytes(raw)
    else:
        candidate.proof_data['pe']['sha256' if mutation == 'proof-digest' else 'bytes'] = (
            '0' * 64 if mutation == 'proof-digest' else 1)
        candidate.proof.write_text(json.dumps(candidate.proof_data))
    with pytest.raises(ValueError):
        verify(candidate)
    assert not candidate.output.exists()


@pytest.mark.parametrize('mutation', ['missing-source', 'extra-source', 'unlocked-toolchain', 'duplicate-key'])
def test_build_proof_cannot_omit_add_or_redeclare_admitted_inputs(candidate, mutation):
    if mutation == 'missing-source':
        del candidate.proof_data['source_sha256']['launcher_core.h']
    elif mutation == 'extra-source':
        candidate.proof_data['source_sha256']['unexpected.dll'] = '0' * 64
    elif mutation == 'unlocked-toolchain':
        candidate.proof_data['toolchain']['archive_sha256'] = '0' * 64
    raw = json.dumps(candidate.proof_data)
    if mutation == 'duplicate-key':
        raw = raw.replace('"schema": 1', '"schema": 1, "schema": 1', 1)
    candidate.proof.write_text(raw)
    with pytest.raises(ValueError):
        verify(candidate)


@pytest.mark.parametrize('mutation', ['architecture', 'console', 'external-dll'])
def test_pinned_bytes_still_require_actual_native_pe_inspection(candidate, mutation):
    raw = bytearray(candidate.exe.read_bytes())
    if mutation == 'architecture':
        struct.pack_into('<H', raw, 0x84, 0x14C)
    elif mutation == 'console':
        struct.pack_into('<H', raw, 0x98 + 68, 3)
    else:
        raw[0x280:0x28D] = b'UNTRUSTED.dll'
    candidate.exe.write_bytes(raw)
    candidate.pin = sha256(raw).hexdigest()
    candidate.proof_data['pe']['sha256'] = candidate.pin
    candidate.proof.write_text(json.dumps(candidate.proof_data))
    with pytest.raises(ValueError):
        verify(candidate)


@pytest.mark.parametrize('mask', [1, 2, 3, 4, 5, 6])
def test_launcher_options_are_all_or_none_before_bundle_publication(candidate, mask):
    arguments = {name: value if mask & (1 << index) else None for index, (name, value) in enumerate((
        ('native_launcher', candidate.exe), ('native_launcher_proof', candidate.proof),
        ('native_launcher_sha256', candidate.pin)))}
    with pytest.raises(ValueError, match='together'):
        build(candidate, **arguments)
    assert not candidate.output.exists()


@pytest.mark.parametrize('missing', ['python_archive', 'source_commit'])
def test_native_launcher_requires_real_source_binding_and_bundled_python(candidate, missing):
    with pytest.raises(ValueError, match='bundled Python|source commit'):
        build(candidate, **{missing: None})
    assert not candidate.output.exists()


def test_release_builder_does_not_admit_an_arbitrary_explicit_pin(candidate, monkeypatch):
    assert verify(candidate)['report']['launcher_sha256'] == candidate.pin
    production = module('fixed_native_release_pin', ROOT / 'packaging/build_windows_bundle.py')
    monkeypatch.setattr(candidate.builder, 'NATIVE_LAUNCHER_SHA256', production.NATIVE_LAUNCHER_SHA256)
    with pytest.raises(ValueError, match='fixed Windows-tested executable pin'):
        build(candidate)
    assert not candidate.output.exists()


def test_real_bundle_seals_launcher_proof_and_exact_application_source(candidate):
    build(candidate)
    candidate.installer.verify(candidate.output)
    manifest = json.loads((candidate.output / 'bundle-manifest.json').read_bytes())
    assert manifest['bundled_native_launcher'] is True
    assert manifest['native_launcher_sha256'] == candidate.pin
    assert manifest['native_launcher_source_commit'] == manifest['source_commit'] == candidate.commit
    assert manifest['source_tree'] == git(candidate.repo, 'rev-parse', candidate.commit + '^{tree}')
    assert (candidate.output / 'ResearchLibrary.exe').read_bytes() == candidate.exe.read_bytes()
    assert (candidate.output / 'native-launcher-build.json').read_bytes() == candidate.proof.read_bytes()
    for name in ('ResearchLibrary.exe', 'native-launcher-build.json'):
        assert manifest['files'][name] == sha256((candidate.output / name).read_bytes()).hexdigest()
    assert not any('windows_child_probe' in name or 'zig' in name for name in manifest['files'])


@pytest.mark.parametrize('mutation', ['omit-exe', 'omit-proof', 'alter-exe', 'alter-proof',
                                     'case-alias', 'source-identity'])
def test_installer_rejects_missing_changed_or_aliased_native_payload(candidate, mutation):
    build(candidate)
    exe = candidate.output / 'ResearchLibrary.exe'
    proof = candidate.output / 'native-launcher-build.json'
    if mutation.startswith('omit-'):
        (exe if mutation == 'omit-exe' else proof).unlink()
    elif mutation.startswith('alter-'):
        (exe if mutation == 'alter-exe' else proof).write_bytes(b'Unexpected replacement')
    elif mutation == 'case-alias':
        exe.rename(exe.with_name('RESEARCHLIBRARY.EXE'))
    else:
        path = candidate.output / 'bundle-manifest.json'
        manifest = json.loads(path.read_bytes())
        manifest['native_launcher_source_commit'] = '0' * 40
        path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        candidate.installer.verify(candidate.output)


def test_old_bundle_contract_remains_valid_and_rejects_unannounced_native_files(candidate):
    build(candidate, native_launcher=None, native_launcher_proof=None, native_launcher_sha256=None)
    candidate.installer.verify(candidate.output)
    manifest = json.loads((candidate.output / 'bundle-manifest.json').read_bytes())
    assert not manifest.get('bundled_native_launcher', False)
    assert not (candidate.output / 'ResearchLibrary.exe').exists()
    (candidate.output / 'ResearchLibrary.exe').write_bytes(candidate.exe.read_bytes())
    with pytest.raises(ValueError, match='unannounced'):
        candidate.installer.verify(candidate.output)


def test_mutable_inputs_after_verification_cannot_change_sealed_snapshot(candidate, monkeypatch):
    executable, proof = candidate.exe.read_bytes(), candidate.proof.read_bytes()
    actual_copy = candidate.builder.shutil.copyfile
    changed = []

    def copy_after_replacement(source, destination, *args, **kwargs):
        if Path(source) == candidate.dep and not changed:
            candidate.exe.write_bytes(b'Unverified later binary')
            candidate.proof.write_bytes(b'Unverified later proof')
            changed.append(True)
        return actual_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(candidate.builder.shutil, 'copyfile', copy_after_replacement)
    build(candidate)
    assert changed == [True]
    assert candidate.exe.read_bytes() != executable and candidate.proof.read_bytes() != proof
    assert (candidate.output / 'ResearchLibrary.exe').read_bytes() == executable
    assert (candidate.output / 'native-launcher-build.json').read_bytes() == proof
    candidate.installer.verify(candidate.output)
