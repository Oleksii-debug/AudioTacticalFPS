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
