"""Real Git/ZIP/publication boundaries with an inert PE envelope, never executed."""
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
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from carr_library import native_checks


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


builder = load('launcher_bundle_builder', ROOT / 'packaging/build_windows_bundle.py')
installer = load('launcher_bundle_installer', ROOT / 'packaging/install_windows.py')
probe = load('launcher_boundary_pe_probe', ROOT / 'packaging/native-launcher/probe_pe.py')


def git(repo, *args):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME='Synthetic Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Synthetic Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-C', str(repo), *args],
                                   env=environment, text=True).strip()


def wheel(folder, name, payload=None, requires=()):
    files = dict(payload or {})
    prefix = name + '-1.0.dist-info/'
    files[prefix + 'METADATA'] = (f'Metadata-Version: 2.4\nName: {name.replace("_", "-")}\nVersion: 1.0\n'
                                  + ''.join('Requires-Dist: ' + value + '\n' for value in requires)).encode()
    files[prefix + 'WHEEL'] = b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    record = prefix + 'RECORD'
    rows = [[relative, 'sha256=' + base64.urlsafe_b64encode(sha256(content).digest()).decode().rstrip('='),
             str(len(content))] for relative, content in files.items()]
    rows.append([record, '', ''])
    output = io.StringIO(newline='')
    csv.writer(output, lineterminator='\n').writerows(rows)
    files[record] = output.getvalue().encode()
    path = folder / (name + '-1.0-py3-none-any.whl')
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for relative, content in files.items():
            archive.writestr(relative, content)
    return path


def inert_pe():
    """Only a structural parser fixture, with no executable machine code."""
    data = bytearray(1536)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 1, 0, 0, 0, 240, 0x22)
    optional = 0x98
    struct.pack_into('<H', data, optional, 0x20b)
    struct.pack_into('<HH', data, optional + 68, 2, 0x160)
    struct.pack_into('<II', data, optional + 112 + 8, 0x1020, 40)
    struct.pack_into('<II', data, optional + 112 + 16, 0x1100, 64)
    section = optional + 240
    data[section:section + 8] = b'.rdata\0\0'
    struct.pack_into('<IIII', data, section + 8, 1024, 0x1000, 1024, 0x200)
    struct.pack_into('<IIIII', data, 0x220, 0, 0, 0, 0x1080, 0)
    data[0x280:0x280 + 13] = b'kernel32.dll\0'
    marker = b'<assembly level="asInvoker" uiAccess="false">longPathAware</assembly> INERT TEST FIXTURE'
    data[0x300:0x300 + len(marker)] = marker
    return bytes(data)


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    repo = tmp_path / 'source repo'
    package = repo / 'carr-library'
    native = package / 'packaging/native-launcher'
    native.mkdir(parents=True)
    names = ('launcher.c', 'launcher_core.h', 'launcher.rc', 'launcher.manifest', 'probe_pe.py')
    for name in names:
        shutil.copyfile(ROOT / 'packaging/native-launcher' / name, native / name)
    for name in ('source_wheel.py', 'install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd'):
        shutil.copyfile(ROOT / 'packaging' / name, package / 'packaging' / name)
    (package / 'packaging/wheel-notices').mkdir()
    (package / 'packaging/wheel-notices/SOURCES.json').write_text('[]', encoding='utf-8')
    (package / 'docs').mkdir()
    for name in ('RUNNING.md', 'START_UK.txt', 'START_V3_UK.txt',
                 'ACADEMIC_SEARCH.md', 'ADVANCED_SEARCH.md', 'ACADEMIC_LIBRARY.md',
                 'ACADEMIC_WORKFLOWS.md', 'CATALOG_TOOLS.md', 'READING_DESK.md',
                 'RESEARCH_NOTEBOOK.md', 'EPUB_READING.md', 'IMPORT_CENTER.md',
                 'TRANSCRIPTION.md', 'recording.md',
                 'LOCAL_AUDIO_RUNTIME_SETUP.md',
                 'LOCAL_VOICES.md',
                 'VOICE_REFERENCE_REVIEW.md',
                 'READING_PREPARATION.md',
                 'BOOK_AUDIO_BATCH.md',
                 'INCREMENTAL_BACKUP.md',
                 'STUDIO_EPUB_PUBLISHING.md',
                 'SEARCH_PAGINATION.md',
                 'WORD_NATIVE_INTEGRATION.md',
                 'ASSISTANT_DOMAINS.md',
                 'PORTABLE_SNAPSHOTS.md',
                 'SYNC_DEPENDENCIES.md'):
        (package / 'docs' / name).write_text('Synthetic packaging boundary fixture.\n', encoding='utf-8')
    (package / 'pyproject.toml').write_text('''[project]
name = "universal-research-library"
version = "1.0"
dependencies = ["fixture==1.0"]
[tool.setuptools]
packages = ["carr_library"]
package-dir = {"" = "src"}
''', encoding='utf-8')
    source = package / 'src/carr_library'
    source.mkdir(parents=True)
    (source / '__init__.py').write_bytes(b'')
    (source / '__main__.py').write_bytes(b'print("Synthetic wheel; never executed")\n')
    git(repo, 'init', '-q')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Synthetic committed launcher boundary')
    commit = git(repo, 'rev-parse', 'HEAD')
    monkeypatch.setattr(builder, 'HERE', package / 'packaging')
    wheelhouse = tmp_path / 'wheelhouse'
    wheelhouse.mkdir()
    dependency = wheel(wheelhouse, 'fixture')
    lock = tmp_path / 'install.lock'
    lock.write_text('fixture==1.0 --hash=sha256:' + builder.sha256(dependency) + '\n')
    application = wheel(tmp_path, 'universal_research_library',
                        {'carr_library/' + p.name: p.read_bytes() for p in source.iterdir()},
                        requires=('fixture==1.0',))
    launcher = tmp_path / 'ResearchLibrary.exe'
    launcher.write_bytes(inert_pe())
    evidence = tmp_path / 'build-evidence.json'
    document = dict(schema=1, target='Windows x64 / MSVC',
                    toolchain={'synthetic': 'Boundary fixture; no compilation or native execution'},
                    source_sha256={name: builder.sha256(native / name) for name in names},
                    pe=probe.inspect_pe(launcher), release_status='unsigned-private-candidate')
    evidence.write_text(json.dumps(document), encoding='utf-8-sig')
    output = tmp_path / 'complete'
    def build(**options):
        defaults = dict(source_commit=commit, launcher=launcher, launcher_evidence=evidence)
        defaults.update(options)
        return builder.build_bundle(application, wheelhouse, lock, output, **defaults)
    return SimpleNamespace(repo=repo, package=package, native=native, commit=commit,
                           launcher=launcher, evidence=evidence, document=document,
                           output=output, build=build)


def test_bom_evidence_and_real_manifest_seal_only_the_launcher(candidate):
    expected = candidate.launcher.read_bytes()
    (candidate.launcher.parent / 'windows_child_probe.exe').write_bytes(b'NEVER INCLUDE THIS PROBE')
    result = candidate.build()
    manifest = native_checks._manifest(candidate.output)
    installer.verify(candidate.output)
    assert manifest['bundled_audio'] is False
    assert 'bundled_launcher' not in manifest and 'launcher' not in manifest
    assert manifest['files']['ResearchLibrary.exe'] == sha256(expected).hexdigest()
    assert (candidate.output / 'ResearchLibrary.exe').read_bytes() == expected
    assert not {'windows_child_probe.exe', 'build-evidence.json', 'launcher.c', 'zig.exe'} & set(manifest['files'])
    assert result['launcher']['source_commit'] == candidate.commit
    assert result['launcher']['release_status'] == 'unsigned-private-candidate'
    assert result['launcher']['native_execution'] == 'NOT_RUN'
    assert result['launcher']['pe']['authenticode_signed'] is False
    (candidate.output / 'ResearchLibrary.exe').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        installer.verify(candidate.output)


def test_legacy_caller_without_launcher_remains_a_valid_bundle(candidate):
    result = candidate.build(launcher=None, launcher_evidence=None)
    installer.verify(candidate.output)
    native_checks._manifest(candidate.output)
    assert 'launcher' not in result
    assert not (candidate.output / 'ResearchLibrary.exe').exists()


def test_historical_source_identity_is_independent_of_current_checkout(candidate):
    (candidate.native / 'launcher.c').write_bytes(b'Changed source at a later commit\n')
    git(candidate.repo, 'add', '.')
    git(candidate.repo, 'commit', '-qm', 'Later launcher source')
    later = git(candidate.repo, 'rev-parse', 'HEAD')
    payload, report = builder.verify_launcher(candidate.launcher, candidate.evidence, candidate.commit)
    assert payload == candidate.launcher.read_bytes() and report['source_commit'] == candidate.commit
    with pytest.raises(ValueError, match='committed source'):
        builder.verify_launcher(candidate.launcher, candidate.evidence, later)


def test_pe_inspector_executes_only_its_verified_source_snapshot(candidate, monkeypatch):
    capture = builder._launcher_file
    def change_after_capture(value, maximum):
        content = capture(value, maximum)
        if Path(value) == candidate.native / 'probe_pe.py':
            Path(value).write_bytes(b'raise AssertionError("unverified inspector reread")\n')
        return content
    monkeypatch.setattr(builder, '_launcher_file', change_after_capture)
    payload, report = builder.verify_launcher(candidate.launcher, candidate.evidence, candidate.commit)
    assert report['pe']['sha256'] == sha256(payload).hexdigest()


def test_original_pe_replacement_cannot_change_already_verified_payload(candidate, monkeypatch):
    expected = candidate.launcher.read_bytes()
    verify = builder.verify_launcher
    def replace_after_verification(*args):
        result = verify(*args)
        candidate.launcher.write_bytes(b'Unapproved replacement after validation')
        return result
    monkeypatch.setattr(builder, 'verify_launcher', replace_after_verification)
    candidate.build()
    assert (candidate.output / 'ResearchLibrary.exe').read_bytes() == expected
    installer.verify(candidate.output)


@pytest.mark.parametrize('change', ['missing_source', 'wrong_source', 'traversal_source', 'unknown_source',
    'pe_hash', 'pe_size', 'execution_claim', 'signed_claim', 'boolean_schema', 'duplicate_json', 'nonfinite_json'])
def test_invalid_evidence_cannot_publish_a_bundle(candidate, change):
    document = candidate.document
    if change == 'missing_source':
        del document['source_sha256']['launcher.c']
    elif change == 'wrong_source':
        document['source_sha256']['launcher.c'] = '0' * 64
    elif change in ('traversal_source', 'unknown_source'):
        document['source_sha256']['../outside.c' if change == 'traversal_source' else 'injected.c'] = 'a' * 64
    elif change == 'pe_hash':
        document['pe']['sha256'] = '0' * 64
    elif change == 'pe_size':
        document['pe']['bytes'] += 1
    elif change == 'execution_claim':
        document['pe']['windows_execution_verified'] = True
    elif change == 'signed_claim':
        document['release_status'] = 'signed-release'
    elif change == 'boolean_schema':
        document['schema'] = True
    raw = json.dumps(document)
    if change == 'duplicate_json':
        raw = '{"schema":1,' + raw[1:]
    elif change == 'nonfinite_json':
        raw = raw.replace('"schema": 1', '"schema": NaN')
    candidate.evidence.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError):
        candidate.build()
    assert not candidate.output.exists()


@pytest.mark.parametrize('data', [b'MZ', b'not a PE', b'MZ' + bytes(100)])
def test_malformed_pe_never_reaches_publication(candidate, data):
    candidate.launcher.write_bytes(data)
    with pytest.raises(ValueError):
        candidate.build()
    assert not candidate.output.exists()


@pytest.mark.parametrize('kind', ['pe', 'evidence'])
def test_oversized_inputs_are_rejected_before_open(candidate, monkeypatch, kind):
    path = candidate.launcher if kind == 'pe' else candidate.evidence
    with path.open('wb') as stream:
        stream.truncate((2 * 1024**2 if kind == 'pe' else 128 * 1024) + 1)
    original = Path.open
    def guarded(current, *args, **kwargs):
        assert current != path, 'Oversized launcher input was opened.'
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    with pytest.raises(ValueError, match='byte allowance'):
        candidate.build()
    assert not candidate.output.exists()


@pytest.mark.parametrize('missing', ['launcher', 'launcher_evidence', 'source_commit'])
def test_launcher_requires_paired_evidence_and_exact_commit(candidate, missing):
    with pytest.raises(ValueError):
        candidate.build(**{missing: None})
    assert not candidate.output.exists()

