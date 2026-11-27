"""Restored engine declarations remain inspectable and fail closed until review."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys

import pytest

from carr_library.durable_state import atomic_json
from carr_library import runtime_settings


def document(tmp_path):
    return {'schema': 1, 'ffmpeg': str(tmp_path / 'missing-ffmpeg.exe'), 'ffprobe': None,
            'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None,
            'restore_requires_revalidation': True}


@pytest.mark.parametrize('operation', ['voice', 'audio'])
def test_restored_engine_activation_blocks_before_external_path_access(tmp_path, monkeypatch, operation):
    from carr_library.audio import musicgen_worker
    state = tmp_path / 'state'
    value = document(tmp_path)
    path = runtime_settings.settings_path(state)
    atomic_json(path, value)
    assert runtime_settings.load_settings(state) == value
    def forbidden(*args, **kwargs):
        raise AssertionError('A restored declaration cannot resolve or launch external assets.')
    monkeypatch.setattr(musicgen_worker, 'plain_local', forbidden)
    with pytest.raises(ValueError, match='revalidation'):
        if operation == 'voice':
            runtime_settings.voice_registry(state)
        else:
            runtime_settings.audio_runtime(tmp_path / 'never-created', state_root=state)
    assert not (tmp_path / 'never-created').exists()


@pytest.mark.parametrize('marker', [0, 1, None, 'true', [], {}])
def test_restore_marker_requires_an_actual_boolean(tmp_path, marker):
    value = document(tmp_path)
    value['restore_requires_revalidation'] = marker
    atomic_json(runtime_settings.settings_path(tmp_path / 'state'), value)
    with pytest.raises(ValueError, match='configuration'):
        runtime_settings.load_settings(tmp_path / 'state')


def test_failed_asset_review_preserves_the_restore_barrier_and_original_bytes(tmp_path):
    value = document(tmp_path)
    state = tmp_path / 'state'
    path = runtime_settings.settings_path(state)
    atomic_json(path, value)
    before = path.read_bytes()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        runtime_settings.save_settings(state, value)
    assert path.read_bytes() == before
    assert runtime_settings.load_settings(state)['restore_requires_revalidation'] is True


def test_explicit_review_clears_only_the_marker_and_preserves_disabled_declarations(tmp_path):
    value = document(tmp_path)
    value['ffmpeg'] = None
    value['voice_helpers'] = [{'executable': str(tmp_path / 'absent-disabled-helper.exe'),
                              'sha256': '0' * 64, 'engine_name': 'Disabled local fixture',
                              'enabled': False}]
    original = deepcopy(value)
    state = tmp_path / 'state'
    atomic_json(runtime_settings.settings_path(state), value)
    runtime_settings.save_settings(state, value)
    result = runtime_settings.load_settings(state)
    assert result == {**original, 'restore_requires_revalidation': False}
    assert value == original, 'Review must not mutate the caller-held configuration.'


def test_enabled_helper_must_complete_real_protocol_review_before_unblocking(tmp_path):
    # The real Python executable is deliberately NOT a voice-helper protocol.
    # This checks process/protocol rejection, never a speech-inference claim.
    binary = Path(sys.executable).resolve(strict=True)
    value = document(tmp_path)
    value['ffmpeg'] = None
    value['voice_helpers'] = [{'executable': str(binary),
                              'sha256': sha256(binary.read_bytes()).hexdigest(),
                              'engine_name': 'Invalid protocol fixture', 'enabled': True}]
    state = tmp_path / 'state'
    path = runtime_settings.settings_path(state)
    atomic_json(path, value)
    before = path.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        runtime_settings.save_settings(state, value)
    assert path.read_bytes() == before


def _legacy_helper(enabled=False, *, runtime_directory=r'..\legacy\voice-runtime'):
    return {'adapter': 'dordiai-voices', 'runtime_directory': runtime_directory,
            'rights_notice': 'Synthetic declaration retained for explicit review only.',
            'enabled': enabled, 'python_executable': None,
            'expected_worker_sha256': 'd' * 64}


def _legacy_settings(helper):
    return {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
            'voice_helpers': [helper], 'music_models': [], 'fluidsynth': None}


def _deny_legacy_asset_access(monkeypatch):
    from carr_library import speech
    from carr_library.audio import musicgen_worker
    def forbidden(*args, **kwargs):
        raise AssertionError('A retained legacy declaration must not inspect or launch voice assets.')
    monkeypatch.setattr(musicgen_worker, 'plain_local', forbidden)
    monkeypatch.setattr(speech, 'LocalHelperProvider', forbidden)
    monkeypatch.setattr(speech, 'PiperProvider', forbidden)
    return forbidden


@pytest.mark.parametrize('declared', [
    r'..\legacy\voice-runtime', r'C:\AbsentOwnerRuntime\voices', '/absent/owner/voices',
], ids=['relative', 'windows', 'posix'])
def test_disabled_legacy_helper_is_pure_data_and_never_constructed(monkeypatch, declared):
    forbidden = _deny_legacy_asset_access(monkeypatch)
    monkeypatch.setattr(runtime_settings, '_declared_path', forbidden)
    helper = _legacy_helper(runtime_directory=declared)
    value = _legacy_settings(helper)
    original = deepcopy(value)
    assert runtime_settings.validate_legacy_dordiai_helper(helper) == helper
    assert runtime_settings._settings_document(value) == original
    assert runtime_settings._configured_voice_providers(value) == []
    assert value == original


@pytest.mark.parametrize(('field', 'invalid'), [
    ('adapter', 'other-adapter'), ('enabled', 1), ('runtime_directory', ''),
    ('runtime_directory', '   '), ('runtime_directory', '//server/share'),
    ('runtime_directory', r'\/server/share'), ('runtime_directory', 'https://example.invalid/voice'),
    ('runtime_directory', r'C:\voices\asset:stream'), ('runtime_directory', 'voice\tpath'),
    ('runtime_directory', 'x' * 4097), ('rights_notice', ''), ('rights_notice', '\ud800'),
    ('rights_notice', 'x' * 4097), ('python_executable', r'\\server\python.exe'),
    ('python_executable', 'file:///python'), ('expected_worker_sha256', None),
    ('expected_worker_sha256', 'A' * 64), ('executable', '/mixed-current-and-legacy'),
], ids=['adapter', 'bool', 'empty-path', 'blank-path', 'unc', 'mixed-unc', 'url',
        'ads', 'control', 'path-bound', 'empty-notice', 'unicode', 'notice-bound',
        'python-unc', 'python-url', 'null-sha', 'uppercase-sha', 'mixed-shape'])
def test_legacy_helper_schema_rejects_malformed_declarations_without_io(monkeypatch, field, invalid):
    forbidden = _deny_legacy_asset_access(monkeypatch)
    monkeypatch.setattr(runtime_settings, '_declared_path', forbidden)
    helper = {**_legacy_helper(), field: invalid}
    with pytest.raises(ValueError):
        runtime_settings._settings_document(_legacy_settings(helper))


@pytest.mark.parametrize('missing', ['adapter', 'runtime_directory', 'rights_notice', 'enabled'])
def test_legacy_helper_requires_its_complete_canonical_schema(missing):
    helper = _legacy_helper()
    helper.pop(missing)
    with pytest.raises(ValueError):
        runtime_settings._settings_document(_legacy_settings(helper))


@pytest.mark.parametrize('operation', ['voice', 'audio', 'save', 'provider-factory'])
def test_unmarked_enabled_legacy_helper_rejects_before_any_engine_admission(tmp_path, monkeypatch, operation):
    forbidden = _deny_legacy_asset_access(monkeypatch)
    monkeypatch.setattr(runtime_settings, 'verify_configured_audio_runtime', forbidden)
    value = _legacy_settings(_legacy_helper(enabled=True))
    # An earlier valid current declaration must not be probed before this
    # incompatible enabled declaration has been rejected.
    value['piper'] = [{'executable': str(tmp_path / 'absent-piper.exe'), 'models': []}]
    state = tmp_path / 'state'
    path = runtime_settings.settings_path(state)
    atomic_json(path, value)
    before = path.read_bytes()
    assert runtime_settings.load_settings(state) == value
    with pytest.raises(ValueError, match='migration'):
        if operation == 'voice':
            runtime_settings.voice_registry(state)
        elif operation == 'audio':
            runtime_settings.audio_runtime(tmp_path / 'uncreated-output', state_root=state)
        elif operation == 'save':
            runtime_settings.save_settings(state, value)
        else:
            runtime_settings._configured_voice_providers(value)
    assert path.read_bytes() == before
    assert not (tmp_path / 'uncreated-output').exists()


def _flat_engine_backup(app, destination):
    """Transpose a real composite backup into the accepted canonical inventory."""
    import json
    import shutil
    from carr_library.library_backup import create_backup
    create_backup(app, destination)
    own = json.loads((destination / 'manifest.json').read_bytes())
    entries = []
    for row in own.get('sidecars', {}).get('files', []):
        name = row['path']
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(destination / 'sidecars' / name, target)
        entries.append({'filename': name, 'size_bytes': row['size_bytes'], 'sha256': row['sha256']})
    if (destination / 'sidecars').exists():
        shutil.rmtree(destination / 'sidecars')
    entries.extend({**row, 'filename': 'state/' + row['filename']} for row in own.get('state', []))
    if not own.get('state') and (destination / 'state').exists():
        (destination / 'state').rmdir()
    value = {key: own[key] for key in ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
    value.update(version=4, sidecars=sorted(entries, key=lambda row: row['filename']),
                 generated_audio_included=False)
    if 'pending' in own:
        value['pending'] = own['pending']
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    (destination / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('layout', ['composite', 'canonical-flat'])
@pytest.mark.parametrize('enabled', [False, True], ids=['disabled', 'enabled'])
def test_real_backup_restore_preserves_legacy_helper_and_revokes_activation(tmp_path, monkeypatch, layout, enabled):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    from carr_library.durable_state import read_json
    _deny_legacy_asset_access(monkeypatch)
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Legacy engine restore')
    app.add_text(workspace, 'Synthetic source', 'Exact source retained across local engine restoration.')
    value = _legacy_settings(_legacy_helper(enabled=enabled))
    path = runtime_settings.settings_path(app.root / 'state')
    atomic_json(path, value)
    original = path.read_bytes()
    backup = tmp_path / 'backup'
    if layout == 'canonical-flat':
        checksum = _flat_engine_backup(app, backup)
    else:
        create_backup(app, backup)
        checksum = sha256((backup / 'manifest.json').read_bytes()).hexdigest()
    manifest = (backup / 'manifest.json').read_bytes()
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    state = restored / 'state'
    expected = {**value, 'restore_requires_revalidation': True}
    assert runtime_settings.load_settings(state) == expected
    assert read_json(runtime_settings.settings_path(state)) == expected
    assert path.read_bytes() == original and (backup / 'manifest.json').read_bytes() == manifest
    before = runtime_settings.settings_path(state).read_bytes()
    with pytest.raises(ValueError, match='revalidation'):
        runtime_settings.voice_registry(state)
    if enabled:
        # Even an explicitly proposed cleared marker cannot authorize this
        # other declaration format or silently fall back to a default voice.
        with pytest.raises(ValueError, match='migration'):
            runtime_settings.save_settings(state, {**expected, 'restore_requires_revalidation': False})
        assert runtime_settings.settings_path(state).read_bytes() == before
    else:
        reviewed = runtime_settings.save_settings(state, expected)
        assert reviewed == {**expected, 'restore_requires_revalidation': False}
        assert runtime_settings.load_settings(state) == reviewed
        assert runtime_settings._configured_voice_providers(reviewed) == []
    assert LibraryApp(restored).search(workspace, 'Exact')
