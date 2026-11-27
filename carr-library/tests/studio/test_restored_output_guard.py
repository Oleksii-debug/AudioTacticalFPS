"""Restored job destinations are evidence, never implicit output permission."""
from pathlib import Path

import pytest

from carr_library.durable_state import atomic_json
from carr_library.studio.render import ProductionService


class NoSpeech:
    def voices(self):
        return ()


@pytest.mark.parametrize('status', ['pending', 'running', 'paused', 'complete'])
def test_restored_job_rejects_before_runtime_or_old_artifact_access(tmp_path, monkeypatch, status):
    def forbidden(*args, **kwargs):
        raise AssertionError('Restored jobs must not access an audio runtime or historical output.')

    service = ProductionService(tmp_path / 'state', voices=NoSpeech(), runtime_factory=forbidden)
    destination = tmp_path / 'outputs'
    destination.mkdir()
    project = service.create('[Pause: 1]', mode='authored')
    job_id = service.submit(project, destination)
    state = service.get(job_id)
    state.update(status=status, restore_requires_new_output=True)
    state['artifacts'] = [{'path': str(Path(state['destination']) / 'master.wav'),
                           'sha256': '0' * 64}]
    path = service.jobs_root / job_id / 'job.json'
    atomic_json(path, state, replace=True)
    original = path.read_bytes()
    monkeypatch.setattr(service, '_verify_artifacts', forbidden)
    with pytest.raises(ValueError, match='new output'):
        service.run(job_id)
    assert path.read_bytes() == original
    assert service.get(job_id) == state
    assert service.jobs() == (state,)
    assert not list(destination.iterdir())
    # Explicit new submission retains the project while binding a fresh output.
    fresh = service.submit(project, destination)
    assert fresh != job_id
    assert service.get(fresh)['destination'] != state['destination']
    assert 'restore_requires_new_output' not in service.get(fresh)
