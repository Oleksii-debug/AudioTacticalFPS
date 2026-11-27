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
    for name in ('RUNNING.md', 'START_UK.txt', 'START_V3_UK.txt', *refresh.BUNDLE_GUIDES):
        (project / 'docs' / name).write_text('Synthetic current guidance: ' + name + '\n')
    (project / 'docs' / 'RUNNING.md').write_bytes(
        b'[Search](ACADEMIC_SEARCH.md)\n[Insights](ACADEMIC_CATALOG_INSIGHTS.md)\n'
        b'[NVDA](ACADEMIC_SEARCH_NVDA.md)\n[Source](../src/example.py)\n'
        b'[Existing](docs/ACADEMIC_SEARCH.md)\n'
        b'[External](https://example.invalid/ACADEMIC_SEARCH.md)\n')
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
    assert not (bundle.source / 'docs').exists()
    source_readme = (bundle.packaging.parent / 'docs' / 'RUNNING.md').read_bytes()
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
    assert {path.name for path in (bundle.output / 'docs').iterdir()} == set(bundle.module.BUNDLE_GUIDES)
    readme = (bundle.output / 'README.txt').read_bytes()
    assert readme == (
        b'[Search](docs/ACADEMIC_SEARCH.md)\n[Insights](docs/ACADEMIC_CATALOG_INSIGHTS.md)\n'
        b'[NVDA](docs/ACADEMIC_SEARCH_NVDA.md)\n[Source](../src/example.py)\n'
        b'[Existing](docs/ACADEMIC_SEARCH.md)\n'
        b'[External](https://example.invalid/ACADEMIC_SEARCH.md)\n')
    assert manifest['files']['README.txt'] == sha256(readme).hexdigest()
    assert (bundle.packaging.parent / 'docs' / 'RUNNING.md').read_bytes() == source_readme
    for name in bundle.module.BUNDLE_GUIDES:
        raw = (bundle.packaging.parent / 'docs' / name).read_bytes()
        assert (bundle.output / 'docs' / name).read_bytes() == raw
        assert manifest['files']['docs/' + name] == sha256(raw).hexdigest()
    for name in ('START_UK.txt', 'START_V3_UK.txt'):
        raw = (bundle.packaging.parent / 'docs' / name).read_bytes()
        assert (bundle.output / name).read_bytes() == raw
        assert manifest['files'][name] == sha256(raw).hexdigest()
    assert not (bundle.output / 'unlisted-private.txt').exists()
    assert (bundle.source / 'bundle-manifest.json').read_bytes() == before
    assert not list(bundle.output.parent.glob('.private-bundle-*'))


@pytest.mark.parametrize('name', ['START_UK.txt', 'TRANSCRIPTION.md'])
def test_refresh_missing_current_guide_never_publishes_incomplete_bundle(bundle, name):
    (bundle.packaging.parent / 'docs' / name).unlink()
    with pytest.raises(FileNotFoundError):
        _refresh(bundle)
    assert not bundle.output.exists()
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
