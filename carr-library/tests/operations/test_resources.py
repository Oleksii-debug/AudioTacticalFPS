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
from carr_library.operations._state import atomic_json
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


@pytest.mark.parametrize('elapsed', [.01, .02])
def test_active_timeout_is_stop_signal_never_forced_reclaim(broker, monkeypatch, elapsed):
    import carr_library.operations.broker as module
    clock = [0.0]
    # Windows native metadata/lock work can consume the old 10 ms allowance
    # before context entry. Advance only the scheduling clock after admission;
    # retain actual files, OS locks and the competing reservation below.
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=time.sleep, time=time.time))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='timed', timeout=0, run_timeout=.01) as lease:
        assert not lease.cancelled()
        assert [row['id'] for row in broker.snapshot()['leases']] == [lease.id]
        clock[0] = elapsed
        assert lease.cancelled()
        with pytest.raises(ResourceTimeout, match='execution time'):
            lease.checkpoint()
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(), operation_id='still-blocked', timeout=0):
                pytest.fail('A live timed-out worker lost its reservation before cleanup.')
        assert [row['id'] for row in broker.snapshot()['leases']] == [lease.id]
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


@pytest.mark.parametrize('boundary', ('construction', 'acquisition'))
def test_unowned_lock_resolves_parent_while_released_leaf_disappears(tmp_path, monkeypatch, boundary):
    from carr_library.operations import _locks
    parent = tmp_path.resolve(strict=True)
    path = parent / '.vanishing.lock'
    path.write_bytes(b'')
    existing = _locks.NativeLock(path) if boundary == 'acquisition' else None
    removed, lookups = [], []

    def release_after_lookup(selected):
        selected = Path(selected)
        lookups.append(selected)
        if selected in (path, parent) and not removed:
            path.unlink()
            removed.append(selected)

    if os.name == 'nt':
        import ntpath
        original = ntpath._getfinalpathname

        def native_lookup(selected):
            result = original(selected)
            # In the old code the leaf resolves once, then disappears before
            # CPython's second prefix-stripping lookup. In the corrected code
            # only the existing parent is resolved, so its second lookup is
            # still valid while the same real lock leaf disappears.
            release_after_lookup(selected)
            return result

        monkeypatch.setattr(ntpath, '_getfinalpathname', native_lookup)
    else:
        original = Path.resolve

        def resolve_parent(selected, *args, **kwargs):
            assert Path(selected) != path, 'An unowned lock leaf must not be resolved.'
            result = original(selected, *args, **kwargs)
            release_after_lookup(selected)
            return result

        monkeypatch.setattr(Path, 'resolve', resolve_parent)

    lock = None
    try:
        lock = existing or _locks.NativeLock(path)
        assert lock.acquire() is True and lock.held
        assert removed == [parent]
        assert path not in lookups
        info, held = path.stat(), os.fstat(lock.lock._context.lock_file_fd)
        assert (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
    finally:
        if lock is not None:
            lock.release()


@pytest.mark.parametrize('name', ('NUL', 'gate.lock:stream', 'gate.lock.'))
def test_absent_unowned_lock_leaf_keeps_portable_filename_refusals(tmp_path, name):
    from carr_library.operations import _locks
    with pytest.raises(ValueError, match='local filename'):
        _locks.NativeLock(tmp_path / name)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('boundary', ('construction', 'acquisition'))
def test_released_lock_leaf_may_disappear_before_native_acquisition(tmp_path, monkeypatch, boundary):
    from carr_library.operations import _locks
    path = tmp_path / '.released.lock'
    path.write_bytes(b'')
    existing = _locks.NativeLock(path) if boundary == 'acquisition' else None
    original, removed = _locks.regular, []

    def release_cleanup(selected):
        if Path(selected) == path and not removed:
            # WindowsFileLock removes its unowned leaf after releasing the
            # native lock. Interleave that real filesystem change precisely
            # between the next owner's presence and metadata observations.
            path.unlink()
            removed.append(True)
        return original(selected)

    monkeypatch.setattr(_locks, 'regular', release_cleanup)
    lock = existing or _locks.NativeLock(path)
    try:
        assert lock.acquire() is True and lock.held
        assert removed == [True]
        info, held = path.stat(), os.fstat(lock.lock._context.lock_file_fd)
        assert (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
    finally:
        lock.release()



def _release_during_local_path_validation(monkeypatch, path, replacement, *, repetitions=1):
    """Reproduce a real missing metadata read, then change the unowned leaf."""
    from carr_library.operations import _locks
    original, changed = _locks.regular, []

    def observed(selected, *, directory=False):
        if Path(selected) == path and len(changed) < repetitions:
            changed.append(True)
            path.unlink()
            try:
                return original(selected, directory=directory)
            except FileNotFoundError:
                replacement()
                raise
        return original(selected, directory=directory)

    monkeypatch.setattr(_locks, 'regular', observed)
    return changed


@pytest.mark.parametrize('boundary', ('construction', 'acquisition'))
@pytest.mark.parametrize('repetitions', (1, 2))
def test_released_lock_leaf_recreated_before_missing_observation_recovers(tmp_path, monkeypatch, boundary, repetitions):
    from carr_library.operations import _locks
    path = tmp_path / '.recreated.lock'
    path.write_bytes(b'')
    existing = _locks.NativeLock(path) if boundary == 'acquisition' else None
    changed = _release_during_local_path_validation(
        monkeypatch, path, lambda: path.write_bytes(b''), repetitions=repetitions)
    lock = existing or _locks.NativeLock(path)
    try:
        assert lock.acquire() is True and lock.held
        assert changed == [True] * repetitions
        info, held = path.stat(), os.fstat(lock.lock._context.lock_file_fd)
        assert (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
    finally:
        lock.release()


@pytest.mark.parametrize('boundary', ('construction', 'acquisition'))
@pytest.mark.parametrize('replacement', ('hardlink', 'directory', 'reparse'))
def test_recreated_unowned_lock_is_revalidated_before_native_open(tmp_path, monkeypatch, boundary, replacement):
    from carr_library.operations import _locks
    path, outside = tmp_path / '.recreated.lock', tmp_path / 'unrelated-state'
    path.write_bytes(b'')
    outside.write_bytes(b'Preserve unrelated bytes.')
    existing = _locks.NativeLock(path) if boundary == 'acquisition' else None
    reparse = []

    def replace_leaf():
        if replacement == 'hardlink':
            os.link(outside, path)
        elif replacement == 'directory':
            path.mkdir()
        else:
            path.write_bytes(b'')
            reparse.append(True)

    # Exercise the Windows reparse metadata refusal on either host without
    # requiring permission to create a symlink or following an actual target.
    original_lstat = Path.lstat
    def attributes(selected, *args, **kwargs):
        info = original_lstat(selected, *args, **kwargs)
        if Path(selected) == path and reparse:
            values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            values['st_file_attributes'] = getattr(info, 'st_file_attributes', 0) | 0x400
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(Path, 'lstat', attributes)
    changed = _release_during_local_path_validation(monkeypatch, path, replace_leaf)
    if existing is not None:
        def refuse_native_open(*args, **kwargs):
            pytest.fail('An unsafe replacement reached native lock acquisition.')
        monkeypatch.setattr(existing.lock, 'acquire', refuse_native_open)
    with pytest.raises(ValueError, match='hard-link|regular local audio file|Reparse'):
        lock = existing or _locks.NativeLock(path)
        lock.acquire()
    assert changed == [True]
    assert existing is None or not existing.held
    assert outside.read_bytes() == b'Preserve unrelated bytes.'


@pytest.mark.parametrize('replacement', ('missing', 'file'))
def test_released_leaf_does_not_hide_a_missing_or_non_directory_parent(tmp_path, monkeypatch, replacement):
    from carr_library.operations import _locks
    parent = tmp_path / 'runtime'
    parent.mkdir()
    path = parent / '.released.lock'
    path.write_bytes(b'')
    lock = _locks.NativeLock(path)

    def replace_parent():
        parent.rmdir()
        if replacement == 'file':
            parent.write_bytes(b'Preserve the replacement parent.')

    changed = _release_during_local_path_validation(monkeypatch, path, replace_parent)
    with pytest.raises((OSError, ValueError)):
        lock.acquire()
    assert changed == [True] and not lock.held
    if replacement == 'file':
        assert parent.read_bytes() == b'Preserve the replacement parent.'
    else:
        assert not parent.exists()


def test_recreated_unowned_leaf_does_not_hide_a_denied_validation(tmp_path, monkeypatch):
    from carr_library.operations import _locks
    path = tmp_path / '.recreated.lock'
    path.write_bytes(b'')
    lock = _locks.NativeLock(path)
    changed = _release_during_local_path_validation(
        monkeypatch, path, lambda: path.write_bytes(b''))
    original, denied = _locks.regular, PermissionError('fixture denied metadata')

    def denied_leaf(selected, **kwargs):
        if Path(selected) == path and changed:
            raise denied
        return original(selected, **kwargs)

    monkeypatch.setattr(_locks, 'regular', denied_leaf)
    with pytest.raises(PermissionError) as caught:
        lock.acquire()
    assert caught.value is denied
    assert changed == [True] and not lock.held


def test_repeated_unowned_leaf_churn_fails_without_native_acquisition(tmp_path, monkeypatch):
    from carr_library.operations import _locks
    path = tmp_path / '.unstable.lock'
    path.write_bytes(b'')
    lock = _locks.NativeLock(path)
    changed = _release_during_local_path_validation(
        monkeypatch, path, lambda: path.write_bytes(b''), repetitions=10)

    def refuse_native_open(*args, **kwargs):
        pytest.fail('An unstable path reached native lock acquisition.')

    monkeypatch.setattr(lock.lock, 'acquire', refuse_native_open)
    with pytest.raises(ResourceStateError, match='kept changing before native acquisition'):
        lock.acquire()
    assert changed == [True] * 3
    assert not lock.held and path.read_bytes() == b''


def test_unowned_lock_replacement_still_rejects_a_hardlink_alias(tmp_path, monkeypatch):
    from carr_library.operations import _locks
    path, outside = tmp_path / '.replaced.lock', tmp_path / 'other-state'
    path.write_bytes(b'')
    outside.write_bytes(b'Preserve this unrelated state.')
    lock = _locks.NativeLock(path)
    original, replaced = _locks.regular, []

    def changed(selected):
        if Path(selected) == path and not replaced:
            path.unlink()
            os.link(outside, path)
            replaced.append(True)
        return original(selected)

    monkeypatch.setattr(_locks, 'regular', changed)
    with pytest.raises(ValueError, match='hard-link'):
        lock.acquire()
    assert not lock.held
    assert outside.read_bytes() == b'Preserve this unrelated state.'


def test_runtime_leases_outside_backup_state_and_truthful_capabilities(tmp_path):
    broker = ResourceBroker.for_app(tmp_path)
    assert broker.root == tmp_path / 'runtime' / 'resources'
    assert broker.policy_path == tmp_path / 'state' / 'operations' / 'resources.json'
    facts = broker.capabilities()
    assert facts['pid_based_reaping'] is False and facts['network_sandbox'] is False
    assert facts['orphan_native_workers_managed'] is False
    assert 'no physical RAM/VRAM measurement' in facts['memory_policy']



def test_forked_child_cannot_release_parent_reservation(broker, monkeypatch):
    """Keep the donor fork boundary and exercise its PID guard on every host."""
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='parent', timeout=0) as lease:
        parent_pid = os.getpid()
        # Windows has no fork; the actual inherited-owner predicate must still
        # reject a foreign PID without touching the parent's native lock.
        with monkeypatch.context() as changed:
            changed.setattr(os, 'getpid', lambda: parent_pid + 1)
            with pytest.raises(ResourceStateError, match='parent reservation'):
                lease.release()
            with pytest.raises(ResourceStateError, match='parent reservation'):
                lease._lock.release()
            with pytest.raises(ResourceStateError, match='current process'):
                lease.checkpoint()
        assert lease._lock.held
        assert len(broker.snapshot()['leases']) == 1

        if 'fork' in multiprocessing.get_all_start_methods():
            # A real fork copies the open description and the lease object.
            # A successful child refusal must leave the parent's lease held.
            def child_release():
                try:
                    lease.release()
                except ResourceStateError:
                    os._exit(0)
                os._exit(1)

            child = multiprocessing.get_context('fork').Process(target=child_release)
            child.start()
            try:
                child.join(10)
                assert not child.is_alive(), 'Inherited lease refusal did not settle.'
                assert child.exitcode == 0
            finally:
                if child.is_alive():
                    child.terminate()
                child.join(5)
                assert not child.is_alive(), 'Owned fixture child did not stop.'
                child.close()
            assert lease._lock.held
            assert len(broker.snapshot()['leases']) == 1
    assert broker.snapshot()['leases'] == []

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


if __name__ == '__main__':
    if sys.argv[2] == 'crash':
        test_actual_spawned_process_crash_releases_without_pid_reaping(ResourceBroker(Path(sys.argv[3])))
    elif sys.argv[2] == 'contention':
        test_actual_process_contention_has_no_overlapping_admission(Path(sys.argv[3]))
    else:
        raise SystemExit('Unknown resource process fixture scenario.')
