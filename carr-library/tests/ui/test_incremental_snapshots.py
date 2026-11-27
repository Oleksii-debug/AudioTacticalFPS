"""Queue, captured-source and lifecycle contracts; separate native tests use real wx."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from carr_library.ui.incremental_backup import start_snapshot
from carr_library import incremental_backup as backend


def frame():
    return SimpleNamespace(_closing=False, working=False, library=object(),
                           cancel_event=threading.Event(), report=Mock(), run_job=Mock())


@pytest.mark.parametrize('action', ['create', 'list', 'verify', 'restore'])
def test_actions_only_queue_existing_backend_and_capture_source(action, monkeypatch):
    owner, completed, failed = frame(), Mock(), Mock()
    functions = {name: Mock(return_value=object()) for name in
                 ('create_snapshot', 'list_snapshots', 'verify_snapshot', 'restore_snapshot')}
    for name, function in functions.items():
        monkeypatch.setattr(backend, name, function)
    source = owner.library
    assert start_snapshot(owner, action, '/selected/repository', checksum='a' * 64,
                          destination='/selected/new-library', done=completed, failed=failed)
    assert all(function.call_count == 0 for function in functions.values())
    _, execute, ready, error = owner.run_job.call_args.args
    names = dict(create='create_snapshot', list='list_snapshots',
                 verify='verify_snapshot', restore='restore_snapshot')
    owner.library = object()
    result = execute()
    function = functions[names[action]]
    assert function.call_count == 1
    if action == 'create':
        assert function.call_args.args[0] is source
    assert function.call_args.kwargs['cancelled']() is False
    owner.cancel_event.set()
    assert function.call_args.kwargs['cancelled']() is True
    ready(result)
    error(ValueError('late failure'))
    completed.assert_not_called()
    failed.assert_not_called()


@pytest.mark.parametrize('state', ['working', '_closing'])
def test_busy_or_closing_frame_never_queues_snapshot_work(state):
    owner = frame()
    setattr(owner, state, True)
    assert start_snapshot(owner, 'create', '/selected/repository') is False
    owner.run_job.assert_not_called()


@pytest.mark.parametrize('action,repository,checksum,destination', [
    ('delete', '/selected/repository', '', ''),
    ('create', ' ', '', ''),
    ('verify', '/selected/repository', 'not-a-digest', ''),
    ('restore', '/selected/repository', 'a' * 64, ''),
])
def test_incomplete_or_unknown_requests_fail_before_queue(action, repository, checksum, destination):
    owner = frame()
    with pytest.raises(ValueError):
        start_snapshot(owner, action, repository, checksum=checksum, destination=destination)
    owner.run_job.assert_not_called()


def test_current_scope_receives_real_completion_and_failure_callbacks():
    owner, completed, failed = frame(), Mock(), Mock()
    start_snapshot(owner, 'list', '/selected/repository', done=completed, failed=failed)
    _, _, ready, error = owner.run_job.call_args.args
    result, exception = object(), ValueError('synthetic failure')
    ready(result)
    error(exception)
    completed.assert_called_once_with(result)
    failed.assert_called_once_with(exception)
    owner._closing = True
    ready(object())
    error(RuntimeError('late'))
    assert completed.call_count == failed.call_count == 1
