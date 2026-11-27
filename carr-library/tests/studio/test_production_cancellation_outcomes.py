"""Real local render receipts distinguish late stop requests from failures.

Only the durable completion write is wrapped to inject a publication error.
Speech, mixing, outputs, and their hashes use the actual installed engines.
"""
from hashlib import sha256
from pathlib import Path
import shutil
import sys

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.speech import FliteProvider, NativeSapiProvider, VoiceRegistry
from carr_library.studio.render import ProductionService


@pytest.fixture(scope='module')
def voices():
    ffmpeg = shutil.which('ffmpeg')
    if sys.platform == 'win32':
        assert ffmpeg and shutil.which('ffprobe'), 'The strict Windows render gate requires FFmpeg and ffprobe.'
        registry = VoiceRegistry([NativeSapiProvider()])
        installed = registry.voices()
        assert installed, 'The strict Windows render gate requires installed native SAPI: ' + '; '.join(registry.notices)
        selected = next((voice for voice in installed if voice.language.casefold().startswith('en')), installed[0])
        return registry, selected.id
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Actual render verification requires installed FFmpeg and ffprobe.')
    registry = VoiceRegistry([FliteProvider(ffmpeg)])
    if 'flite:slt' not in {voice.id for voice in registry.voices()}:
        pytest.skip('Actual render verification requires the installed Flite slt voice.')
    return registry, 'flite:slt'


def production(tmp_path, voices):
    registry, selected_voice = voices
    service = ProductionService(tmp_path / 'state', voices=registry)
    project = service.create('Keep the saved audio.', voice_ids=(selected_voice,))
    output = tmp_path / 'output'
    output.mkdir()
    return service, service.submit(project, output, stems=False)


@pytest.mark.parametrize('stop_kind', ['pause', 'cancel', 'predicate'])
def test_late_stop_does_not_hide_error_after_verified_outputs(tmp_path, voices, monkeypatch, stop_kind):
    service, job = production(tmp_path, voices)
    verify, save = service._verify_artifacts, service._save
    stopping, accepted, completion_attempts = [False], {}, []

    def fail_completion_write(state):
        if state['status'] != 'complete':
            return save(state)
        # The actual verifier runs before completion and lease release.
        # Inject at the durable completion write after that verification.
        verify(state)
        assert state['status'] == 'complete' and state['artifacts']
        completion_attempts.append(state['id'])
        for artifact in state['artifacts']:
            path = Path(artifact['path'])
            accepted[str(path)] = (path.stat().st_size, sha256(path.read_bytes()).hexdigest())
        if stop_kind == 'pause':
            service.request_pause(job)
        elif stop_kind == 'cancel':
            service.cancel(job)
        else:
            stopping[0] = True
        raise OSError('Synthetic final receipt publication failure')

    monkeypatch.setattr(service, '_save', fail_completion_write)
    with pytest.raises(OSError, match='final receipt publication'):
        service.run(job, cancelled=lambda: stopping[0])
    assert completion_attempts == [job]
    saved = service.get(job)
    assert saved['status'] == 'failed'
    assert saved['error'] == 'Synthetic final receipt publication failure'
    assert saved['artifacts'] and accepted
    for filename, before in accepted.items():
        path = Path(filename)
        assert (path.stat().st_size, sha256(path.read_bytes()).hexdigest()) == before
    verify(saved)  # The genuine error does not delete or rewrite accepted files.


@pytest.mark.parametrize(('stop_kind', 'expected'), [('pause', 'paused'), ('cancel', 'cancelled')])
def test_actual_dsp_cancellation_retains_typed_paused_or_cancelled_receipt(tmp_path, voices, stop_kind, expected):
    service, job = production(tmp_path, voices)
    requested = []

    def before_meter(message):
        if message == 'Measuring the actual completed master':
            requested.append(message)
            if stop_kind == 'pause':
                service.request_pause(job)
            else:
                service.cancel(job)

    with pytest.raises(AudioCancelled):
        service.run(job, progress=before_meter)
    assert len(requested) == 1
    saved = service.get(job)
    assert saved['status'] == expected
    assert saved['artifacts']
    service._verify_artifacts(saved)
