"""Independent refresh integrity checks with real local Git and wheel records.

All runtimes/models are inert synthetic bytes; no supplied application or
native binary may execute. Source verification uses the real Git/wheel verifier.
"""
import base64
import csv
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
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
    for name in ('RUNNING.md', 'START_UK.txt', 'START_V3_UK.txt'):
        (project / 'docs' / name).write_text('Synthetic current installation guidance.\n')
    for name in ('install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd'):
        (packaging / name).write_text('raise AssertionError("PACKAGING MUST NOT EXECUTE PAYLOAD")\n')
    from build_windows_bundle import AUDIO_SETUP_FILES
    (packaging / 'SetupLocalAudio.cmd').write_bytes(b'@echo Synthetic source-only setup fixture\r\n')
    for relative in AUDIO_SETUP_FILES:
        target = packaging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(('Synthetic current setup file: ' + relative + '\n').encode('utf-8'))
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


@pytest.mark.parametrize('parent_tree', [None, 'b' * 40])
def test_refreshed_source_tree_comes_from_exact_verified_commit(bundle, parent_tree):
    previous = bundle.source / 'bundle-manifest.json'
    parent = json.loads(previous.read_bytes())
    if parent_tree is not None:
        parent['source_tree'] = parent_tree
    raw = (json.dumps(parent, sort_keys=True) + '\n').encode()
    previous.write_bytes(raw)
    bundle.parent_hash = sha256(raw).hexdigest()

    _refresh(bundle)

    current = json.loads((bundle.output / 'bundle-manifest.json').read_bytes())
    identity = json.loads((bundle.output / 'application-source.json').read_bytes())
    expected = _git(bundle.repo, 'rev-parse', bundle.commit + '^{tree}')
    assert current['source_tree'] == expected
    assert identity['source_tree'] == expected
    assert current['parent_source_commit'] == parent['source_commit']
    assert current['parent_manifest_sha256'] == bundle.parent_hash
    assert previous.read_bytes() == raw


def test_actual_refresh_manifest_is_accepted_by_installation_diagnostic(bundle):
    _refresh(bundle)
    from carr_library import native_checks as diagnostic
    expected = json.loads((bundle.output / 'bundle-manifest.json').read_bytes())
    assert diagnostic._manifest(bundle.output) == expected


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


def _prepared_canonical_audio(tmp_path, monkeypatch):
    # Load a plain fixture helper by exact file path; installed/importlib pytest
    # never depends on another test module being importable by its short name.
    helper = _load('refresh_canonical_audio_fixture',
                   Path(__file__).with_name('test_audio_bundle_convergence.py'))
    return helper._canonical_audio(tmp_path, monkeypatch)


def _reseal_previous(bundle, **fields):
    manifest_path = bundle.source / 'bundle-manifest.json'
    previous = json.loads(manifest_path.read_bytes())
    previous.update(fields)
    previous['files'] = {p.relative_to(bundle.source).as_posix(): sha256(p.read_bytes()).hexdigest()
                         for p in bundle.source.rglob('*') if p.is_file() and p != manifest_path}
    raw = (json.dumps(previous, sort_keys=True) + '\n').encode('utf-8')
    manifest_path.write_bytes(raw)
    bundle.parent_hash = sha256(raw).hexdigest()
    return raw


def test_refresh_replaces_current_setup_sources_and_does_not_inherit_old_audio(bundle, monkeypatch, tmp_path):
    from build_windows_bundle import AUDIO_SETUP_FILES
    prepared = _prepared_canonical_audio(tmp_path / 'previous-audio-fixture', monkeypatch)
    shutil.copytree(prepared.runtime, bundle.source / 'audio-runtime')
    old = bundle.source / 'tools/audio-setup'
    old.mkdir(parents=True)
    (old / 'setup_local_audio.py').write_bytes(b'Obsolete setup bytes must not survive.\n')
    (old / 'obsolete.py').write_bytes(b'Obsolete unneeded helper.\n')
    (bundle.source / 'SetupLocalAudio.cmd').write_bytes(b'Obsolete setup entry point.\n')
    before = _reseal_previous(bundle, bundled_audio_runtime=True)
    before_audio = {p.relative_to(bundle.source).as_posix(): p.read_bytes()
                    for p in (bundle.source / 'audio-runtime').rglob('*') if p.is_file()}

    _refresh(bundle)

    current = json.loads((bundle.output / 'bundle-manifest.json').read_bytes())
    assert current['bundled_audio_runtime'] is False
    assert current['bundled_audio'] is False
    assert not (bundle.output / 'audio-runtime').exists()
    assert not (bundle.output / 'audio').exists()
    setup = bundle.output / 'tools/audio-setup'
    assert {p.relative_to(setup).as_posix() for p in setup.rglob('*') if p.is_file()} == set(AUDIO_SETUP_FILES)
    for relative in AUDIO_SETUP_FILES:
        assert (setup / relative).read_bytes() == (bundle.packaging / relative).read_bytes()
    assert (bundle.output / 'SetupLocalAudio.cmd').read_bytes() == (bundle.packaging / 'SetupLocalAudio.cmd').read_bytes()
    assert (bundle.output / 'START_UK.txt').read_bytes() == (bundle.packaging.parent / 'docs/START_UK.txt').read_bytes()
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == before
    assert {name: (bundle.source / name).read_bytes() for name in before_audio} == before_audio
    assert (old / 'obsolete.py').read_bytes() == b'Obsolete unneeded helper.\n'


def test_refresh_explicit_canonical_audio_preserves_integrity_without_granting_transfer_rights(bundle, monkeypatch, tmp_path):
    from carr_library import audio_runtime
    from create_delivery import _require_transferable_audio_inventory
    prepared = _prepared_canonical_audio(tmp_path / 'selected-audio-fixture', monkeypatch)
    original = (bundle.source / 'bundle-manifest.json').read_bytes()

    _refresh(bundle, audio_runtime=prepared.runtime)

    manifest = json.loads((bundle.output / 'bundle-manifest.json').read_bytes())
    report = audio_runtime.inspect_bundled_audio(bundle.output)
    assert manifest['bundled_audio_runtime'] is True and manifest['bundled_audio'] is False
    assert report['layout'] == 'audio-runtime'
    assert report['manifest_sha256'] == prepared.report['manifest_sha256']
    assert report['source_lock_sha256'] == prepared.report['source_lock_sha256']
    assert report['native_execution'] == 'NOT_RUN'
    assert {Path(path).parent for path in report['tools'].values()} == {bundle.output / 'audio-runtime/bin'}
    with pytest.raises(ValueError, match='distribution clearance'):
        _require_transferable_audio_inventory(manifest)
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == original


def test_refresh_rejects_changed_audio_after_actual_verification_before_any_publication(bundle, monkeypatch, tmp_path):
    from carr_library import audio_runtime
    prepared = _prepared_canonical_audio(tmp_path / 'changed-audio-fixture', monkeypatch)
    inspect = audio_runtime._inspect_audio_runtime
    changed = []
    source_before = (bundle.source / 'bundle-manifest.json').read_bytes()

    def verified_then_changed(path):
        result = inspect(path)
        if Path(path) == prepared.runtime:
            target = prepared.runtime / 'bin/ffprobe.exe'
            target.write_bytes(target.read_bytes() + b'Changed after reviewed inventory.')
            changed.append(True)
        return result

    monkeypatch.setattr(audio_runtime, '_inspect_audio_runtime', verified_then_changed)
    with pytest.raises(ValueError, match='audio runtime changed'):
        _refresh(bundle, audio_runtime=prepared.runtime)
    assert changed == [True]
    assert not bundle.output.exists()
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == source_before


def test_refresh_rejects_two_selected_audio_layouts_before_reading_inputs(bundle, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('Mutually exclusive runtime selection must fail before reading the source manifest.')

    monkeypatch.setattr(bundle.module, '_read_manifest', forbidden)
    with pytest.raises(ValueError, match='either the canonical'):
        _refresh(bundle, audio=bundle.source / 'audio', audio_runtime=bundle.source / 'audio-runtime')
    assert not bundle.output.exists()


def test_refresh_acceptance_manifest_is_published_only_after_all_payload(bundle, monkeypatch):
    original_iterdir, original_move = Path.iterdir, shutil.move
    original_manifest = (bundle.source / 'bundle-manifest.json').read_bytes()

    def manifest_first(path):
        items = list(original_iterdir(path))
        if path.name.startswith('.private-bundle-'):
            items.sort(key=lambda item: item.name != 'bundle-manifest.json')
        return iter(items)

    def fail_payload(source, destination, *args, **kwargs):
        if Path(destination).parent == bundle.output and Path(source).name != 'bundle-manifest.json':
            raise OSError('Synthetic interrupted refresh publication.')
        return original_move(source, destination, *args, **kwargs)

    monkeypatch.setattr(Path, 'iterdir', manifest_first)
    monkeypatch.setattr(shutil, 'move', fail_payload)
    with pytest.raises(OSError, match='interrupted refresh'):
        _refresh(bundle)
    assert bundle.output.is_dir()
    assert not (bundle.output / 'bundle-manifest.json').exists()
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == original_manifest
