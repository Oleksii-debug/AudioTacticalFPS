"""Shared speech boundaries and actual native Windows SAPI synthesis.

The six real Flite cases are required separately in test_flite_runtime.py.
"""
from hashlib import sha256
import os
import sys
import time

import pytest

from carr_library.speech import (LocalHelperProvider, PiperProvider, SpeechSettings,
                                 VoiceRegistry, split_utterances)
from carr_library.speech._io import new_output, run_process


def test_split_retains_every_unicode_character_and_bounds_calls():
    text = ('Довгий український текст. A Slovak sentence: Dobrý deň.\n' * 200) + '尾声'
    chunks = list(split_utterances(text))
    assert ''.join(chunks) == text
    assert all(len(chunk) <= 2400 for chunk in chunks)


@pytest.mark.parametrize('settings', [dict(rate=True), dict(rate=11), dict(volume=-1),
    dict(pitch=100), dict(punctuation=1), dict(timeout_seconds=float('nan')), dict(max_output_bytes=2**32)])
def test_invalid_settings_fail_early(settings):
    with pytest.raises(ValueError):
        SpeechSettings(**settings)


def test_worker_timeout_and_output_budget_are_real_process_checks(tmp_path):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_process([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(30)'], timeout=.12)
    assert time.monotonic() - started < 3
    with pytest.raises(ValueError, match='diagnostic-output'):
        run_process([sys.executable, '-I', '-S', '-c', 'import sys; sys.stdout.write("x" * 2000100)'])


def test_atomic_output_publication_detects_concurrent_destination(tmp_path):
    target = tmp_path / 'result.wav'
    with pytest.raises(FileExistsError):
        with new_output(target) as staged:
            staged.write_bytes(b'our output')
            target.write_bytes(b'other output')
    assert target.read_bytes() == b'other output'
    assert len(list(tmp_path.iterdir())) == 1


def test_helper_requires_opt_in_and_exact_binary_hash(tmp_path):
    executable = tmp_path / 'voice-helper.exe'
    executable.write_bytes(b'not executable, never run')
    with pytest.raises(ValueError, match='Explicitly enable'):
        LocalHelperProvider(executable, sha256=sha256(executable.read_bytes()).hexdigest(), engine_name='dordiai-voices')
    with pytest.raises(ValueError, match='hash verification'):
        LocalHelperProvider(executable, sha256='0'*64, engine_name='dordiai-voices', enabled=True)


def test_piper_requires_hashed_existing_model_and_config(tmp_path):
    model = tmp_path / 'voice.onnx'
    config = tmp_path / 'voice.onnx.json'
    model.write_bytes(b'not a real neural model; never executed')
    config.write_bytes(b'{}')
    row = {'path': str(model), 'sha256': sha256(model.read_bytes()).hexdigest(),
           'config_sha256': sha256(config.read_bytes()).hexdigest(), 'name': 'Test manifest',
           'language': 'uk', 'license': 'Synthetic fixture, no voice model bundled'}
    provider = PiperProvider(sys.executable, [row])
    assert len(provider.voices()) == 1
    config.write_bytes(b'{"modified":true}')
    with pytest.raises(ValueError, match='hash verification'):
        PiperProvider(sys.executable, [row])


@pytest.mark.skipif(os.name != 'nt', reason='Actual SAPI requires native Windows.')
def test_native_windows_sapi_voice_and_pcm_output(tmp_path):
    registry = VoiceRegistry.discover(include_flite=False)
    voices = [voice for voice in registry.voices() if voice.id.startswith('sapi5-')]
    assert voices, 'Windows must expose at least one installed SAPI voice: ' + '; '.join(registry.notices)
    result = registry.synthesize('A real Windows source-preserving speech test. <voice>Literal text.</voice>',
                                 voices[0].id, tmp_path / 'native-голос.wav', settings=SpeechSettings(volume=50))
    assert result.frames > result.sample_rate
    assert result.path.is_file()
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
