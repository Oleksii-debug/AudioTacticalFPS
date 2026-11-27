"""Real native cross-process resource coordination; no model or device inference."""
from dataclasses import replace
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

# A real file entrypoint gives spawn an importable __main__, even when pytest
# itself runs through isolated stdin and collects tests with importlib. Bind
# only the package already imported by the parent; never add checkout/src or
# alter sys.path to make an installed-package check pass.
if __name__ in ('__main__', '__mp_main__'):
    if len(sys.argv) != 6 or sys.argv[1] != '--resource-process-scenario':
        raise SystemExit('Use the explicit resource process fixture entrypoint.')
    package_file = Path(sys.argv[4]).resolve(strict=True)
    package_spec = importlib.util.spec_from_file_location('carr_library', package_file,
        submodule_search_locations=[str(package_file.parent)])
    package = importlib.util.module_from_spec(package_spec)
    sys.modules['carr_library'] = package
    package_spec.loader.exec_module(package)

import pytest

import carr_library
from carr_library.durable_state import atomic_json
from carr_library.operations import (ResourceBroker, ResourceLimits, ResourceRequest,
    ResourceCancelled, ResourceConflict, ResourceError, ResourceStateError,
    ResourceTimeout, ResourceUnavailable)

if __name__ in ('__main__', '__mp_main__'):
    assert Path(carr_library.__file__).resolve() == Path(sys.argv[4]).resolve()
    assert Path(sys.modules[ResourceBroker.__module__].__file__).resolve() == Path(sys.argv[5]).resolve()


def _standalone_process_case(scenario, directory):
    product = Path(carr_library.__file__).resolve()
    broker_module = Path(sys.modules[ResourceBroker.__module__].__file__).resolve()
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8',
        str(Path(__file__).resolve()), '--resource-process-scenario', scenario,
        str(directory), str(product), str(broker_module)], capture_output=True,
        text=True, encoding='utf-8', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def _hold(directory, request, queue, stop, *, crash=False):
    try:
        broker = ResourceBroker(directory)
        with broker.reserve(ResourceRequest.from_dict(request), operation_id='child', timeout=2) as lease:
            queue.put(('held', lease.id))
            if not stop.wait(10):
                raise TimeoutError('Parent did not stop fixture process.')
            if crash:
                os._exit(73)
        queue.put(('released', None))
    except BaseException as exc:
        queue.put(('error', type(exc).__name__))
        raise


def _contend(directory, label, queue, start):
    broker = ResourceBroker(directory)
    if not start.wait(10):
        raise TimeoutError('Parent did not start fixture process.')
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id=label, timeout=5):
        queue.put((label, 'entered', time.monotonic()))
        time.sleep(.12)
        queue.put((label, 'exiting', time.monotonic()))


@pytest.fixture
def broker(tmp_path):
    return ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(cpu_slots=2,
        memory_bytes=1000, disk_bytes=1024 * 1024, min_free_disk_bytes=0))


def test_declared_cpu_memory_capacity_and_release_on_exception(broker):
    with pytest.raises(RuntimeError, match='worker failed'):
        with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=800), operation_id='first', timeout=0):
            with pytest.raises(ResourceTimeout, match='memory_bytes'):
                with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=201), operation_id='second', timeout=0):
                    pytest.fail('Over-budget worker started.')
            assert broker.snapshot()['usage'] == {'cpu_slots': 1, 'memory_bytes': 800, 'disk_bytes': 0}
            raise RuntimeError('worker failed')
    assert broker.snapshot()['leases'] == []


def test_request_above_policy_never_waits_or_starts(broker):
    with pytest.raises(ResourceUnavailable, match='cpu_slots'):
        with broker.reserve(ResourceRequest(cpu_slots=3), operation_id='too-big', timeout=60):
            pytest.fail('Impossible worker started.')
    assert broker.snapshot()['leases'] == []


def test_exclusive_configured_gpus_and_device_ids(broker):
    request = ResourceRequest(cpu_slots=0, gpu_device='cuda:0', gpu_memory_bytes=400)
    with pytest.raises(ResourceUnavailable, match='explicit configured capacity'):
        with broker.reserve(request, operation_id='gpu', timeout=0):
            pytest.fail('Unconfigured GPU was admitted.')
    broker.configure(replace(broker.limits, gpu_devices=(('cuda:0', 500), ('cuda:1', 600))))
    with broker.reserve(request, operation_id='gpu-one', timeout=0):
        with pytest.raises(ResourceTimeout, match='exclusive GPU'):
            with broker.reserve(replace(request, gpu_memory_bytes=1), operation_id='gpu-two', timeout=0):
                pytest.fail('Same GPU used concurrently.')
        with broker.reserve(replace(request, gpu_device='cuda:1'), operation_id='other-gpu', timeout=0):
            assert broker.snapshot()['gpu_devices_reserved'] == ['cuda:0', 'cuda:1']
    microphone = ResourceRequest(cpu_slots=0, devices=('microphone:fixture',))
    with broker.reserve(microphone, operation_id='capture', timeout=0):
        with pytest.raises(ResourceTimeout, match='exclusive audio'):
            with broker.reserve(microphone, operation_id='capture-two', timeout=0):
                pytest.fail('Same capture device used concurrently.')


def test_real_free_disk_and_same_volume_commitments(tmp_path):
    other = tmp_path / 'second-output'
    other.mkdir()
    free = shutil.disk_usage(tmp_path).free
    broker = ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(
        cpu_slots=1, disk_bytes=free * 2, min_free_disk_bytes=0))
    request = ResourceRequest(cpu_slots=0, disk_bytes=free * 3 // 5, disk_path=tmp_path)
    with broker.reserve(request, operation_id='disk-one', timeout=0):
        with pytest.raises(ResourceTimeout, match='actual free disk'):
            with broker.reserve(replace(request, disk_path=other), operation_id='disk-two', timeout=0):
                pytest.fail('Two paths on one volume overcommitted free space.')
    broker.configure(replace(broker.limits, min_free_disk_bytes=shutil.disk_usage(tmp_path).free + 1024 * 1024))
    with pytest.raises(ResourceTimeout, match='actual free disk'):
        with broker.reserve(ResourceRequest(disk_bytes=1, disk_path=tmp_path), operation_id='low-disk', timeout=0):
            pytest.fail('The required free space floor was ignored.')


def test_multiple_output_directories_are_admitted_as_one_atomic_request(broker, tmp_path):
    cache, output = tmp_path / 'cache', tmp_path / 'output'
    cache.mkdir()
    output.mkdir()
    request = ResourceRequest(cpu_slots=1, disk_bytes=100, disk_path=cache,
                              additional_disks=((output, 200),))
    with broker.reserve(request, operation_id='combined', timeout=0):
        state = broker.snapshot()
        assert state['usage']['disk_bytes'] == 300
        assert len(state['leases']) == 1
        assert len(state['leases'][0]['disk_volumes']) == 2
        with pytest.raises(ResourceUnavailable, match='disk_bytes'):
            with broker.reserve(replace(request, additional_disks=((output, broker.limits.disk_bytes),)),
                                operation_id='cannot-fit', timeout=0):
                pytest.fail('Partial reservation started.')
        assert len(broker.snapshot()['leases']) == 1


def test_waiting_cancellation_is_prompt_and_adds_no_reservation(broker):
    entered, stop = threading.Event(), threading.Event()
    failures = []
    def cancelled():
        entered.set()
        return stop.is_set()
    def waiting():
        try:
            with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='waiting', cancelled=cancelled, timeout=5):
                failures.append('started')
        except ResourceCancelled:
            failures.append('cancelled')
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
        worker = threading.Thread(target=waiting)
        worker.start()
        assert entered.wait(2)
        stop.set()
        worker.join(2)
        assert not worker.is_alive() and failures == ['cancelled']
        assert len(broker.snapshot()['leases']) == 1


def test_active_timeout_is_stop_signal_never_forced_reclaim(broker):
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='timed', timeout=0, run_timeout=.01) as lease:
        time.sleep(.02)
        assert lease.cancelled()
        with pytest.raises(ResourceTimeout, match='execution time'):
            lease.checkpoint()
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(), operation_id='still-blocked', timeout=0):
                pytest.fail('A live timed-out worker lost its reservation before cleanup.')
    assert not broker.snapshot()['leases']


def test_policy_persistence_cannot_invalidate_live_work(broker):
    with broker.reserve(ResourceRequest(cpu_slots=2, memory_bytes=800), operation_id='active', timeout=0):
        with pytest.raises(ResourceConflict, match='invalidate'):
            broker.configure(replace(broker.limits, cpu_slots=1))
        with pytest.raises(ResourceConflict, match='already holds'):
            with broker.reserve(ResourceRequest(), operation_id='active', timeout=0):
                pytest.fail('Duplicate operation started.')
    broker.configure(replace(broker.limits, cpu_slots=1))
    assert ResourceBroker(broker.root).limits.cpu_slots == 1
    with pytest.raises(ResourceConflict, match='different resource policy'):
        ResourceBroker(broker.root, limits=ResourceLimits(cpu_slots=2))


def test_one_runtime_coordinator_cannot_bind_two_policies(broker, tmp_path):
    alternate = tmp_path / 'alternate-policy.json'
    with pytest.raises(ResourceConflict, match='another policy path'):
        ResourceBroker(broker.root, policy_path=alternate)
    assert not alternate.exists()


def test_capacity_freed_after_deadline_does_not_start_late_worker(broker, monkeypatch):
    import carr_library.operations.broker as module
    holder = broker.reserve(ResourceRequest(cpu_slots=2), operation_id='holder', timeout=0)
    holder.__enter__()
    clock, released = [0.0], []
    def delayed_release(seconds):
        assert 0 < seconds <= .01
        # Native lock and checkpoint reads may already exceed 10 ms on Windows.
        # Hold only the broker's scheduling clock still until its first blocked
        # wait, then free real capacity strictly after the waiting deadline.
        clock[0] += seconds + .02
        holder.__exit__(None, None, None)
        released.append(True)
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=delayed_release, time=time.time))
    try:
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(), operation_id='too-late', timeout=.01):
                pytest.fail('A worker was admitted after its finite waiting deadline.')
        assert released == [True] and clock[0] > .01
        assert not broker.snapshot()['leases']
    finally:
        if not released:
            holder.__exit__(None, None, None)


def test_actual_spawned_process_crash_releases_without_pid_reaping(broker):
    if __name__ != '__main__':
        _standalone_process_case('crash', broker.root)
        return
    ctx = multiprocessing.get_context('spawn')
    queue, stop = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_hold, args=(broker.root, ResourceRequest(cpu_slots=2).to_dict(), queue, stop),
                          kwargs={'crash': True})
    process.start()
    try:
        state, identity = queue.get(timeout=5)
        assert state == 'held'
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(), operation_id='parent', timeout=0):
                pytest.fail('A live process reservation was ignored.')
        stop.set()
        process.join(5)
        assert process.exitcode == 73
        path = broker.leases_root / (identity + '.json')
        stale = json.loads(path.read_text())
        # Simulate a reused PID belonging to this live test process. The native
        # unlocked lease, not the PID number, must determine recovery.
        stale['owner_pid'] = os.getpid()
        atomic_json(path, stale, replace=True)
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='after-crash', timeout=1):
            assert len(broker.snapshot()['leases']) == 1
        assert not path.exists()
        assert not path.with_suffix('.lock').exists()
        assert list(broker.leases_root.iterdir()) == []
    finally:
        stop.set()
        if process.is_alive():
            process.terminate()
        process.join(5)
        queue.close()


def test_actual_process_contention_has_no_overlapping_admission(tmp_path):
    if __name__ != '__main__':
        _standalone_process_case('contention', tmp_path)
        return
    broker = ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(cpu_slots=1))
    ctx = multiprocessing.get_context('spawn')
    queue, start = ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_contend, args=(broker.root, label, queue, start)) for label in ('one', 'two')]
    for process in workers:
        process.start()
    try:
        start.set()
        events = [queue.get(timeout=10) for _ in range(4)]
        times = {label: {event: stamp for who, event, stamp in events if who == label} for label in ('one', 'two')}
        first, second = sorted(times.values(), key=lambda value: value['entered'])
        assert first['exiting'] <= second['entered']
        for process in workers:
            process.join(5)
            assert process.exitcode == 0
        assert broker.snapshot()['leases'] == []
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
            process.join(5)
        queue.close()


def test_bad_live_metadata_stops_admission_and_unlock_recovers(broker):
    context = broker.reserve(ResourceRequest(), operation_id='active', timeout=0)
    lease = context.__enter__()
    path = broker.leases_root / (lease.id + '.json')
    path.write_text('{broken checkpoint', encoding='utf-8')
    with pytest.raises(ResourceStateError):
        broker.snapshot()
    with pytest.raises(ResourceStateError):
        context.__exit__(None, None, None)
    assert not broker.snapshot()['leases']


def test_failed_publication_releases_native_lock(broker, monkeypatch):
    import carr_library.operations.broker as module
    save = module.atomic_json
    def fail(path, value, **kwargs):
        if Path(path).parent == broker.leases_root:
            raise OSError('fixture publication failure')
        return save(path, value, **kwargs)
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError, match='publication failure'):
        with broker.reserve(ResourceRequest(), operation_id='failure', timeout=0):
            pytest.fail('Worker started without a saved reservation.')
    assert not broker.snapshot()['leases']


def test_runtime_leases_outside_backup_state_and_truthful_capabilities(tmp_path):
    broker = ResourceBroker.for_app(tmp_path)
    assert broker.root == tmp_path / 'runtime' / 'resources'
    assert broker.policy_path == tmp_path / 'state' / 'operations' / 'resources.json'
    facts = broker.capabilities()
    assert facts['pid_based_reaping'] is False and facts['network_sandbox'] is False
    assert facts['orphan_native_workers_managed'] is False
    assert 'no physical RAM/VRAM measurement' in facts['memory_policy']


def test_paths_do_not_follow_links_and_invalid_requests_are_rejected(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    linked = tmp_path / 'linked'
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        # Windows developer hosts may not grant symlink creation; hardlinks
        # below still exercise the regular-file boundary on those hosts.
        linked = None
    if linked is not None:
        with pytest.raises(ValueError):
            ResourceBroker(linked)
    broker = ResourceBroker(real)
    another = tmp_path / 'hardlink.json'
    os.link(broker.policy_path, another)
    with pytest.raises(ValueError):
        broker.snapshot()
    for values in ({'cpu_slots': True}, {'gpu_device': 'cuda:0'}, {'disk_bytes': 1},
                   {'devices': ('microphone:one', 'microphone:one')}, {'cpu_slots': 0}):
        with pytest.raises(ValueError):
            ResourceRequest(**values)




def test_many_completed_reservations_do_not_exhaust_inventory_or_unlink_a_live_owner(broker, monkeypatch):
    import carr_library.operations.broker as module
    from carr_library.operations._locks import NativeLock
    # Keep the production bound intact. This lower fixture bound exposes a
    # lifetime leak after only a few actual acquisitions instead of 8192.
    monkeypatch.setattr(module, '_MAX_RECORDS', 4)
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='held-owner', timeout=0) as owner:
        owner_path = broker.leases_root / (owner.id + '.lock')
        before = owner_path.stat()
        for index in range(32):
            with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='completed-' + str(index), timeout=0):
                assert len(broker.snapshot()['leases']) == 2
                assert len(list(broker.leases_root.iterdir())) == 4
            after = owner_path.stat()
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            contender = NativeLock(owner_path)
            assert contender.acquire() is False
            assert owner._lock.held
            assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['held-owner']
            assert len(list(broker.leases_root.iterdir())) == 2
    assert broker.snapshot()['leases'] == []
    assert list(broker.leases_root.iterdir()) == []


def test_replaced_released_lock_is_not_unlinked(broker, monkeypatch, tmp_path):
    from carr_library.operations._locks import NativeLock
    # Create a distinct inode while the original lease still exists, then
    # replace only its released path. This is not a concurrent live owner.
    replacement = tmp_path / 'replacement-lock'
    replacement.write_bytes(b'Unexpected replacement; preserve for inspection.')
    actual = NativeLock.release
    target = []
    def replaced(lock):
        actual(lock)
        if target and lock.path == target[0] and replacement.exists():
            os.replace(replacement, lock.path)
    with pytest.raises(ResourceStateError, match='replaced'):
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='replacement-guard', timeout=0) as lease:
            target.append(broker.leases_root / (lease.id + '.lock'))
            monkeypatch.setattr(NativeLock, 'release', replaced)
    assert target[0].read_bytes() == b'Unexpected replacement; preserve for inspection.'



if __name__ == '__main__':
    if sys.argv[2] == 'crash':
        test_actual_spawned_process_crash_releases_without_pid_reaping(ResourceBroker(Path(sys.argv[3])))
    elif sys.argv[2] == 'contention':
        test_actual_process_contention_has_no_overlapping_admission(Path(sys.argv[3]))
    else:
        raise SystemExit('Unknown resource process fixture scenario.')

@pytest.mark.parametrize('phase', ['construction', 'acquisition'])
def test_native_release_can_remove_the_file_during_unheld_preflight(tmp_path, monkeypatch, phase):
    """Release a real held native lock exactly before the waiter's path check."""
    from carr_library.operations import _locks
    path = tmp_path / 'contended.lock'
    holder = _locks.NativeLock(path)
    assert holder.acquire()
    waiter = _locks.NativeLock(path) if phase == 'acquisition' else None
    contender = None
    requested, released = threading.Event(), threading.Event()
    errors = []
    caller = threading.current_thread()
    actual_regular = _locks.regular
    reached = []

    def release_holder():
        try:
            if not requested.wait(10):
                raise AssertionError('The waiter did not reach its pre-acquisition boundary.')
            holder.release()
            assert not holder.held
            assert not os.path.lexists(path)
        except BaseException as exc:
            errors.append(exc)
        finally:
            released.set()

    def checked(candidate, *arguments, **keywords):
        if (Path(candidate) == path and threading.current_thread() is caller
                and not reached):
            reached.append(True)
            requested.set()
            assert released.wait(10), 'The actual native holder did not release.'
            assert not errors, errors
        return actual_regular(candidate, *arguments, **keywords)

    thread = threading.Thread(target=release_holder, name='resource-release-race')
    thread.start()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(_locks, 'regular', checked)
            if waiter is None:
                waiter = _locks.NativeLock(path)
            assert waiter.acquire()
            assert reached == [True] and released.is_set() and not errors
            assert waiter.held
            info = path.lstat()
            held = os.fstat(waiter.lock._context.lock_file_fd)
            assert (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
            contender = _locks.NativeLock(path)
            assert contender.acquire() is False
            assert waiter.held and not contender.held
            waiter.release()
            assert contender.acquire()
    finally:
        requested.set()
        thread.join(12)
        if contender is not None:
            contender.release()
        if waiter is not None:
            waiter.release()
        holder.release()
    assert not thread.is_alive() and not errors


@pytest.mark.parametrize('phase', ['construction', 'acquisition'])
def test_optional_lock_preflight_still_rejects_hardlinks_without_touching_bytes(tmp_path, phase):
    from carr_library.operations._locks import NativeLock
    outside = tmp_path / 'retained-original'
    original = b'This retained file must not become a mutable resource lock.'
    outside.write_bytes(original)
    path = tmp_path / 'untrusted.lock'
    lock = NativeLock(path) if phase == 'acquisition' else None
    os.link(outside, path)
    try:
        with pytest.raises(ValueError):
            if lock is None:
                NativeLock(path)
            else:
                lock.acquire()
        assert outside.read_bytes() == original and path.read_bytes() == original
        assert lock is None or not lock.held
    finally:
        if lock is not None:
            lock.release()

@pytest.mark.parametrize('phase', ['construction', 'acquisition'])
def test_lock_leaf_disappears_inside_local_path(tmp_path, monkeypatch, phase):
    """Exercise the earlier leaf lstat boundary from the Windows traceback."""
    from carr_library import durable_state
    from carr_library.operations._locks import NativeLock
    path = tmp_path / 'released.lock'
    lock = NativeLock(path) if phase == 'acquisition' else None
    path.write_bytes(b'')
    original = durable_state.regular
    removed = []
    def disappear(candidate, **kwargs):
        if Path(candidate) == path and not removed:
            path.unlink()
            removed.append(True)
        return original(candidate, **kwargs)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(durable_state, 'regular', disappear)
            if lock is None:
                lock = NativeLock(path)
            assert lock.acquire()
        assert removed == [True] and lock.held
        peer = NativeLock(path)
        assert not peer.acquire()
    finally:
        if lock is not None:
            lock.release()


@pytest.mark.parametrize('phase', ['construction', 'acquisition'])
@pytest.mark.parametrize('boundary', ['local_path', 'direct'])
def test_unheld_lock_can_observe_zero_links_during_release(tmp_path, monkeypatch, phase, boundary):
    """Reproduce the observed regular zero-link stat at either unheld check."""
    from carr_library import durable_state
    from carr_library.operations import _locks
    path = tmp_path / 'released-zero-links.lock'
    lock = _locks.NativeLock(path) if phase == 'acquisition' else None
    path.write_bytes(b'')
    module = durable_state if boundary == 'local_path' else _locks
    original = module.regular
    lstat = Path.lstat
    observed = []

    def released_stat(candidate, *args, **kwargs):
        value = lstat(candidate, *args, **kwargs)
        if candidate == path and not observed:
            path.unlink()
            observed.append(True)
            return SimpleNamespace(st_mode=value.st_mode, st_nlink=0,
                                   st_file_attributes=getattr(value, 'st_file_attributes', 0))
        return value

    def checked(candidate, **kwargs):
        if Path(candidate) == path and not observed:
            with monkeypatch.context() as scoped:
                scoped.setattr(Path, 'lstat', released_stat)
                return original(candidate, **kwargs)
        return original(candidate, **kwargs)

    peer = None
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(module, 'regular', checked)
            if lock is None:
                lock = _locks.NativeLock(path)
            assert lock.acquire()
        assert observed == [True] and lock.held
        peer = _locks.NativeLock(path)
        assert not peer.acquire()
        lock.release()
        assert peer.acquire()
    finally:
        if peer is not None:
            peer.release()
        if lock is not None:
            lock.release()


def test_zero_link_stat_remains_invalid_for_general_state(tmp_path, monkeypatch):
    from carr_library import durable_state
    path = tmp_path / 'state.json'
    path.write_bytes(b'{}')
    original = Path.lstat

    def stale(candidate, *args, **kwargs):
        value = original(candidate, *args, **kwargs)
        if candidate == path:
            return SimpleNamespace(st_mode=value.st_mode, st_nlink=0,
                                   st_file_attributes=getattr(value, 'st_file_attributes', 0))
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'lstat', stale)
        with pytest.raises(durable_state.UnlinkedRegularFile) as exc:
            durable_state.regular(path)
        assert isinstance(exc.value, ValueError) and exc.value.path == path
        with pytest.raises(ValueError):
            durable_state.local_path(path)
    assert path.read_bytes() == b'{}'
