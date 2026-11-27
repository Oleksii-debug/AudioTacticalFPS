"""Actual SQLite backup/restore of passive, synthetic engine declarations."""
from copy import deepcopy
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library import backup_sidecars
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup


NAME = 'state/audio/engines.json'


def configuration():
    # Deliberately nonexistent Windows/POSIX references, never executables or
    # model fixtures. Restoring settings must not inspect the other machine.
    return {
        'schema': 1,
        'ffmpeg': r'C:\Synthetic uninstalled engines\ffmpeg.exe',
        'ffprobe': '/synthetic-uninstalled-engines/ffprobe',
        'piper': [{'executable': '../synthetic-uninstalled-engines/piper', 'models': [{
            'path': r'D:\Synthetic voice models\voice.onnx',
            'config': r'D:\Synthetic voice models\voice.onnx.json',
            'sha256': 'a' * 64, 'config_sha256': 'b' * 64,
            'name': 'Synthetic voice declaration', 'language': 'uk',
            'license': 'Synthetic license metadata; no model is supplied.',
        }]}],
        'voice_helpers': [
            {'executable': '/synthetic-uninstalled-engines/helper', 'sha256': 'c' * 64,
             'engine_name': 'Existing JSON helper', 'enabled': True, 'max_voices': 2048},
            {'executable': 'relative-missing-helper.exe', 'sha256': 'd' * 64,
             'engine_name': 'Existing helper with optional defaults'},
            {'adapter': 'dordiai-voices', 'runtime_directory': r'C:\Synthetic voices\runtime',
             'rights_notice': 'Дозвіл для синтетичного прикладу; no real voice supplied.', 'enabled': True},
            {'adapter': 'dordiai-voices', 'runtime_directory': '/synthetic-missing-voice-runtime',
             'python_executable': None, 'expected_worker_sha256': 'e' * 64,
             'rights_notice': 'Disabled synthetic installation.', 'enabled': False},
        ],
        'music_models': [
            {'engine': 'musicgen', 'python': r'C:\Synthetic music\python.exe',
             'directory': r'C:\Synthetic music\models', 'manifest': r'C:\Synthetic music\model.json',
             'device': 'cpu', 'private_noncommercial': True, 'max_chunk_seconds': 25.5},
            {'engine': 'ace-step', 'python': '/synthetic-music/python',
             'directory': '/synthetic-music/models', 'manifest': '/synthetic-music/model.json'},
        ],
        'fluidsynth': {'binary': '/synthetic-uninstalled-engines/fluidsynth',
                      'soundfont': r'D:\Synthetic soundfonts\fixture.sf2', 'sha256': 'f' * 64,
                      'license_notice': 'Synthetic SoundFont rights; no SoundFont supplied.'},
    }


def write_settings(root, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    path = root / NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def snapshot_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def resign(folder, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def historical_settings_manifest(folder, manifest, version):
    """Use exact old envelopes only for the explicit legacy reader cases."""
    assert manifest['version'] == 5 and manifest['extensions']['version'] == 4
    assert manifest.pop('extensions')['files'] == []
    (folder / 'extensions').rmdir()
    manifest['version'] = version
    if version == 3:
        # These fixtures have no imports and therefore no pending producer.
        assert manifest.pop('pending', None) is None
    elif version == 4:
        manifest['sidecars'] = [{**row, 'filename': 'state/' + row['filename']}
                                for row in manifest.pop('state')]
    else:
        raise AssertionError('Only exact historical V3/V4 settings are requested.')


def test_configured_engines_roundtrip_without_importing_or_running_engines(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic engine backup')
    app.add_text(workspace, 'Source remains intact', 'Unchanged source text.', edition_id='edition')
    original = write_settings(app.root, configuration())

    def forbidden(*args, **kwargs):
        pytest.fail('A passive settings backup must not import or execute an optional engine.')

    monkeypatch.setattr(backup_sidecars, 'import_module', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 5 and manifest['extensions']['version'] == 4
    assert manifest['state'] == [{'filename': NAME.removeprefix('state/'), 'size_bytes': len(original),
                                 'sha256': sha256(original).hexdigest()}]
    before = snapshot_files(folder)
    target = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (target / NAME).read_bytes() == original == (app.root / NAME).read_bytes()
    assert json.loads((target / NAME).read_bytes()) == configuration()
    assert LibraryApp(target).edition_metadata(workspace, 'edition').title == 'Source remains intact'
    assert snapshot_files(folder) == before


def test_legacy_generic_helper_settings_keep_exact_bytes_and_defaults(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Legacy helper settings')
    value = configuration()
    value.update(ffmpeg=None, ffprobe=None, piper=[], music_models=[], fluidsynth=None)
    value['voice_helpers'] = value['voice_helpers'][:2]
    raw = write_settings(app.root, value)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (restored / NAME).read_bytes() == raw


def test_v3_state_shape_can_restore_engine_settings_without_rewriting_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Legacy state manifest')
    raw = write_settings(app.root, configuration())
    folder = tmp_path / 'legacy-v3'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    historical_settings_manifest(folder, manifest, 3)
    checksum = resign(folder, manifest)
    before = snapshot_files(folder)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (restored / NAME).read_bytes() == raw
    assert snapshot_files(folder) == before


@pytest.mark.parametrize(('location', 'field', 'invalid'), [
    ((), 'schema', True),
    ((), 'credentials', {'api_key': 'synthetic-secret-must-not-be-accepted'}),
    ((), 'voice_helpers', {}),
    ((), 'ffmpeg', 'https://example.invalid/engine.exe'),
    ((), 'ffprobe', r'\\server\share\ffprobe.exe'),
    (('piper', 0), 'command', 'arbitrary command'),
    (('piper', 0, 'models', 0), 'api_key', 'synthetic-secret-must-not-be-accepted'),
    (('piper', 0, 'models', 0), 'config_sha256', 'not-a-checksum'),
    (('voice_helpers', 0), 'enabled', 1),
    (('voice_helpers', 0), 'max_voices', 10001),
    (('voice_helpers', 0), 'credential_ref', 'synthetic-reference'),
    (('voice_helpers', 2), 'adapter', 'unrecognized-transport'),
    (('voice_helpers', 2), 'rights_notice', ''),
    (('voice_helpers', 3), 'python_executable', 'file:///engine.exe'),
    (('voice_helpers', 3), 'expected_worker_sha256', None),
    (('music_models', 0), 'directory', 'relative-model-directory'),
    (('music_models', 0), 'private_noncommercial', 'yes'),
    (('music_models', 0), 'max_chunk_seconds', True),
    (('music_models', 0), 'max_chunk_seconds', 10 ** 1000),
    (('music_models', 0), 'device', 'automatic-cloud'),
    (('music_models', 0), 'token', 'synthetic-secret-must-not-be-accepted'),
    (('fluidsynth',), 'soundfont', r'C:\Soundfont\voice.sf2:stream'),
    (('fluidsynth',), 'license_notice', ''),
])
def test_invalid_engine_declarations_reject_backup_and_keep_source(tmp_path, location, field, invalid):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Invalid settings remain recoverable')
    value = configuration()
    record = value
    for part in location:
        record = record[part]
    record[field] = invalid
    raw = write_settings(app.root, value)
    target = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError) as error:
        create_backup(app, target)
    assert not target.exists()
    assert (app.root / NAME).read_bytes() == raw
    assert 'synthetic-secret-must-not-be-accepted' not in str(error.value)


def test_semantically_invalid_resigned_restore_preserves_inputs_and_publishes_nothing(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Restore validation')
    original = write_settings(app.root, configuration())
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    malicious = configuration()
    malicious['voice_helpers'][2]['credential'] = 'synthetic-secret-must-not-be-accepted'
    raw = write_settings(folder, malicious)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['state'][0].update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = resign(folder, manifest)
    before = snapshot_files(folder)
    target = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError, match='engine setting fields'):
        restore_backup(folder, target, checksum)
    assert not target.exists()
    assert snapshot_files(folder) == before
    assert (app.root / NAME).read_bytes() == original
    assert not list(tmp_path.glob('.carr-library-restore-*'))


@pytest.mark.parametrize('change', ['duplicate-piper', 'too-many-providers', 'nonfinite', 'duplicate-json'])
def test_settings_collection_and_json_boundaries(change):
    value = configuration()
    if change == 'duplicate-piper':
        value['piper'][0]['models'].append(deepcopy(value['piper'][0]['models'][0]))
    elif change == 'too-many-providers':
        value['voice_helpers'] = [value['voice_helpers'][0]] * 101
    elif change == 'nonfinite':
        value['music_models'][0]['max_chunk_seconds'] = float('nan')
    raw = json.dumps(value).encode('utf-8')
    if change == 'duplicate-json':
        raw = raw.replace(b'"schema": 1', b'"schema": 1, "schema": 1', 1)
    with pytest.raises(ValueError):
        backup_sidecars.validate_sidecar(None, NAME, raw)
