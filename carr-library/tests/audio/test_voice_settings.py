"""Persistent registration boundaries and real shared product entry points.

The small Piper assets below are manifest fixtures, never a neural-inference
claim. Existing speech suites separately render with actual installed engines.
"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys

import pytest

from carr_library.audio.cli import add_arguments, execute, requested
from carr_library.audio.director import ProductionDirector
from carr_library.audio.voice_settings import (VoiceSettingsStore, load_registered_voice_config,
    validate_voice_config, validate_voice_settings_document)
from carr_library.audio.voices import VoiceRegistry
from carr_library.ui.audio_studio import StudioController
from carr_library.ui.voice_settings import FIELDS, VoiceSettingsModel


def installation(tmp_path):
    folder = tmp_path / 'installed-piper'
    folder.mkdir()
    files = {'voice.onnx': b'INERT SYNTHETIC TEST FIXTURE; NOT A MODEL', 'voice.json': b'{}'}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    manifest = {'schema_version': 1, 'engine': 'piper', 'model_id': 'synthetic-profile',
        'revision': 'a' * 40, 'license': 'Synthetic fixture only',
        'files': {name: sha256(data).hexdigest() for name, data in files.items()},
        'settings': {'model': 'voice.onnx', 'config': 'voice.json', 'language': 'uk', 'name': 'Fixture Ukrainian'},
        'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': False}
    path = folder / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return {'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
            'piper': {'executable': str(Path(sys.executable).resolve()), 'manifest': str(path)}}


def test_persisted_registration_is_the_single_default_for_studio_director_cli_and_assistant(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.assistant.workflows import LocalAssistant, Scope, Step, WorkflowPlan
    config = installation(tmp_path)
    library = LibraryApp(tmp_path / 'library')
    store = VoiceSettingsStore(library.root)
    saved = store.register(config, expected_revision=0)
    assert saved['revision'] == 1 and set(saved['bindings']) == {'piper.executable', 'piper.manifest'}
    studio = StudioController(library.root)
    voice, = studio.voices
    project = studio.save('Перша сцена', 'Оповідач: Початок.', 'uk')
    assert project.roles[0].voice_id == voice.id and voice.languages == ('uk',)
    assert ProductionDirector(library.root).registry.list_voices() == studio.voices
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    lines = []
    assert execute(parser.parse_args(['--voices']), library.root, printer=lines.append) == 0
    assert json.loads(lines[-1])['voices'][0]['id'] == voice.id
    assistant = LocalAssistant(library, Scope())
    plan = WorkflowPlan('list-configured-voices', 'List configured voices', (Step('voices', 'voices.list', {}),))
    approved = assistant.prepare(plan)
    result = assistant.run(plan.id, approved_fingerprint=approved['fingerprint'])
    assert result['state'] == 'complete'
    assert assistant.registry.list_voices() == studio.voices
    assert load_registered_voice_config(library.root) == config


def test_cli_registration_and_default_language_check_before_project_creation(tmp_path):
    config = installation(tmp_path)
    selected = tmp_path / 'selected.json'
    selected.write_text(json.dumps({'version': 1, 'kind': 'local-voice-engines', 'config': config}))
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    lines, root = [], tmp_path / 'library'
    args = parser.parse_args(['--voice-config', str(selected)])
    assert requested(args) and execute(args, root, printer=lines.append) == 0
    assert json.loads(lines[-1])['inference_tested'] is False
    script = tmp_path / 'source.txt'
    script.write_text('Narrator: Original.', encoding='utf-8')
    with pytest.raises(ValueError, match='declares the requested language'):
        execute(parser.parse_args(['--studio-script', str(script), '--audio-language', 'sk']), root)
    with pytest.raises(ValueError, match='separately'):
        execute(parser.parse_args(['--voice-status', '--studio-list']), root)
    assert execute(parser.parse_args(['--studio-script', str(script), '--audio-language', 'uk']), root,
                   printer=lines.append) == 0
    project = json.loads(lines[-1])
    assert project['roles'][0]['voice_id'].startswith('piper:') and project['roles'][0]['language'] == 'uk'
    assert execute(parser.parse_args(['--voice-reset']), root, printer=lines.append) == 0
    assert VoiceSettingsStore(root).read()['config'] == {}
    assert Path(config['piper']['manifest']).is_file()


def test_runtime_and_manifest_bindings_are_rechecked_even_after_voice_enumeration(tmp_path):
    config = installation(tmp_path)
    store = VoiceSettingsStore(tmp_path / 'library')
    store.register(config, expected_revision=0)
    registry = VoiceRegistry.for_library(tmp_path / 'library')
    assert registry.list_voices()
    manifest = Path(config['piper']['manifest'])
    manifest.write_text(manifest.read_text() + '\n')
    with pytest.raises(ValueError, match='changed'):
        registry.list_voices()
    with pytest.raises(ValueError, match='changed'):
        VoiceRegistry.for_library(tmp_path / 'library')
    # The explicit revalidation action accepts the reviewed current bytes.
    store.revalidate(expected_revision=1)
    assert VoiceRegistry.for_library(tmp_path / 'library').list_voices()
    store.reset(expected_revision=2)
    assert store.read()['bindings'] == {}


def test_concurrent_or_cancelled_registration_never_replaces_saved_config(tmp_path):
    config = installation(tmp_path)
    store = VoiceSettingsStore(tmp_path / 'library')
    with pytest.raises(InterruptedError):
        store.register(config, expected_revision=0, cancelled=lambda: True)
    assert not store.path.exists()
    saved = store.register(config, expected_revision=0)
    with pytest.raises(ValueError, match='another window'):
        store.register(config, expected_revision=0)
    assert store.read() == saved
    with pytest.raises(ValueError, match='another window'):
        store.reset(expected_revision=0)


@pytest.mark.parametrize('value', [
    {'api_key': 'do-not-persist'}, {'piper': {'manifest': '/local/manifest', 'executable': '/local/run', 'command': 'x'}},
    {'ffmpeg': True}, {'include_sapi': 1}, {'dordiai_manifest': '//server/model.json'},
    {'dordiai_manifest': 'C:\\model.json:stream'}, {'dordiai_manifest': 'C:\\..\\model.json'},
    {'dordiai_manifest': 'https://model.invalid/a'}, {'piper': {'executable': 'run', 'manifest': '/m'}},
])
def test_configuration_rejects_remote_secret_command_and_ambiguous_paths(value):
    with pytest.raises(ValueError):
        validate_voice_config(value)


def test_backup_decoder_is_pure_and_rejects_unknown_fields_and_unbound_paths(tmp_path):
    value = {'version': 1, 'kind': 'local-voice-settings', 'revision': 7, 'restored': False,
        'config': {'dordiai_manifest': 'C:\\Reviewed\\manifest.json'},
        'bindings': {'dordiai_manifest': 'b' * 64}}
    assert validate_voice_settings_document(value) == value
    for edited in ({**value, 'secret': 'never'}, {**value, 'bindings': {}}, {**value, 'restored': 1}):
        with pytest.raises(ValueError):
            validate_voice_settings_document(edited)
    bad = deepcopy(value)
    bad['config']['dordiai_manifest'] = 'C:\\Reviewed\\manifest.json:stream'
    with pytest.raises(ValueError):
        validate_voice_settings_document(bad)


def test_native_form_model_registers_same_config_and_requires_complete_piper_pair(tmp_path):
    config = installation(tmp_path)
    model = VoiceSettingsModel(tmp_path / 'library')
    fields = {key: '' for key, _ in FIELDS}
    fields.update(piper_executable=config['piper']['executable'], piper_manifest=config['piper']['manifest'])
    saved = model.save(fields, sapi=False, flite=False, espeak=False, expected_revision=0)
    assert saved['config'] == config
    assert 'Fixture Ukrainian' in model.describe()
    fields['piper_manifest'] = ''
    with pytest.raises(ValueError, match='both'):
        model.save(fields, sapi=False, flite=False, espeak=False, expected_revision=1)


def test_full_backup_preserves_registration_inactive_without_copying_external_assets(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    config = installation(tmp_path)
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Books')
    library.add_text(workspace, 'Source', 'Source remains unchanged.')
    VoiceSettingsStore(library.root).register(config, expected_revision=0)
    backup = tmp_path / 'backup'
    checksum = create_backup(library, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    paths = {row['path'] for row in manifest['sidecars']['files']}
    assert 'studio/voice-settings.json' in paths
    assert not any('voice.onnx' in path or 'manifest.json' in path for path in paths)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    copied = VoiceSettingsStore(root)
    assert copied.read()['restored'] is True and copied.read()['config'] == config
    with pytest.raises(ValueError, match='revalidation'):
        VoiceRegistry.for_library(root)
    copied.revalidate(expected_revision=1)
    assert VoiceRegistry.for_library(root).voices_for_language('uk')
    assert VoiceSettingsStore(library.root).read()['restored'] is False
