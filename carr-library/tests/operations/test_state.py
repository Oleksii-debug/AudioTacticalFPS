"""Real resource publication; inject denial after the shared native fallback."""
from pathlib import Path

import pytest

from carr_library.operations import _state
from carr_library import durable_state


@pytest.mark.parametrize('code', [5, 32, 33])
def test_transient_windows_sharing_retries_the_same_flushed_stage(tmp_path, monkeypatch, code):
    target = tmp_path / 'policy.json'
    _state.atomic_json(target, {'value': 'old'})
    real_replace = _state.jobs._replace_state_file
    attempts, sleeps = [], []
    def replace(stage, destination):
        raw = Path(stage).read_bytes()
        attempts.append((Path(stage), raw, Path(stage).stat().st_ino))
        if len(attempts) <= 2:
            failure = PermissionError('Synthetic Windows sharing conflict')
            failure.winerror = code
            raise failure
        return real_replace(stage, destination)
    monkeypatch.setattr(_state.jobs, '_replace_state_file', replace)
    monkeypatch.setattr(durable_state.time, 'sleep', sleeps.append)
    _state.atomic_json(target, {'value': 'new'}, replace=True)
    assert _state.read_json(target) == {'value': 'new'}
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert sleeps == [.01, .02]
    assert not tuple(tmp_path.glob('.pending-*'))


def test_exhausted_sharing_retry_preserves_the_original_and_cleans_only_its_stage(tmp_path, monkeypatch):
    target = tmp_path / 'policy.json'
    _state.atomic_json(target, {'value': 'old'})
    original = target.read_bytes()
    calls, sleeps = [], []
    def fail(stage, destination):
        calls.append(Path(stage))
        failure = PermissionError('Synthetic persistent sharing conflict')
        failure.winerror = 32
        raise failure
    monkeypatch.setattr(_state.jobs, '_replace_state_file', fail)
    monkeypatch.setattr(durable_state.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError):
        _state.atomic_json(target, {'value': 'new'}, replace=True)
    assert len(calls) == 7 and len(set(calls)) == 1
    assert sleeps == [.01, .02, .04, .08, .16, .32]
    assert target.read_bytes() == original and not tuple(tmp_path.glob('.pending-*'))


def test_unrelated_publication_error_has_no_retry_or_destination_removal(tmp_path, monkeypatch):
    target = tmp_path / 'policy.json'
    _state.atomic_json(target, {'value': 'old'})
    original = target.read_bytes()
    calls = []
    def fail(stage, destination):
        calls.append(stage)
        raise PermissionError('Synthetic unrelated denial')
    monkeypatch.setattr(_state.jobs, '_replace_state_file', fail)
    monkeypatch.setattr(durable_state.time, 'sleep', lambda seconds: pytest.fail('Unrelated denial was retried.'))
    with pytest.raises(PermissionError):
        _state.atomic_json(target, {'value': 'new'}, replace=True)
    assert len(calls) == 1 and target.read_bytes() == original
    assert not tuple(tmp_path.glob('.pending-*'))


def test_create_only_policy_never_replaces_existing_or_linked_records(tmp_path):
    target = tmp_path / 'policy.json'
    _state.atomic_json(target, {'value': 'old'})
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        _state.atomic_json(target, {'value': 'new'})
    alias = tmp_path / 'alias.json'
    _state.os.link(target, alias)
    with pytest.raises(ValueError):
        _state.atomic_json(target, {'value': 'new'}, replace=True)
    with pytest.raises(ValueError):
        _state.read_json(target)
    assert target.read_bytes() == alias.read_bytes() == original
