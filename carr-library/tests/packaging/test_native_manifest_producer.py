"""The installed manifest reader consumes the actual canonical producer output.

Real Git, source wheels, native PE inspection, bundle construction and private
refresh run with inert fixtures. No Windows code or extension is executed.
"""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'manifest_native_fixture_support', Path(__file__).with_name('test_native_launcher_bundle.py'))
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)
candidate = support.candidate
refresh_support = support.module(
    'manifest_refresh_fixture_support', Path(__file__).with_name('test_native_launcher_refresh.py'))
reader = support.module('canonical_native_manifest_reader', ROOT / 'src/carr_library/native_checks.py')
runtime = support.module('canonical_native_manifest_runtime', ROOT / 'src/carr_library/native_runtime.py')


@pytest.mark.parametrize('native', [False, True])
def test_actual_base_builder_manifest_is_readable_without_native_execution(candidate, native):
    options = {} if native else dict(native_launcher=None, native_launcher_proof=None,
                                    native_launcher_sha256=None)
    support.build(candidate, **options)
    expected = json.loads((candidate.output / 'bundle-manifest.json').read_bytes())
    assert reader._manifest(candidate.output) == expected
    assert expected['source_commit'] == candidate.commit
    assert expected['source_tree'] == support.git(candidate.repo, 'rev-parse', 'HEAD^{tree}')
    if native:
        assert expected['native_launcher_source_commit'] == candidate.commit
        assert expected['native_launcher_sha256'] == candidate.pin
    # The supplementary DLL runtime is absent in this inert fixture. Its real
    # reader must still accept the enclosing producer's new metadata fields.
    assert runtime._bundle(candidate.output) is None


@pytest.fixture
def produced(candidate, monkeypatch):
    prepared = refresh_support.prepare(candidate, monkeypatch)
    audio, citations = refresh_support.canonical_runtimes(prepared)
    refresh_support.refresh(prepared, audio=audio, citations=citations)
    manifest_path = prepared.output / 'bundle-manifest.json'
    return SimpleNamespace(candidate=candidate, prepared=prepared, path=manifest_path,
                           manifest=json.loads(manifest_path.read_bytes()))


def test_actual_refresh_parent_audio_citation_and_native_identity_is_readable(produced):
    expected = produced.manifest
    assert reader._manifest(produced.path.parent) == expected
    assert expected['source_commit'] == produced.candidate.commit
    assert expected['source_tree'] == support.git(produced.candidate.repo, 'rev-parse', 'HEAD^{tree}')
    assert expected['parent_source_commit'] == json.loads(produced.prepared.parent)['source_commit']
    assert expected['parent_manifest_sha256'] == sha256(produced.prepared.parent).hexdigest()
    assert expected['use_scope'] == 'private-personal-installation'
    assert expected['bundled_audio'] is expected['bundled_citation_runtime'] is True
    assert expected['native_launcher_source_commit'] == expected['source_commit']
    assert expected['native_launcher_sha256'] == expected['files']['ResearchLibrary.exe']
    assert runtime._bundle(produced.path.parent) is None


def new_application_revision(candidate):
    package = candidate.native.parent.parent / 'src/carr_library'
    (package / '__main__.py').write_bytes(b'# New synthetic application revision.\nraise SystemExit(0)\n')
    support.git(candidate.repo, 'add', '.')
    support.git(candidate.repo, 'commit', '-qm', 'New application retaining identical native source inputs')
    candidate.commit = support.git(candidate.repo, 'rev-parse', 'HEAD')
    support.write_wheel(candidate.app, 'universal_research_library',
                        {'carr_library/' + p.name: p.read_bytes() for p in package.iterdir()},
                        requires=('fixture==1.0',))


def test_actual_builder_rebinds_retained_launcher_to_a_new_application_commit(candidate):
    old_commit = candidate.commit
    executable, proof = candidate.exe.read_bytes(), candidate.proof.read_bytes()
    new_application_revision(candidate)
    assert candidate.commit != old_commit
    support.build(candidate)
    manifest = reader._manifest(candidate.output)
    assert manifest['source_commit'] == manifest['native_launcher_source_commit'] == candidate.commit
    assert manifest['source_tree'] == support.git(candidate.repo, 'rev-parse', 'HEAD^{tree}')
    assert (candidate.output / 'ResearchLibrary.exe').read_bytes() == executable
    assert (candidate.output / 'native-launcher-build.json').read_bytes() == proof


def test_actual_refresh_rebinds_retained_launcher_and_keeps_distinct_parent_identity(candidate, monkeypatch):
    prepared = refresh_support.prepare(candidate, monkeypatch)
    old_commit = candidate.commit
    new_application_revision(candidate)
    assert candidate.commit != old_commit
    refresh_support.refresh(prepared)
    manifest = reader._manifest(prepared.output)
    assert manifest['source_commit'] == manifest['native_launcher_source_commit'] == candidate.commit
    assert manifest['source_tree'] == support.git(candidate.repo, 'rev-parse', 'HEAD^{tree}')
    assert manifest['source_tree'] != json.loads(prepared.parent)['source_tree']
    assert manifest['parent_source_commit'] == old_commit
    assert manifest['parent_manifest_sha256'] == sha256(prepared.parent).hexdigest()
    for name in ('ResearchLibrary.exe', 'native-launcher-build.json'):
        assert (prepared.output / name).read_bytes() == (prepared.source / name).read_bytes()
    assert (prepared.source / 'bundle-manifest.json').read_bytes() == prepared.parent


@pytest.mark.parametrize('field,value', [
    ('source_commit', 'HEAD'),
    ('source_commit', 'b' * 40),
    ('source_tree', 'HEAD'),
    ('source_tree', 'A' * 40),
    ('source_tree', True),
    ('bundled_native_runtime', 'false'),
    ('bundled_native_launcher', 1),
    ('bundled_audio', 'true'),
    ('bundled_citation_runtime', 1),
    ('use_scope', 'public-commercial-distribution'),
    ('parent_source_commit', 'HEAD'),
    ('parent_source_commit', 'A' * 40),
    ('parent_manifest_sha256', 'a' * 63),
    ('parent_manifest_sha256', 'A' * 64),
    ('native_launcher_source_commit', 'b' * 40),
    ('native_launcher_sha256', '0' * 64),
    ('native_launcher_sha256', 123),
    ('bundled_native_launcher', False),
    ('bundled_python', False),
    ('bundled_audio_runtime', True),
    ('disable_checks', True),
])
def test_changed_producer_metadata_is_rejected(produced, field, value):
    produced.manifest[field] = value
    produced.path.write_text(json.dumps(produced.manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        reader._manifest(produced.path.parent)


@pytest.mark.parametrize('field', [
    'source_commit', 'source_tree', 'use_scope', 'parent_source_commit', 'parent_manifest_sha256',
    'bundled_native_launcher', 'native_launcher_source_commit', 'native_launcher_sha256',
])
def test_missing_source_parent_or_native_identity_is_rejected(produced, field):
    del produced.manifest[field]
    produced.path.write_text(json.dumps(produced.manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        reader._manifest(produced.path.parent)


@pytest.mark.parametrize('entry', ['ResearchLibrary.exe', 'native-launcher-build.json'])
def test_native_inventory_pair_cannot_be_omitted(produced, entry):
    del produced.manifest['files'][entry]
    produced.path.write_text(json.dumps(produced.manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        reader._manifest(produced.path.parent)


@pytest.mark.parametrize('entry', ['ResearchLibrary.exe', 'native-launcher-build.json'])
def test_native_inventory_requires_the_exact_producer_spelling(produced, entry):
    produced.manifest['files'][entry.upper()] = produced.manifest['files'].pop(entry)
    produced.path.write_text(json.dumps(produced.manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        reader._manifest(produced.path.parent)


def test_source_tree_still_requires_a_commit_in_a_legacy_nonnative_manifest(candidate):
    support.build(candidate, native_launcher=None, native_launcher_proof=None,
                  native_launcher_sha256=None)
    path = candidate.output / 'bundle-manifest.json'
    manifest = json.loads(path.read_bytes())
    del manifest['source_commit']
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='source tree'):
        reader._manifest(candidate.output)
    del manifest['source_tree']
    path.write_text(json.dumps(manifest), encoding='utf-8')
    assert reader._manifest(candidate.output) == manifest
