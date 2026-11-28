"""Primary dordiai settings use the existing typed common4 engine adapter."""
from copy import deepcopy
import json
import subprocess

import pytest

from carr_library import backup_sidecars, runtime_settings
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from test_engine_settings_backup import (
    NAME, configuration, guard_collection_imports, resign, snapshot_files, write_settings,
)


MISSING = object()


@pytest.mark.parametrize('version', [3, 4])
@pytest.mark.parametrize('declaration', [MISSING, None, {'enabled': False},
    {'enabled': True, 'runtime_directory': r'C:\Synthetic missing voices\kx47Runtime'},
    {'enabled': False, 'runtime_directory': None, 'python_executable': None},
    {'enabled': True, 'runtime_directory': '/synthetic-missing-voices/kx47Runtime',
     'python_executable': '/synthetic-missing-voices/python', 'expected_worker_sha256': '1' * 64}])
def test_primary_declaration_roundtrips_without_provider_probe_or_activation(
        tmp_path, monkeypatch, version, declaration):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Passive primary voice declaration')
    value = configuration()
    if declaration is not MISSING:
        value['dordiai'] = deepcopy(declaration)
    original_value = deepcopy(value)
    raw = write_settings(app.root, value)

    def forbidden(*args, **kwargs):
        pytest.fail('Examining archived engine settings must not probe or activate an engine.')

    real_import = backup_sidecars.import_module
    imports = []
    def pure_validator_only(name, package):
        assert (name, package) == ('.runtime_settings', 'carr_library')
        imports.append(name)
        return real_import(name, package)
    collection_imports = guard_collection_imports(monkeypatch, pure_validator_only)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    monkeypatch.setattr(runtime_settings, 'load_settings', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    if version == 3:
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest['version'] = 3
        manifest['state'] = [{**entry, 'filename': entry['filename'].removeprefix('state/')}
                             for entry in manifest.pop('sidecars')]
        checksum = resign(folder, manifest)
    before = snapshot_files(folder)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (restored / NAME).read_bytes() == raw == (app.root / NAME).read_bytes()
    assert json.loads(raw) == original_value == value
    assert workspace in {item.id for item in LibraryApp(restored).workspaces()}
    assert snapshot_files(folder) == before
    assert bool(imports) is (declaration is not MISSING and declaration is not None)
    assert set(collection_imports) == {
        ('.speech.tabs', 'carr_library'), ('.speech.playlist', 'carr_library'),
    }


@pytest.mark.parametrize('declaration', [True, {}, {'enabled': 1}, {'enabled': True},
    {'enabled': True, 'runtime_directory': '//server/share'},
    {'enabled': True, 'runtime_directory': 'https://example.invalid/voice'},
    {'enabled': True, 'runtime_directory': '/synthetic-local', 'python_executable': ''},
    {'enabled': True, 'runtime_directory': '/synthetic-local', 'expected_worker_sha256': 'invalid'},
    {'enabled': True, 'runtime_directory': '/synthetic-local', 'credential': 'synthetic-secret'}])
def test_bad_primary_declaration_rejects_backup_without_rewriting_source(tmp_path, declaration):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Invalid declared engine')
    value = configuration()
    value['dordiai'] = declaration
    raw = write_settings(app.root, value)
    target = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError):
        create_backup(app, target)
    assert not target.exists()
    assert (app.root / NAME).read_bytes() == raw


def test_pure_provider_validator_is_required_before_accepting_non_null_declaration(monkeypatch):
    value = configuration()
    value['dordiai'] = {'enabled': False}
    monkeypatch.setattr(runtime_settings, 'validate_dordiai_configuration', None)
    with pytest.raises(ValueError, match='settings validator'):
        backup_sidecars.validate_sidecar(None, NAME, json.dumps(value).encode())
