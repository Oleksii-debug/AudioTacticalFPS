"""Canonical sealed bundles and explicitly owned profiles keep distinct admission."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest

from carr_library import native_checks

PACKAGING = Path(__file__).resolve().parents[2] / 'packaging'


def module(name):
    path = PACKAGING / (name + '.py')
    spec = importlib.util.spec_from_file_location('profile_' + name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / 'legacy sealed program'
    root.mkdir()
    files = {}
    for name in ('install.lock', 'install_windows.py', 'Install.cmd',
                 'ResearchLibrary.cmd', 'CheckInstallation.cmd', 'README.txt',
                 'ResearchLibrary.exe', 'wheelhouse/fixture-1.0-py3-none-any.whl'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ('Synthetic inventory fixture: ' + name + '\n').encode()
        path.write_bytes(raw)
        files[name] = sha256(raw).hexdigest()
    value = {'schema': 1, 'target': 'Windows x64 / CPython 3.11',
             'bundled_python': False, 'bundled_model': False,
             'bundled_ocr': False, 'files': files}
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    return root, value


def test_legacy_sealed_native_entry_is_supported_without_granting_new_ownership(legacy):
    root, expected = legacy
    installer = module('install_windows')
    installer.verify(root)
    assert native_checks._manifest(root) == expected
    assert 'bundled_launcher' not in expected
    assert not (root / 'native-launcher-build.json').exists()


@pytest.mark.parametrize('change', ['unsealed-receipt', 'sealed-receipt', 'explicit-false'])
def test_legacy_compatibility_does_not_admit_an_unannounced_ownership_receipt(legacy, change):
    root, value = legacy
    if change == 'explicit-false':
        value['bundled_launcher'] = False
    else:
        receipt = root / 'native-launcher-build.json'
        receipt.write_bytes(b'not a source-bound owner receipt')
        if change == 'sealed-receipt':
            value['files'][receipt.name] = sha256(receipt.read_bytes()).hexdigest()
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='unannounced native launcher'):
        module('install_windows').verify(root)


def test_legacy_entry_still_requires_the_exact_sealed_executable(legacy):
    root, _value = legacy
    (root / 'ResearchLibrary.exe').write_bytes(b'changed after sealing')
    with pytest.raises(ValueError, match='checksum'):
        module('install_windows').verify(root)


@pytest.mark.parametrize('arguments', [
    {'native_launcher': 'legacy.exe', 'launcher_dir': 'current-build'},
    {'native_launcher': 'legacy.exe', 'portable_updates': True},
    {'launcher_dir': 'current-build', 'portable_updates': True,
     'source_commit': 'a' * 40},
])
def test_incompatible_or_incomplete_profiles_fail_before_creating_output(tmp_path, arguments):
    output = tmp_path / 'untouched output'
    with pytest.raises(ValueError, match='Choose|portable updates require'):
        module('build_windows_bundle').build_bundle(
            tmp_path / 'app.whl', tmp_path / 'wheels', tmp_path / 'lock', output,
            **arguments)
    assert not output.exists()


@pytest.mark.parametrize('flag', [
    'bundled_native_runtime', 'bundled_audio_runtime', 'bundled_audio',
    'bundled_citation_runtime', 'bundled_launcher',
])
def test_all_supported_optional_profiles_keep_boolean_schema(legacy, flag):
    root, value = legacy
    value[flag] = False
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    assert native_checks._manifest(root) == value
    value[flag] = 0
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='boolean'):
        native_checks._manifest(root)


def test_canonical_tree_and_passive_historical_provenance_are_not_dropped(legacy):
    root, value = legacy
    value.update(source_commit='a' * 40, source_tree='b' * 40,
                 parent_source_commit='c' * 40, parent_manifest_sha256='d' * 64,
                 use_scope='private-personal-installation',
                 bundled_audio_runtime=True)
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    assert native_checks._manifest(root) == value
    value['source_tree'] = 'HEAD'
    (root / 'bundle-manifest.json').write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='source identity'):
        native_checks._manifest(root)
