"""Both retained stores are visible without workspace-ID crossrouting."""
from hashlib import sha256
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json
from carr_library.operations import OperationFacade, ResourceStateError
from carr_library.transcription import TranscriptionService


def test_same_job_basename_in_both_protocols_has_distinct_scope_bound_operation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected library')
    other = app.create_workspace('Other library')
    source = tmp_path / 'original.wav'
    source.write_bytes(b'Private retained source; never displayed by the dashboard.')
    strict = TranscriptionService(app, workspace)
    job = strict.get(strict.submit([source]))
    scope = sha256(workspace.encode()).hexdigest()
    key = scope + '.' + job['id']
    atomic_json(app.root / 'state/transcription/jobs' / job['id'] / 'job.json',
        {'schema': 'resonance-transcription-job/1', 'id': job['id'],
         'status': 'interrupted', 'done': 0, 'total': 1, 'updated': 1000})
    controls = Mock()
    facade = OperationFacade(app, transcription=controls)
    views = facade.list(workspace=workspace)
    assert {row.key for row in views} == {'asr:' + job['id'], 'asr-receipts:' + key}
    scoped = facade.get('asr-receipts', key, workspace=workspace)
    assert (scoped.workspace, scoped.status, scoped.total, scoped.controls) == (workspace, 'ready', 1, ())
    assert all('Private retained' not in repr(row) for row in views)
    with pytest.raises(PermissionError):
        facade.get('asr-receipts', key, workspace=other)
    with pytest.raises(ValueError):
        facade.get('asr', key)
    with pytest.raises(ValueError, match='does not expose'):
        facade.control('asr-receipts', key, 'cancel', workspace=workspace)
    controls.cancel.assert_not_called()
    facade.control('asr', job['id'], 'cancel')
    controls.cancel.assert_called_once_with(job['id'])
    assert source.read_bytes().startswith(b'Private retained')
    assert strict.get(job['id']) == job


def test_workspace_digest_tampering_and_missing_scope_never_retarget_receipt_jobs(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected')
    other = app.create_workspace('Foreign')
    source = tmp_path / 'input.wav'
    source.write_bytes(b'Synthetic original')
    strict = TranscriptionService(app, workspace)
    job = strict.get(strict.submit([source]))
    key = sha256(workspace.encode()).hexdigest() + '.' + job['id']
    facade = OperationFacade(app)
    with pytest.raises(ValueError):
        facade.get('asr-receipts', job['id'])
    job['workspace'] = other
    atomic_json(strict._path(job['id']) / 'job.json', job, replace=True)
    with pytest.raises(ResourceStateError, match='another workspace'):
        facade.get('asr-receipts', key, workspace=workspace)


def test_receipt_discovery_bounds_nonmatching_empty_directories(tmp_path, monkeypatch):
    from carr_library.operations import facade as module
    app = LibraryApp(tmp_path / 'library')
    base = app.root / 'state/transcription'
    base.mkdir(parents=True)
    for name in ('unknown-one', 'unknown-two', 'unknown-three'):
        (base / name).mkdir()
    monkeypatch.setattr(module, '_MAX_DISCOVERY', 2)
    with pytest.raises(ResourceStateError, match='discovery limit'):
        OperationFacade(app).list(limit=2)
