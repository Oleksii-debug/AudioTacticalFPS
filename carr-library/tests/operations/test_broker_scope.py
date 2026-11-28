"""Real directory and native-descriptor lifetime boundaries for resource scopes."""
from pathlib import Path
import threading

import pytest

from carr_library.audio.cleanup import pending_cleanup
from carr_library.audio.models import AudioCleanupPending
import carr_library.operations.broker as broker_module
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceStateError
from carr_library.operations._locks import NativeLock


SCOPE_ERROR = 'selected resource directory changed'


def directory_bytes(path):
    return tuple(
        (entry.relative_to(path).as_posix(), 'directory' if entry.is_dir() else 'file',
         None if entry.is_dir() else entry.read_bytes())
        for entry in sorted(path.rglob('*'))
    )


def replace_directory(path, destination):
    policy = path / 'resources.json'
    saved_policy = policy.read_bytes() if policy.is_file() else None
    path.rename(destination)
    path.mkdir()
    (path / 'unrelated-marker.bin').write_bytes(b'Preserve this replacement directory exactly.')
    if saved_policy is not None:
        # Equal policy bytes must not confer the displaced directory's identity.
        (path / 'resources.json').write_bytes(saved_policy)
    return directory_bytes(path)


@pytest.fixture
def bound(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    return root, ResourceBroker.for_app(root, limits=ResourceLimits(cpu_slots=2))


@pytest.mark.parametrize('scope', ['application', 'coordinator', 'leases', 'policy'])
@pytest.mark.parametrize('action', ['snapshot', 'reserve', 'configure', 'limits'])
def test_stale_broker_refuses_replacement_before_any_new_coordination(bound, tmp_path, scope, action):
    root, broker = bound
    selected = {
        'application': root,
        'coordinator': broker.root,
        'leases': broker.leases_root,
        'policy': broker.policy_path.parent,
    }[scope]
    moved = tmp_path / 'displaced-owned-directory'
    before = replace_directory(selected, moved)
    with pytest.raises(ResourceStateError, match=SCOPE_ERROR):
        if action == 'snapshot':
            broker.snapshot(timeout=0)
        elif action == 'reserve':
            with broker.reserve(ResourceRequest(), operation_id='must-not-start', timeout=0):
                pytest.fail('A displaced resource scope admitted work.')
        elif action == 'configure':
            broker.configure(ResourceLimits(cpu_slots=1), timeout=0)
        else:
            broker.limits
    assert moved.is_dir()
    assert directory_bytes(selected) == before


def assert_descriptor_released(lease):
    assert lease._released and not lease._lock.held and lease.cleanup_pending
    assert lease._lock.lock._context.lock_file_fd is None
    # This is a fresh actual native acquisition, not a PID or elapsed-time test.
    contender = NativeLock(lease._lock.path)
    assert contender.acquire()
    contender.release()
    lease.release()  # Explicit repeated release is inert after the real close.


def contains_error(error, kind):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
    return False


def test_live_release_closes_only_owned_descriptor_and_preserves_replacement(bound, tmp_path):
    _, broker = bound
    selected = broker.policy_path.parent
    lease = None
    with pytest.raises(ResourceStateError, match=SCOPE_ERROR):
        with broker.reserve(ResourceRequest(), operation_id='displaced-live-lease', timeout=0) as lease:
            record_path = broker.leases_root / (lease.id + '.json')
            original_record = record_path.read_bytes()
            before = replace_directory(selected, tmp_path / 'displaced-policy')
            with pytest.raises(ResourceStateError, match=SCOPE_ERROR):
                lease.checkpoint()
    assert lease is not None
    assert_descriptor_released(lease)
    assert record_path.read_bytes() == original_record
    assert directory_bytes(selected) == before


def test_scope_changed_by_gate_callback_cannot_create_a_new_coordinator(bound, tmp_path):
    _, broker = bound
    changed = []
    def cancelled():
        if not changed:
            changed.append(replace_directory(broker.root, tmp_path / 'displaced-coordinator'))
        return False
    with pytest.raises(ResourceStateError, match=SCOPE_ERROR):
        broker.snapshot(timeout=0, cancelled=cancelled)
    assert len(changed) == 1
    assert directory_bytes(broker.root) == changed[0]


def test_scope_change_after_lease_publication_releases_before_unstarted_work(bound, tmp_path, monkeypatch):
    _, broker = bound
    original = broker_module.ResourceLease
    captured = []
    def create(*args, **kwargs):
        lease = original(*args, **kwargs)
        record_path = broker.leases_root / (lease.id + '.json')
        retained = record_path.read_bytes()
        before = replace_directory(broker.policy_path.parent, tmp_path / 'displaced-after-publication')
        captured.append((lease, record_path, retained, before))
        return lease
    monkeypatch.setattr(broker_module, 'ResourceLease', create)
    with pytest.raises(ResourceStateError, match=SCOPE_ERROR):
        with broker.reserve(ResourceRequest(), operation_id='gate-exit-after-publication', timeout=0):
            pytest.fail('Work started after its admission scope was revoked.')
    assert len(captured) == 1
    lease, record_path, retained, before = captured[0]
    assert_descriptor_released(lease)
    assert record_path.read_bytes() == retained
    assert directory_bytes(broker.policy_path.parent) == before


def test_pending_cleanup_retains_native_lease_after_scope_revocation_until_quiescent(
        bound, tmp_path, monkeypatch):
    _, broker = bound
    pending, settling = threading.Event(), threading.Event()
    revoked, retried_after_revocation, finish = threading.Event(), threading.Event(), threading.Event()
    observed, failures = {}, []
    actual_start = threading.Thread.start
    def start(thread):
        if thread.name == 'audio-cleanup':
            raise RuntimeError('Synthetic cleanup-thread admission failure')
        return actual_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)

    def cleanup():
        settling.set()
        if revoked.is_set():
            retried_after_revocation.set()
        return finish.is_set()

    def work():
        try:
            with broker.reserve(ResourceRequest(), operation_id='retained-owner-with-revoked-scope',
                                timeout=0) as lease:
                observed['lease'] = lease
                record_path = broker.leases_root / (lease.id + '.json')
                observed['record_path'] = record_path
                observed['record'] = record_path.read_bytes()
                error = pending_cleanup(cleanup, message='Synthetic owned family behind a cleanup barrier')
                observed['owner'] = error.cleanup_owner
                pending.set()
                raise error
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=work, name='resource-scope-owned-worker')
    worker.start()
    try:
        assert pending.wait(5) and settling.wait(5)
        lease, owner = observed['lease'], observed['owner']
        before = replace_directory(broker.policy_path.parent, tmp_path / 'displaced-during-cleanup')
        revoked.set()
        assert retried_after_revocation.wait(5)
        assert worker.is_alive() and not failures
        assert not owner.quiescent_event.is_set() and lease._lock.held
        contender = NativeLock(lease._lock.path)
        acquired = contender.acquire()
        try:
            assert not acquired, 'A pending native owner lost its real reservation.'
        finally:
            if acquired:
                contender.release()
        assert directory_bytes(broker.policy_path.parent) == before
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and len(failures) == 1
    assert isinstance(failures[0], ResourceStateError)
    assert SCOPE_ERROR in str(failures[0])
    assert contains_error(failures[0], AudioCleanupPending)
    assert owner.quiescent_event.is_set()
    assert_descriptor_released(lease)
    assert observed['record_path'].read_bytes() == observed['record']
    assert directory_bytes(broker.policy_path.parent) == before
