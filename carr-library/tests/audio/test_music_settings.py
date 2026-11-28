"""Local registration integrity, usable CLI/model controls, and explicit engine choice."""
import argparse
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest

from carr_library.audio.cli import add_arguments, execute, requested
from carr_library.audio.music_settings import (MusicSettingsStore, load_music_options,
    music_configuration_status, validate_music_settings)
from carr_library.audio.projects import parse_script
from carr_library.audio.render import file_sha256, tool_path
from carr_library.ui.music_settings import MusicSettingsModel


def installation(tmp_path):
    directory = tmp_path/'weights'
    directory.mkdir()
    (directory/'config.json').write_text('{}', encoding='utf-8')
    (directory/'model.safetensors').write_bytes(b'inert synthetic inventory fixture; never inference')
    manifest = dict(version=1, engine='musicgen-transformers', model_id='facebook/musicgen-small',
        revision='a'*40, weights_license='CC-BY-NC-4.0', code_license='Apache-2.0',
        transformers_version='4.57.6', torch_version='2.6.0',
        files=[dict(path=p.name, size_bytes=p.stat().st_size, sha256=file_sha256(p)) for p in sorted(directory.iterdir())])
    path = tmp_path/'model-manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return dict(model_dir=str(directory), manifest=str(path), manifest_sha256=file_sha256(path),
                python_executable=os.path.abspath(sys.executable), python_sha256=file_sha256(Path(sys.executable)),
                device='cpu', timeout_seconds=60, private_noncommercial=True)


def test_settings_save_validate_load_and_revision_conflict(tmp_path):
    store = MusicSettingsStore(tmp_path/'library')
    value = store.read()
    value['musicgen'] = installation(tmp_path)
    saved = store.save(value,expected_revision=0)
    assert saved['revision'] == 1
    options = load_music_options(tmp_path/'library')
    assert options['musicgen_config'].manifest_sha256 == value['musicgen']['manifest_sha256']
    assert options['musicgen_config'].python_executable == os.path.abspath(sys.executable)
    assert not music_configuration_status(tmp_path/'library')['musicgen']['inference_tested']
    with pytest.raises(ValueError,match='another window'):
        store.save(value,expected_revision=0)
    store.remove('musicgen',expected_revision=1)
    assert load_music_options(tmp_path/'library') == {}
    assert Path(value['musicgen']['model_dir']).is_dir()


@pytest.mark.parametrize('field,value', [('manifest_sha256','0'*64),('python_sha256','1'*64),
    ('private_noncommercial',False),('device','remote'),('timeout_seconds',float('nan')),
    ('manifest','https://models.invalid/model.json')])
def test_invalid_or_changed_installation_does_not_publish(tmp_path,field,value):
    store = MusicSettingsStore(tmp_path/'library')
    settings = store.read()
    settings['musicgen'] = installation(tmp_path)
    settings['musicgen'][field] = value
    with pytest.raises((ValueError,OSError)):
        store.save(settings,expected_revision=0)
    assert not store.path.exists()


def test_registration_load_detects_changed_model_manifest(tmp_path):
    store = MusicSettingsStore(tmp_path/'library')
    value = store.read()
    value['musicgen'] = installation(tmp_path)
    store.save(value,expected_revision=0)
    Path(value['musicgen']['manifest']).write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):
        load_music_options(tmp_path/'library')
    # A stale registration can be removed without needing its lost assets.
    store.remove('musicgen',expected_revision=1)
    assert load_music_options(tmp_path/'library') == {}


def test_structural_backup_validation_keeps_foreign_paths_without_loading(tmp_path):
    value = dict(version=1,revision=5,musicgen=None,fluidsynth=dict(
        soundfont='C:\\Reviewed\\font.sf2',soundfont_sha256='a'*64,soundfont_license='Reviewed synthetic terms',
        fluidsynth_library='C:\\Reviewed\\libfluidsynth.dll',fluidsynth_sha256='b'*64))
    assert validate_music_settings(value,check_files=False) == value
    with pytest.raises((OSError,ValueError)):
        validate_music_settings(value,check_files=True)


def test_secret_or_command_fields_are_not_configuration(tmp_path):
    value = MusicSettingsStore(tmp_path/'library').read()
    value['api_key'] = 'never-persist-this'
    with pytest.raises(ValueError):
        validate_music_settings(value)


def test_settings_form_uses_local_model_environment_and_actual_seal(tmp_path):
    record = installation(tmp_path)
    model = MusicSettingsModel(tmp_path/'library')
    fields = {name: str(record[name]) for name in ('model_dir','manifest','manifest_sha256',
                                                 'python_executable','device','timeout_seconds')}
    result = model.save('musicgen',fields,expected_revision=0)
    assert result['musicgen']['python_sha256'] == file_sha256(Path(sys.executable))
    assert tool_path('Python',sys.executable) == os.path.abspath(sys.executable)
    assert model.values('musicgen')['manifest'] == record['manifest']


def test_real_procedural_sample_through_native_settings_model(tmp_path):
    result = MusicSettingsModel(tmp_path/'library').sample('procedural',tmp_path/'sample.wav')
    assert result.frames == 72000 and result.channels == 2 and result.path.is_file()
    assert result.metadata['original_neural_generation'] is False


def test_missing_registered_engine_fails_without_fallback(tmp_path):
    model = MusicSettingsModel(tmp_path/'library')
    for engine in ('musicgen','fluidsynth'):
        with pytest.raises(ValueError):
            model.sample(engine,tmp_path/(engine+'.wav'))
        assert not (tmp_path/(engine+'.wav')).exists()


def test_cli_registration_status_and_real_sample_use_same_core(tmp_path):
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    output = []
    status = parser.parse_args(['--music-status'])
    assert requested(status)
    assert execute(status,tmp_path/'library',printer=output.append) == 0
    assert json.loads(output[-1])['mode'] == 'LOCAL_ONLY'
    sample = parser.parse_args(['--music-sample',str(tmp_path/'cli.wav')])
    assert execute(sample,tmp_path/'library',printer=output.append) == 0
    assert json.loads(output[-1])['frames'] == 72000
    with pytest.raises(ValueError,match='separately'):
        execute(parser.parse_args(['--music-status','--studio-list']),tmp_path/'library',printer=output.append)


def test_engine_directive_and_prompt_preserve_original_script():
    script = '@music musicgen suspense 100\n@music-prompt Quiet strings, no vocals.\nNarrator: Original source.\n'
    project = parse_script(script,['local'])
    assert project.script == script
    assert project.scenes[0].music.engine == 'musicgen'
    assert project.scenes[0].music.prompt == 'Quiet strings, no vocals.'
    assert project.scenes[0].music.tempo_bpm == 100
    assert parse_script('@music calm 80\nText',['local']).scenes[0].music.engine == 'procedural'
    assert parse_script('@music fluidsynth calm\nText',['local']).scenes[0].music.engine == 'fluidsynth'
    plain = parse_script(script,['local'],directives=False)
    assert plain.scenes[0].music is None
    assert '@music-prompt' in plain.script


@pytest.mark.parametrize('script', ['@music-prompt no preceding music\nText',
    '@music calm\nText\n@music-prompt detached',
    '@music musicgen calm\n@music-prompt first\n@music-prompt second\nText',
    '@music musicgen\nText'])
def test_ambiguous_music_directive_is_rejected(script):
    with pytest.raises(ValueError):
        parse_script(script,['local'])
