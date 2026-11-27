"""Independent refresh integrity checks with real local Git and wheel records.

All runtimes/models are inert synthetic bytes; no supplied application or
native binary may execute. Source verification uses the real Git/wheel verifier.
"""
import base64
import csv
import errno
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME='Synthetic Review', GIT_AUTHOR_EMAIL='review@example.invalid',
        GIT_COMMITTER_NAME='Synthetic Review', GIT_COMMITTER_EMAIL='review@example.invalid')
    return subprocess.check_output(['git', '-C', str(repo), *args], env=environment).decode().strip()


def _wheel(path, payload, name, version):
    record = f'{name.replace("-", "_")}-{version}.dist-info/RECORD'
    rows = [[key, 'sha256=' + base64.urlsafe_b64encode(sha256(value).digest()).decode().rstrip('='), str(len(value))]
            for key, value in payload.items()]
    rows.append([record, '', ''])
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='\n').writerows(rows)
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for key, value in payload.items():
            archive.writestr(key, value)
        archive.writestr(record, stream.getvalue().encode())
    return path


def _dependency(path, name='fixture-dependency', version='1.0'):
    prefix = f'{name.replace("-", "_")}-{version}.dist-info/'
    return _wheel(path, {prefix + 'METADATA': f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n'.encode(),
                         prefix + 'WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}, name, version)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'packaging'))
    refresh = _load('review_refresh_private_bundle', ROOT / 'packaging/refresh_private_bundle.py')
    verifier = _load('review_refresh_source_wheel', ROOT / 'packaging/source_wheel.py')
    repo = tmp_path / 'trusted-source'
    project = repo / 'carr-library'
    packaging = project / 'packaging'
    packaging.mkdir(parents=True)
    (packaging / 'wheel-notices').mkdir()
    (packaging / 'wheel-notices' / 'SOURCES.json').write_text('{"synthetic-current-notices":true}\n')
    (project / 'docs').mkdir()
    for name in ('RUNNING.md', 'START_V3_UK.txt'):
        (project / 'docs' / name).write_text('Synthetic current installation guidance.\n')
    for name in ('install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd'):
        (packaging / name).write_text('raise AssertionError("PACKAGING MUST NOT EXECUTE PAYLOAD")\n')
    package = project / 'src/carr_library'
    package.mkdir(parents=True)
    for name in ('__init__.py', '__main__.py'):
        (package / name).write_text('raise AssertionError("PACKAGING MUST NOT IMPORT APPLICATION CODE")\n')
    (project / 'LICENSE').write_text('Synthetic private application notice.\n')
    (project / 'pyproject.toml').write_text('''[project]
name = "universal-research-library"
version = "1.0"
license-files = ["LICENSE"]
[tool.setuptools]
packages = ["carr_library"]
package-dir = {"" = "src"}
''')
    _git(repo, 'init', '-q')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'Synthetic reviewed source')
    commit = _git(repo, 'rev-parse', 'HEAD')
    prefix = 'universal_research_library-1.0.dist-info/'
    payload = {f'carr_library/{path.name}': path.read_bytes() for path in package.iterdir()}
    payload.update({prefix + 'METADATA': b'Metadata-Version: 2.4\nName: universal-research-library\nVersion: 1.0\n',
                    prefix + 'WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                    prefix + 'licenses/LICENSE': (project / 'LICENSE').read_bytes()})
    wheel = _wheel(tmp_path / 'universal_research_library-1.0-py3-none-any.whl', payload, 'universal-research-library', '1.0')
    previous = tmp_path / 'previous-private-bundle'
    (previous / 'wheelhouse').mkdir(parents=True)
    dependency = _dependency(previous / 'wheelhouse/fixture_dependency-1.0-py3-none-any.whl')
    (previous / 'wheelhouse' / wheel.name).write_bytes(wheel.read_bytes())
    lock = packaging / 'windows-cp311.lock'
    lock.write_text('fixture-dependency==1.0 --hash=sha256:' + sha256(dependency.read_bytes()).hexdigest() + '\n')
    sbom = {'bomFormat': 'CycloneDX', 'specVersion': '1.5', 'components': [{'name': 'fixture-dependency', 'version': '1.0'}]}
    (packaging / 'windows-sbom.cdx.json').write_text(json.dumps(sbom))
    protected = {'notices/python/LICENSE.synthetic.txt': b'Exact prior private dependency notice.\n',
                 'models/MODEL_LICENSE.txt': b'Exact retained private model notice.\n'}
    for name, raw in {
        'install.lock': b'synthetic prior lock\n', 'install_windows.py': b'raise AssertionError("OLD PAYLOAD MUST NOT EXECUTE")\n',
        'Install.cmd': b'old launcher', 'ResearchLibrary.cmd': b'old launcher',
        'CheckInstallation.cmd': b'old launcher', 'README.txt': b'old guidance',
        'ocr/tesseract.exe': b'INERT SYNTHETIC NATIVE PAYLOAD', 'ocr/tessdata/eng.traineddata': b'synthetic model',
        'ocr/ocr-manifest.json': b'{"synthetic":true}\n', **protected}.items():
        path = previous / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    source_tar = io.BytesIO()
    with tarfile.open(fileobj=source_tar, mode='w') as archive:
        raw = b'Synthetic exact corresponding source.\n'
        info = tarfile.TarInfo('source/README.txt')
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    (previous / 'ocr-corresponding-source.tar').write_bytes(source_tar.getvalue())
    protected['ocr-corresponding-source.tar'] = source_tar.getvalue()
    manifest = {'schema': 1, 'source_commit': 'a' * 40, 'target': 'Windows x64 / CPython 3.11',
        'bundled_python': False, 'bundled_model': False, 'bundled_ocr': True, 'bundled_native_runtime': False,
        'files': {path.relative_to(previous).as_posix(): sha256(path.read_bytes()).hexdigest()
                  for path in previous.rglob('*') if path.is_file()}}
    raw = (json.dumps(manifest, sort_keys=True) + '\n').encode()
    (previous / 'bundle-manifest.json').write_bytes(raw)
    monkeypatch.setattr(refresh, 'HERE', packaging)
    monkeypatch.setattr(refresh, 'verify_application_source',
                        lambda candidate, expected: verifier.verify_application_wheel(candidate, repo, expected))
    return SimpleNamespace(module=refresh, source=previous, wheel=wheel, commit=commit, repo=repo, packaging=packaging,
        payload=payload, prefix=prefix, parent_hash=sha256(raw).hexdigest(), protected=protected,
        output=tmp_path / 'refreshed', lock=lock, sbom=sbom)


def _refresh(bundle, **kwargs):
    return bundle.module.refresh(bundle.source, bundle.wheel, bundle.output, bundle.commit,
                                 parent_manifest_sha256=bundle.parent_hash, **kwargs)


def test_refresh_exact_source_parent_hash_notices_and_sources_without_execution(bundle, monkeypatch):
    # CPython discovers Windows version with the fixed OS `ver` command.
    # Bootstrap the cached host marker snapshot before guarding payload use;
    # the guard still permits only Git during the reviewed packaging operation.
    from packaging.markers import default_environment
    default_environment()
    run = subprocess.run
    def git_only(arguments, *args, **kwargs):
        assert arguments[0] == 'git', 'A runtime or installer was executed during packaging.'
        return run(arguments, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', git_only)
    (bundle.source / 'unlisted-private.txt').write_text('This private file must not be copied.')
    before = (bundle.source / 'bundle-manifest.json').read_bytes()
    report = _refresh(bundle)
    manifest = json.loads((bundle.output / 'bundle-manifest.json').read_bytes())
    identity = json.loads((bundle.output / 'application-source.json').read_bytes())
    assert identity['source_commit'] == bundle.commit
    assert identity['wheel_sha256'] == sha256(bundle.wheel.read_bytes()).hexdigest()
    assert manifest['parent_manifest_sha256'] == bundle.parent_hash
    assert manifest['parent_source_commit'] == 'a' * 40
    assert manifest['use_scope'] == 'private-personal-installation'
    assert report['manifest_sha256'] == sha256((bundle.output / 'bundle-manifest.json').read_bytes()).hexdigest()
    for name, raw in bundle.protected.items():
        assert (bundle.output / name).read_bytes() == raw
    assert not (bundle.output / 'unlisted-private.txt').exists()
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == before
    assert not list(bundle.output.parent.glob('.private-bundle-*'))


def test_wrong_recorded_parent_hash_fails_before_publication(bundle):
    bundle.parent_hash = '0' * 64
    with pytest.raises(ValueError, match='recorded SHA256'):
        _refresh(bundle)
    assert not bundle.output.exists()


def test_manifest_is_never_reopened_after_verification_for_copy_paths(bundle, monkeypatch, tmp_path):
    original_verify = bundle.module.verify
    sentinel = tmp_path / 'outside-sentinel'
    sentinel.write_text('Keep these private bytes.')
    def mutate_after_check(root, **kwargs):
        result = original_verify(root, **kwargs)
        if root == bundle.source:
            assert kwargs['manifest_bytes'] == (bundle.source / 'bundle-manifest.json').read_bytes()
            changed = json.loads(kwargs['manifest_bytes'])
            changed['files']['../outside-sentinel'] = sha256(sentinel.read_bytes()).hexdigest()
            (bundle.source / 'bundle-manifest.json').write_text(json.dumps(changed))
        return result
    monkeypatch.setattr(bundle.module, 'verify', mutate_after_check)
    _refresh(bundle)
    assert sentinel.read_text() == 'Keep these private bytes.'
    assert '../outside-sentinel' not in json.loads((bundle.output / 'bundle-manifest.json').read_bytes())['files']


def test_source_verified_wheel_cannot_change_between_verification_and_copy(bundle, monkeypatch):
    verify = bundle.module.verify_application_source
    def replace_after_verification(path, commit):
        result = verify(path, commit)
        with path.open('ab') as stream:
            stream.write(b'Changed after source verification')
        return result
    monkeypatch.setattr(bundle.module, 'verify_application_source', replace_after_verification)
    with pytest.raises(ValueError, match='wheel changed'):
        _refresh(bundle)
    assert not bundle.output.exists()


@pytest.mark.parametrize('requirement', ['not-in-lock==9.0', 'fixture-dependency>=9',
    'remote-payload @ https://example.invalid/payload.whl#sha256=' + '0' * 64])
def test_offline_dependency_closure_rejects_missing_incompatible_or_direct_url(bundle, requirement):
    payload = dict(bundle.payload)
    payload[bundle.prefix + 'METADATA'] += ('Requires-Dist: ' + requirement + '\n').encode()
    _wheel(bundle.wheel, payload, 'universal-research-library', '1.0')
    with pytest.raises(ValueError):
        _refresh(bundle)
    assert not bundle.output.exists()


def test_locked_extra_wheel_union_updates_install_inventory_without_execution(bundle, tmp_path):
    extra = tmp_path / 'extra-wheelhouse'
    extra.mkdir()
    wheel = _dependency(extra / 'fixture_extra-2.0-py3-none-any.whl', 'fixture-extra', '2.0')
    with bundle.lock.open('a') as stream:
        stream.write('fixture-extra==2.0 --hash=sha256:' + sha256(wheel.read_bytes()).hexdigest() + '\n')
    bundle.sbom['components'].append({'name': 'fixture-extra', 'version': '2.0'})
    (bundle.packaging / 'windows-sbom.cdx.json').write_text(json.dumps(bundle.sbom))
    payload = dict(bundle.payload)
    payload[bundle.prefix + 'METADATA'] += b'Requires-Dist: fixture-extra>=2\n'
    _wheel(bundle.wheel, payload, 'universal-research-library', '1.0')
    _refresh(bundle, extra_wheelhouse=extra)
    assert (bundle.output / 'wheelhouse' / wheel.name).read_bytes() == wheel.read_bytes()
    assert 'fixture-extra==2.0' in (bundle.output / 'install.lock').read_text()
    assert json.loads((bundle.output / 'runtime-sbom.cdx.json').read_bytes()) == bundle.sbom


@pytest.mark.parametrize('kind', ['directory', 'file', 'symlink'])
def test_refresh_never_overwrites_existing_or_linked_output(bundle, kind, tmp_path):
    if kind == 'directory':
        bundle.output.mkdir()
        (bundle.output / 'keep').write_text('Private existing content')
    elif kind == 'file':
        bundle.output.write_text('Private existing file')
    else:
        other = tmp_path / 'other'
        other.mkdir()
        bundle.output.symlink_to(other, target_is_directory=True)
    with pytest.raises((ValueError, FileExistsError)):
        _refresh(bundle)
    if kind == 'directory':
        assert (bundle.output / 'keep').read_text() == 'Private existing content'
    elif kind == 'file':
        assert bundle.output.read_text() == 'Private existing file'
    else:
        assert bundle.output.is_symlink() and list(other.iterdir()) == []


def _archive_refresh(bundle, **kwargs):
    return bundle.module.refresh_archive(bundle.source, bundle.wheel,
        bundle.output.with_suffix('.zip'), bundle.commit,
        parent_manifest_sha256=bundle.parent_hash, **kwargs)


def _source_companion(bundle, path):
    from create_delivery import _write_archive, SOURCES
    raw = (bundle.source / 'bundle-manifest.json').read_bytes()
    manifest = json.loads(raw)
    _write_archive(path, bundle.source, {SOURCES}, manifest['files'], raw, lambda: False)
    return path


def test_direct_archive_matches_directory_bytes_without_unpacked_copy_or_execution(bundle, monkeypatch, tmp_path):
    _refresh(bundle)
    from packaging.markers import default_environment
    default_environment()
    run = subprocess.run
    def git_only(arguments, *args, **kwargs):
        assert arguments[0] == 'git', 'A supplied runtime or installer was executed.'
        return run(arguments, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', git_only)
    def no_copy(*_args, **_kwargs):
        raise AssertionError('Direct ZIP refresh must not create another unpacked bundle.')
    monkeypatch.setattr(bundle.module, 'TemporaryDirectory', no_copy)
    monkeypatch.setattr(bundle.module.shutil, 'copyfile', no_copy)
    report = _archive_refresh(bundle)
    output = bundle.output.with_suffix('.zip')
    prefix = bundle.module.PREFIX
    with ZipFile(output) as archive:
        expected = {prefix + path.relative_to(bundle.output).as_posix(): path.read_bytes()
                    for path in bundle.output.rglob('*') if path.is_file()}
        assert set(archive.namelist()) == set(expected)
        assert all(archive.read(name) == value for name, value in expected.items())
        archive.extractall(tmp_path / 'extracted-archive')
    bundle.module.verify(tmp_path / 'extracted-archive' / prefix.rstrip('/'))
    assert report['archives']['application']['sha256'] == sha256(output.read_bytes()).hexdigest()
    assert report['manifest_sha256'] == sha256((bundle.output / 'bundle-manifest.json').read_bytes()).hexdigest()
    assert report['both_archives_required'] is False
    assert report['native_execution'] == report['model_inference'] == 'NOT_RUN'
    assert not list(output.parent.glob('.private-archive-*'))
    for name, value in bundle.protected.items():
        assert (bundle.source / name).read_bytes() == value


def test_split_archive_reuses_exact_companion_and_manifest_requires_the_union(bundle, tmp_path):
    from create_delivery import verify_archives, SOURCES, MANIFEST, PREFIX
    companion = _source_companion(bundle, tmp_path / 'existing-sources.zip')
    before = (companion.stat().st_ino, companion.stat().st_size, companion.stat().st_mtime_ns, companion.read_bytes())
    report = _archive_refresh(bundle, split_ocr_sources=True, source_companion=companion)
    output = bundle.output.with_suffix('.zip')
    with ZipFile(output) as archive:
        raw = archive.read(PREFIX + MANIFEST)
        manifest = json.loads(raw)
        assert SOURCES in manifest['files']
        assert PREFIX + SOURCES not in archive.namelist()
        assert set(archive.namelist()) == {PREFIX + name for name in manifest['files'] if name != SOURCES} | {PREFIX + MANIFEST}
        archive.extractall(tmp_path / 'union')
    extracted = tmp_path / 'union' / PREFIX.rstrip('/')
    with pytest.raises((ValueError, FileNotFoundError)):
        bundle.module.verify(extracted)
    with ZipFile(companion) as archive:
        archive.extractall(tmp_path / 'union')
    bundle.module.verify(extracted)
    verify_archives(output, companion, raw, bundle.commit)
    assert before == (companion.stat().st_ino, companion.stat().st_size, companion.stat().st_mtime_ns, companion.read_bytes())
    assert report['both_archives_required'] is True
    assert report['archives']['sources']['reused'] is True
    assert report['archives']['sources']['path'] == str(companion)


@pytest.mark.parametrize('damage', ['wrong-payload', 'extra-entry', 'trailing-bytes'])
def test_direct_archive_rejects_inexact_source_companion(bundle, tmp_path, damage):
    from create_delivery import PREFIX, SOURCES
    companion = _source_companion(bundle, tmp_path / 'sources.zip')
    if damage == 'trailing-bytes':
        with companion.open('ab') as stream:
            stream.write(b'unlisted envelope bytes')
    else:
        with ZipFile(companion, 'w', ZIP_DEFLATED) as archive:
            archive.writestr(PREFIX + SOURCES,
                b'wrong source archive' if damage == 'wrong-payload' else bundle.protected[SOURCES])
            if damage == 'extra-entry':
                archive.writestr(PREFIX + 'unrequested-private.txt', b'not part of the source companion')
    with pytest.raises(ValueError):
        _archive_refresh(bundle, split_ocr_sources=True, source_companion=companion)
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


def test_direct_archive_requires_explicit_companion_in_split_mode(bundle):
    with pytest.raises(ValueError, match='companion'):
        _archive_refresh(bundle, split_ocr_sources=True)
    assert not bundle.output.with_suffix('.zip').exists()


@pytest.mark.parametrize('damage', ['parent-hash', 'locked-dependency', 'source-wheel', 'sbom'])
def test_direct_archive_keeps_original_input_validation_gates(bundle, damage):
    if damage == 'parent-hash':
        bundle.parent_hash = '0' * 64
    elif damage == 'locked-dependency':
        (bundle.source / 'wheelhouse/fixture_dependency-1.0-py3-none-any.whl').write_bytes(b'changed locked wheel')
    elif damage == 'source-wheel':
        payload = dict(bundle.payload)
        payload['carr_library/__init__.py'] += b'\nUNCOMMITTED = True\n'
        _wheel(bundle.wheel, payload, 'universal-research-library', '1.0')
    else:
        (bundle.packaging / 'windows-sbom.cdx.json').write_text('{"bomFormat":"CycloneDX","specVersion":"1.5","components":[]}')
    with pytest.raises(ValueError):
        _archive_refresh(bundle)
    assert not bundle.output.with_suffix('.zip').exists()


def test_direct_archive_detects_input_change_during_stream_and_removes_partial_zip(bundle):
    changed = []
    source = bundle.source / 'notices/python/LICENSE.synthetic.txt'
    def change_after_planning():
        if not changed and next(bundle.output.parent.glob('.private-archive-*.zip'), None) is not None:
            raw = source.read_bytes()
            source.write_bytes(b'X' * len(raw))
            changed.append(True)
        return False
    with pytest.raises(ValueError, match='changed'):
        _archive_refresh(bundle, cancelled=change_after_planning)
    assert changed
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


def test_direct_archive_cancellation_keeps_inputs_and_publishes_nothing(bundle):
    before = (bundle.source / 'bundle-manifest.json').read_bytes()
    def cancel_after_open():
        return next(bundle.output.parent.glob('.private-archive-*.zip'), None) is not None
    with pytest.raises(InterruptedError):
        _archive_refresh(bundle, cancelled=cancel_after_open)
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == before
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


def test_direct_archive_publication_race_never_replaces_foreign_output(bundle, monkeypatch):
    publish = bundle.module._publish
    output = bundle.output.with_suffix('.zip')
    def competing_output(stage, destination, parent_fd):
        destination.write_bytes(b'Existing private output wins.')
        return publish(stage, destination, parent_fd)
    monkeypatch.setattr(bundle.module, '_publish', competing_output)
    with pytest.raises(FileExistsError):
        _archive_refresh(bundle)
    assert output.read_bytes() == b'Existing private output wins.'
    assert not list(bundle.output.parent.glob('.private-archive-*'))


def test_direct_archive_rejects_changed_bytes_after_output_verification(bundle, monkeypatch):
    verify = bundle.module._verify_archive
    def mutate_verified(path, expected, cancelled):
        report = verify(path, expected, cancelled)
        if path.name.startswith('.private-archive-'):
            with path.open('ab') as stream:
                stream.write(b'changed after verification')
        return report
    monkeypatch.setattr(bundle.module, '_verify_archive', mutate_verified)
    with pytest.raises(ValueError, match='changed after verification'):
        _archive_refresh(bundle)
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


def _native_fixtures(bundle, tmp_path):
    # Valid PE32+ envelope, inert bytes; never native execution evidence.
    raw = bytearray(90)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 0x3C, 64)
    raw[64:68] = b'PE\0\0'
    struct.pack_into('<H', raw, 68, 0x8664)
    struct.pack_into('<H', raw, 88, 0x20B)
    audio, citations = tmp_path / 'audio-runtime', tmp_path / 'node-runtime'
    audio.mkdir()
    citations.mkdir()
    output_files = {}
    for name, value in {'bin/ffmpeg.exe': bytes(raw), 'bin/ffprobe.exe': bytes(raw),
                        'LICENSE': b'Fixture license', 'README.txt': b'Fixture runtime notice'}.items():
        path = audio / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        output_files[name] = {'size': len(value), 'sha256': sha256(value).hexdigest(), 'member': 'fixture/' + name}
    lock = {'schema': 1, 'target': 'win_amd64', 'engine': 'FFmpeg',
        'artifact': {'filename': 'fixture.zip', 'format': 'zip', 'size': 1, 'sha256': '0' * 64,
                     'url': 'https://github.com/GyanD/codexffmpeg/releases/download/fixture/fixture.zip'},
        'output_files': output_files,
        'deployment': {'download_default': False, 'public_binary_distribution': False}}
    (bundle.packaging / 'windows-audio.lock.json').write_text(json.dumps(lock))
    (citations / 'node.exe').write_bytes(raw)
    (citations / 'LICENSE').write_bytes(b'Fixture Node license')
    node_lock = {'files': {path.name: sha256(path.read_bytes()).hexdigest() for path in citations.iterdir()}}
    (bundle.packaging / 'windows-citations.lock.json').write_text(json.dumps(node_lock))
    return audio, citations


def test_local_refresh_keeps_pinned_audio_but_transfer_archive_rejects_it(bundle, monkeypatch, tmp_path):
    audio, citations = _native_fixtures(bundle, tmp_path)
    _refresh(bundle, audio=audio, citations=citations)
    manifest = json.loads((bundle.output / 'bundle-manifest.json').read_bytes())
    assert manifest['bundled_audio'] is True and manifest['bundled_citation_runtime'] is True
    for name, path in [('audio/bin/ffmpeg.exe', audio / 'bin/ffmpeg.exe'),
                       ('citation-runtime/node.exe', citations / 'node.exe')]:
        assert (bundle.output / name).read_bytes() == path.read_bytes()
        assert manifest['files'][name] == sha256(path.read_bytes()).hexdigest()
    original = (bundle.source / 'bundle-manifest.json').read_bytes()

    def forbidden(*_args, **_kwargs):
        pytest.fail('Uncleared audio must be refused before transfer staging or payload copying.')

    monkeypatch.setattr(bundle.module._Payload, 'write', forbidden)
    monkeypatch.setattr(bundle.module.tempfile, 'mkstemp', forbidden)
    with pytest.raises(ValueError, match='Audio runtime distribution clearance'):
        _archive_refresh(bundle, audio=audio, citations=citations)
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == original
    assert (bundle.output / 'audio/bin/ffmpeg.exe').read_bytes() == (audio / 'bin/ffmpeg.exe').read_bytes()
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


def test_direct_archive_keeps_pinned_node_without_audio(bundle, tmp_path):
    _, citations = _native_fixtures(bundle, tmp_path)
    report = _archive_refresh(bundle, citations=citations)
    with ZipFile(bundle.output.with_suffix('.zip')) as archive:
        raw = archive.read(bundle.module.PREFIX + 'bundle-manifest.json')
        manifest = json.loads(raw)
        assert manifest['bundled_audio'] is False and manifest['bundled_citation_runtime'] is True
        assert not any(name.startswith(('audio/', 'audio-runtime/')) for name in manifest['files'])
        name = 'citation-runtime/node.exe'
        assert archive.read(bundle.module.PREFIX + name) == (citations / 'node.exe').read_bytes()
        assert manifest['files'][name] == sha256((citations / 'node.exe').read_bytes()).hexdigest()
    assert report['native_execution'] == 'NOT_RUN'


@pytest.mark.parametrize('flags,name', [
    ({}, 'audio-runtime/bin/ffmpeg.exe'),
    ({'bundled_audio': False, 'bundled_audio_runtime': False}, 'audio-runtime/bin/ffplay.exe'),
    ({'bundled_audio': False, 'bundled_audio_runtime': False,
      'public_distribution_clearance': True}, 'AUDIO-RUNTIME/provenance/source-lock.json'),
    ({'bundled_audio': False, 'bundled_audio_runtime': False}, 'AuDiO/bin/ffprobe.exe'),
])
def test_direct_archive_rejects_retained_audio_inventory_despite_absence_flags(
        bundle, monkeypatch, flags, name):
    payload = b'Synthetic retained local-only runtime payload.'
    selected = bundle.source / name
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_bytes(payload)
    path = bundle.source / 'bundle-manifest.json'
    manifest = json.loads(path.read_bytes())
    manifest.update(flags)
    manifest['files'][name] = sha256(payload).hexdigest()
    raw = (json.dumps(manifest, sort_keys=True) + '\n').encode('utf-8')
    path.write_bytes(raw)
    bundle.parent_hash = sha256(raw).hexdigest()

    def forbidden(*_args, **_kwargs):
        pytest.fail('Retained local-only audio must be rejected before transfer output is created.')

    monkeypatch.setattr(bundle.module._Payload, 'write', forbidden)
    monkeypatch.setattr(bundle.module.tempfile, 'mkstemp', forbidden)
    with pytest.raises(ValueError, match='Audio runtime distribution clearance'):
        _archive_refresh(bundle)
    assert path.read_bytes() == raw and selected.read_bytes() == payload
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


@pytest.mark.parametrize('component', ['audio', 'node'])
def test_direct_archive_rejects_changed_pinned_native_payload(bundle, tmp_path, component):
    audio, citations = _native_fixtures(bundle, tmp_path)
    selected = audio / 'bin/ffmpeg.exe' if component == 'audio' else citations / 'node.exe'
    selected.write_bytes(b'Changed native payload, never execute.')
    with pytest.raises(ValueError, match='runtime differs'):
        _archive_refresh(bundle, audio=audio, citations=citations)
    assert not bundle.output.with_suffix('.zip').exists()


@pytest.mark.skipif(os.name == 'nt', reason='Windows prevents renaming this open temporary descriptor.')
def test_direct_archive_stage_replacement_never_deletes_foreign_file_or_leaks_descriptor(bundle, monkeypatch, tmp_path):
    create = bundle.module.tempfile.mkstemp
    records = {}
    def replace_stage(*args, **kwargs):
        descriptor, name = create(*args, **kwargs)
        path = Path(name)
        moved = tmp_path / 'moved-own-stage.zip'
        path.rename(moved)
        path.write_bytes(b'Foreign replacement must remain.')
        records.update(descriptor=descriptor, foreign=path, moved=moved)
        return descriptor, name
    monkeypatch.setattr(bundle.module.tempfile, 'mkstemp', replace_stage)
    with pytest.raises(ValueError, match='staging file changed'):
        _archive_refresh(bundle)
    assert records['foreign'].read_bytes() == b'Foreign replacement must remain.'
    assert records['moved'].read_bytes() == b''
    with pytest.raises(OSError) as closed:
        os.fstat(records['descriptor'])
    assert closed.value.errno == errno.EBADF
    assert not bundle.output.with_suffix('.zip').exists()


@pytest.mark.parametrize('failure', ['write', 'publication'])
def test_direct_archive_io_failure_closes_handles_and_cleans_only_own_stage(bundle, monkeypatch, failure):
    original = (bundle.source / 'bundle-manifest.json').read_bytes()
    if failure == 'write':
        def disk_full(self, destination, cancelled):
            destination.write(b'Partial output before the volume fills.')
            raise OSError(errno.ENOSPC, 'Synthetic full volume')
        monkeypatch.setattr(bundle.module._Payload, 'write', disk_full)
        expected = errno.ENOSPC
    else:
        def locked_publication(stage, output, parent_fd):
            raise PermissionError(errno.EACCES, 'Synthetic locked publication')
        monkeypatch.setattr(bundle.module, '_publish', locked_publication)
        expected = errno.EACCES
    with pytest.raises(OSError) as failed:
        _archive_refresh(bundle)
    assert failed.value.errno == expected
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == original
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))


@pytest.mark.parametrize('old_name', ['notices/wheels/sources.json', 'notices/wheels'])
def test_direct_archive_rejects_generated_portable_path_collisions_before_writing(bundle, old_name):
    # Each origin is valid on its own, including Windows. Their refreshed union
    # collides with the new notices/wheels/SOURCES.json path by case or prefix.
    payload = b'Prior inventory entry colliding with current notices.'
    old = bundle.source / old_name
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(payload)
    manifest_path = bundle.source / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['files'][old_name] = sha256(payload).hexdigest()
    raw = (json.dumps(manifest, sort_keys=True) + '\n').encode('utf-8')
    manifest_path.write_bytes(raw)
    bundle.parent_hash = sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        _archive_refresh(bundle)
    assert not bundle.output.with_suffix('.zip').exists()
    assert not list(bundle.output.parent.glob('.private-archive-*'))
