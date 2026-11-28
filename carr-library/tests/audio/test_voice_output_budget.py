"""Real child-process budget checks; these fixtures are not speech/model inference."""
import base64
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.audio import model_voices, voices


@pytest.mark.parametrize('runner', ['system', 'model'])
def test_fast_child_cannot_evade_post_exit_audio_budget(tmp_path, runner):
    output = tmp_path / 'private-output-fixture.wav'
    command = [sys.executable, '-I', '-c',
               'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"x"*8192)', str(output)]
    with pytest.raises((ValueError, RuntimeError), match='budget'):
        if runner == 'system':
            voices._run(command, tmp_path, timeout_seconds=5, output=output, max_output_bytes=4096)
        else:
            model_voices._run(command, timeout=5, output=output, max_output_bytes=4096)
    assert output.stat().st_size == 8192  # Private rejection fixture, never published as speech.


def test_streaming_model_protocol_checks_caller_budget_before_writing_pcm(tmp_path):
    # A fixed protocol fixture writes encoded zero samples. It is not a neural
    # worker and does not count as successful generation by any real model.
    encoded = base64.b64encode(bytes(8192)).decode('ascii')
    code = ('import json,sys; sys.stdin.readline(); '
            'print(json.dumps({"pcm":sys.argv[1]}),flush=True)')
    engine = object.__new__(model_voices.DordiaiVoicesEngine)
    engine.timeout = 5
    engine.manifest = SimpleNamespace(root=tmp_path)
    output = tmp_path / 'private-stream-fixture.wav'
    with pytest.raises(ValueError, match='PCM byte budget'):
        engine._stream([sys.executable, '-I', '-c', code, encoded], {'mode': 'fixture'}, output,
                       lambda: False, max_output_bytes=4096)
    assert output.stat().st_size <= 44


@pytest.mark.parametrize('budget', [True, 0, -1, 4095, '4096'])
def test_invalid_registry_budget_rejects_before_output_or_voice_discovery(tmp_path, budget):
    registry = voices.VoiceRegistry({'ffmpeg': False, 'espeak_ng': False, 'include_sapi': False})
    with pytest.raises(ValueError, match='budget'):
        registry.synthesize('Text.', tmp_path / 'never.wav', voice_id='flite:missing',
                            max_output_bytes=budget)
    assert not list(tmp_path.iterdir())
