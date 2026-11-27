"""Real local render receipts distinguish late stop requests from failures.

Only publication/verification boundaries are wrapped to inject failures or stops.
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


def test_completed_replay_requires_full_inventory_and_owner(tmp_path, voices, monkeypatch):
    from copy import deepcopy
    from carr_library.durable_state import atomic_json, read_json
    service, job = production(tmp_path, voices)
    complete = service.run(job)
    output = Path(complete['destination'])
    original = {row['path']: row['sha256'] for row in complete['artifacts']}
    receipt = service.jobs_root / job / 'job.json'
    marker = output / 'production-owner.json'
    owner = read_json(marker)

    def forbid_render():
        pytest.fail('Committed replay must not invoke the audio runtime.')

    monkeypatch.setattr(service, '_runtime', forbid_render)
    assert service.run(job) == complete
    for missing in ('master.wav', 'script.vtt', 'script.srt', 'timeline.json', 'project.json', 'chapter'):
        damaged = deepcopy(complete)
        damaged['artifacts'] = [
            row for row in damaged['artifacts']
            if (Path(row['path']).parent.name != 'chapters' if missing == 'chapter'
                else Path(row['path']).name != missing)
        ]
        assert len(damaged['artifacts']) < len(complete['artifacts'])
        atomic_json(receipt, damaged, replace=True)
        with pytest.raises(ValueError, match='missing required artifacts'):
            service.run(job)
        assert read_json(receipt) == damaged

    damaged = deepcopy(complete)
    damaged['artifacts'] = []
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='missing required artifacts'):
        service.run(job)

    damaged = deepcopy(complete)
    damaged['artifacts'].append(damaged['artifacts'][0])
    atomic_json(receipt, damaged, replace=True)
    with pytest.raises(ValueError, match='Duplicate production artifact'):
        service.run(job)

    atomic_json(receipt, complete, replace=True)
    atomic_json(marker, {**owner, 'job': 'another-job'}, replace=True)
    with pytest.raises(ValueError, match='owner receipt changed'):
        service.run(job)
    atomic_json(marker, owner, replace=True)
    assert service.run(job) == complete
    assert {name: sha256(Path(name).read_bytes()).hexdigest() for name in original} == original


@pytest.mark.parametrize('stop_kind', ['pause', 'cancel', 'predicate'])
def test_late_stop_does_not_hide_error_after_verified_outputs(tmp_path, voices, monkeypatch, stop_kind):
    service, job = production(tmp_path, voices)
    verify = service._verify_artifacts
    stopping, accepted = [False], {}

    def verify_then_fail(state, *, cancelled=lambda: False):
        verify(state, cancelled=cancelled)
        assert state['status'] == 'complete' and state['artifacts']
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

    monkeypatch.setattr(service, '_verify_artifacts', verify_then_fail)
    with pytest.raises(OSError, match='final receipt publication'):
        service.run(job, cancelled=lambda: stopping[0])
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

def test_stop_during_staged_master_validation_prevents_publication_and_resumes(tmp_path, voices, monkeypatch):
    from carr_library.studio import render
    service, job = production(tmp_path, voices)
    output = Path(service.get(job)['destination'])
    digest = render.digest_file
    stopping = False
    observed = []

    def digest_then_stop(path, *args, **kwargs):
        nonlocal stopping
        result = digest(path, *args, **kwargs)
        path = Path(path)
        if path.parent == output and path.name.startswith('.pending-') and not observed:
            observed.append(path)
            stopping = True
        return result

    monkeypatch.setattr(render, 'digest_file', digest_then_stop)
    with pytest.raises(InterruptedError, match='completed chunks are retained'):
        service.run(job, cancelled=lambda: stopping)
    assert len(observed) == 1
    saved = service.get(job)
    assert saved['status'] == 'paused'
    assert any(Path(row['path']) == output / 'master.wav' for row in saved['artifacts'])
    assert not (output / 'master.wav').exists()
    assert not observed[0].exists()

    stopping = False
    completed = service.run(job, cancelled=lambda: stopping)
    assert completed['status'] == 'complete'
    service._verify_artifacts(completed)
    assert (output / 'master.wav').is_file()
