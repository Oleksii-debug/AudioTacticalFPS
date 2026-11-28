"""Windows job ownership and failure boundaries; native cases run on Windows CI."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import FunctionType, SimpleNamespace

import pytest

from carr_library.audio import windows_process as module


def fixture_launcher(native):
    class Handle(int):
        closed = False
        def Close(self):
            if not self.closed:
                self.closed = True
                native.CloseHandle(int(self))

    def launch(owner, command, *, fail_after=False):
        process, thread, pid, _ = _winapi.CreateProcess(
            None, command, None, None, True, 0, None, None, None)
        owner._handle = Handle(process)
        owner.pid = pid
        owner._child_created = True
        _winapi.CloseHandle(thread)
        if fail_after:
            raise RuntimeError('after handle adoption')

    namespace = dict(launch.__globals__, _winapi=native)
    result = FunctionType(launch.__code__, namespace, launch.__name__,
                          launch.__defaults__, launch.__closure__)
    result.__kwdefaults__ = launch.__kwdefaults__
    return result


def fixture_api(monkeypatch, *, fail=None, assigned=None, release=None):
    events = []
    closed = set()

    class Native:
        counter = 10
        def CreateProcess(self, *arguments):
            events.append(('create', arguments[1], arguments[5]))
            if fail == 'create':
                raise OSError('create failed')
            self.counter += 10
            return self.counter, self.counter + 1, self.counter + 2, self.counter + 3

        def CloseHandle(self, handle):
            assert handle not in closed, 'A native handle was closed twice.'
            closed.add(handle)
            events.append(('close', handle))

        def TerminateProcess(self, process, status):
            events.append(('terminate', process))

        def WaitForSingleObject(self, process, timeout):
            events.append(('wait', process, timeout))
            return 0

    class Job:
        def __init__(self):
            self.closed = False
            events.append(('job',))

        def assign(self, process):
            events.append(('assign', process))
            if fail == 'assign':
                raise OSError('assign failed')
            if assigned is not None:
                assigned.set()
                assert release.wait(5)

        def resume(self, thread):
            events.append(('resume', thread))
            if fail == 'resume':
                raise OSError('resume failed')

        def record_leader_exit(self, process):
            assert ('wait', process, 5000) in events
            events.append(('leader-signaled', process))

        def close(self):
            if not self.closed:
                self.closed = True
                events.append(('job-close',))

    monkeypatch.setattr(module, '_Job', Job)
    native = Native()
    return native, events, closed


def test_owned_launch_assigns_before_resume_without_changing_native_globals(monkeypatch):
    native, events, closed = fixture_api(monkeypatch)
    source = fixture_launcher(native)
    owner = SimpleNamespace(_child_created=False)
    module._execute_owned(owner, source, ('owned',), {})
    assert source.__globals__['_winapi'] is native
    assert events[:5] == [('job',), ('create', 'owned', module.CREATE_SUSPENDED | module.CREATE_NO_WINDOW),
                          ('assign', 20), ('resume', 21), ('close', 21)]
    assert not owner._carr_job.closed
    owner._carr_job.close()
    owner._handle.Close()
    assert closed == {20, 21}


@pytest.mark.parametrize('failure', ['create', 'assign', 'resume', 'after-adoption'])
def test_launch_failure_closes_only_exact_owned_handles(monkeypatch, failure):
    native, events, closed = fixture_api(monkeypatch, fail=failure)
    owner = SimpleNamespace(_child_created=False)
    with pytest.raises((OSError, RuntimeError)):
        module._execute_owned(owner, fixture_launcher(native), ('owned',),
                              {'fail_after': failure == 'after-adoption'})
    assert events.count(('job-close',)) == 1
    assert owner._carr_job is None and owner._child_created is False
    if failure == 'create':
        assert closed == set()
    else:
        assert closed == {20, 21}
        assert ('terminate', 20) in events
        assert ('wait', 20, 5000) in events
    if failure == 'assign':
        assert not any(event[0] == 'resume' for event in events)


def test_concurrent_launcher_keeps_unrelated_process_outside_owned_proxy(monkeypatch):
    assigned, release = threading.Event(), threading.Event()
    native, events, closed = fixture_api(monkeypatch, assigned=assigned, release=release)
    source = fixture_launcher(native)
    owned, unrelated = SimpleNamespace(_child_created=False), SimpleNamespace(_child_created=False)
    errors = []

    def run():
        try:
            module._execute_owned(owned, source, ('owned',), {})
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert assigned.wait(5)
        assert source.__globals__['_winapi'] is native
        source(unrelated, 'unrelated')
        assert ('create', 'unrelated', 0) in events
        assert not hasattr(unrelated, '_carr_job')
    finally:
        release.set()
        worker.join(5)
        if hasattr(owned, '_carr_job') and owned._carr_job is not None:
            owned._carr_job.close()
        for owner in (owned, unrelated):
            if hasattr(owner, '_handle'):
                owner._handle.Close()
    assert not worker.is_alive() and not errors
    assert [event for event in events if event[0] == 'assign'] == [('assign', 20)]
    assert closed == {20, 21, 30, 31}


@pytest.mark.parametrize('configure', [True, False])
def test_job_uses_kill_on_close_without_breakaway_and_closes_on_configuration_failure(monkeypatch, configure):
    calls, ports = [], []
    class Kernel:
        def CreateJobObjectW(self, security, name):
            assert security is None and name is None
            return 101
        def CreateIoCompletionPort(self, file, existing, key, concurrency):
            assert file == ctypes.c_void_p(-1).value and existing is None and key == concurrency == 0
            ports.append('create')
            return 201
        def SetHandleInformation(self, port, mask, flags):
            assert (port, mask, flags) == (201, 1, 0)
            ports.append('noninheritable')
            return True
        def SetInformationJobObject(self, job, kind, pointer, size):
            if kind == 7:
                value = ctypes.cast(pointer, ctypes.POINTER(module._CompletionPort)).contents
                assert (value.key, value.port) == (101, 201)
                ports.append('associate-empty')
                return True
            limits = ctypes.cast(pointer, ctypes.POINTER(module._ExtendedLimits)).contents
            calls.append(('limits', job, kind, size, limits.basic.flags))
            return configure
        def TerminateJobObject(self, job, status):
            calls.append(('terminate-job', job))
            return True
        def CloseHandle(self, job):
            if job == 201:
                ports.append('close')
                return True
            calls.append(('close-job', job))
            return True
    monkeypatch.setattr(module, '_kernel',
                        lambda: SimpleNamespace(kernel=Kernel(), error=lambda: OSError('native failure')))
    if configure:
        job = module._Job()
        job.close()
        job.close()
    else:
        with pytest.raises(OSError, match='native failure'):
            module._Job()
    assert calls == [('limits', 101, 9, ctypes.sizeof(module._ExtendedLimits), module.KILL_ON_JOB_CLOSE),
                     ('terminate-job', 101), ('close-job', 101)]
    assert ports == (['create', 'noninheritable', 'associate-empty', 'close'] if configure else [])

@pytest.mark.parametrize('failure', ['terminate', 'close'])
def test_failed_job_shutdown_retains_the_exact_handle_for_retry(monkeypatch, failure):
    calls, released = [], False
    class Kernel:
        def CreateJobObjectW(self, *_args): return 101
        def SetInformationJobObject(self, *_args): return True
        def CreateIoCompletionPort(self, *_args): return 201
        def SetHandleInformation(self, *_args): return True
        def TerminateJobObject(self, handle, status):
            calls.append(('terminate', handle))
            return released or failure != 'terminate'
        def CloseHandle(self, handle):
            if handle == 201:
                return True
            calls.append(('close', handle))
            return released or failure != 'close'
    monkeypatch.setattr(module, '_kernel',
        lambda: SimpleNamespace(kernel=Kernel(), error=lambda: OSError('Synthetic shutdown failure')))
    job = module._Job()
    with pytest.raises(OSError, match='shutdown failure'):
        job.close()
    assert job.handle == 101
    if failure == 'terminate':
        assert ('close', 101) not in calls
    released = True
    assert job.close() is True and job.handle is None
    assert calls[-2:] == [('terminate', 101), ('close', 101)]


def test_assignment_and_abort_failure_keep_the_unassigned_child_owned_until_retry(monkeypatch):
    from carr_library.audio.cleanup import AudioCleanupPending
    native, events, closed = fixture_api(monkeypatch, fail='assign')
    released = threading.Event()
    original_terminate, original_wait = native.TerminateProcess, native.WaitForSingleObject
    def terminate(process, status):
        if not released.is_set():
            raise OSError('Synthetic termination failure before assignment')
        return original_terminate(process, status)
    def wait(process, timeout):
        return original_wait(process, timeout) if released.is_set() else 0x102
    native.TerminateProcess, native.WaitForSingleObject = terminate, wait
    owner = SimpleNamespace(_child_created=False)
    pending = None
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            module._execute_owned(owner, fixture_launcher(native), ('owned',), {})
        pending = failure.value.cleanup_owner
        assert not pending.quiescent and owner._carr_job is not None
        assert not closed and not any(event[0] == 'resume' for event in events)
        released.set()
        assert pending.quiescent_event.wait(5)
        assert pending.close() and closed == {20, 21}
        assert owner._carr_job is None and owner._child_created is False
        assert ('terminate', 20) in events
    finally:
        released.set()
        if pending is not None:
            pending.close()
            assert pending.quiescent_event.wait(5)

def drain_job(monkeypatch):
    """Model native accounting separately from actual process-handle signals."""
    events = []
    class Kernel:
        identifiers = (22,)
        total, active = 2, 1  # Original leader exited; its descendant remains.
        member = True
        wait_result = 0
        fail = None
        late_member = False
        inventory_count = None
        on_query = None
        on_wait = None
        open_error = None
        error_code = 5

        def __init__(self):
            self.notifications = []
        def CreateJobObjectW(self, *_args):
            return 101
        def CreateIoCompletionPort(self, file, existing, key, concurrency):
            assert file == ctypes.c_void_p(-1).value and existing is None and key == concurrency == 0
            events.append(('port-create', 201))
            return 201
        def SetHandleInformation(self, port, mask, flags):
            assert (port, mask, flags) == (201, 1, 0)
            events.append(('port-noninheritable', port))
            return True
        def SetInformationJobObject(self, job, kind, pointer, size):
            if kind == 7:
                value = ctypes.cast(pointer, ctypes.POINTER(module._CompletionPort)).contents
                assert (value.key, value.port) == (101, 201)
                events.append(('port-associate', job))
            else:
                assert kind == 9
            return True
        def GetQueuedCompletionStatus(self, port, message, key, value, milliseconds):
            assert port == 201 and milliseconds == 0
            if not self.notifications:
                self.error_code = 258
                return False
            code, pid, expected_key = self.notifications.pop(0)
            ctypes.cast(message, ctypes.POINTER(ctypes.c_uint32)).contents.value = code
            ctypes.cast(key, ctypes.POINTER(ctypes.c_size_t)).contents.value = expected_key
            ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p)).contents.value = pid
            events.append(('notice', code, pid, expected_key))
            return True
        def GetProcessId(self, handle):
            assert handle == 701
            return 11
        def AssignProcessToJobObject(self, job, handle):
            assert job == 101 and handle == 701
            self.notifications.append((6, 11, 101))
            events.append(('assign-job', handle))
            return True
        def QueryInformationJobObject(self, job, kind, pointer, size, length):
            assert job == 101
            assert (length is None) == (kind != 3)
            events.append(('query', kind))
            if self.on_query is not None:
                self.on_query(kind)
            if kind == 1:
                if self.fail == 'initial-query':
                    return False
                if self.fail == 'query-after-termination' and self.active == 0:
                    return False
                value = ctypes.cast(pointer, ctypes.POINTER(module._Accounting)).contents
                value.total_processes, value.active_processes = self.total, self.active
            elif kind == 3:
                value = ctypes.cast(pointer, ctypes.POINTER(module._ProcessIds)).contents
                value.assigned = value.listed = (
                    len(self.identifiers) if self.inventory_count is None else self.inventory_count)
                for index, pid in enumerate(self.identifiers):
                    value.processes[index] = pid
                ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents.value = (
                    module._ProcessIds.processes.offset
                    + min(value.listed, module._MAX_JOB_MEMBERS) * ctypes.sizeof(ctypes.c_size_t))
            else:
                raise AssertionError(kind)
            return True
        def OpenProcess(self, access, inherit, pid):
            assert access == 0x00101000 and inherit is False
            events.append(('open-member', pid))
            if self.open_error is not None:
                self.error_code = self.open_error
                return 0
            return 1000 + pid
        def IsProcessInJob(self, process, job, pointer):
            assert job == 101
            events.append(('membership', process))
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = int(self.member)
            return True
        def TerminateJobObject(self, job, status):
            assert job == 101 and status == 1
            events.append(('terminate-job', job))
            if self.fail == 'terminate':
                return False
            self.active = 0
            if self.late_member:
                self.total += 1
                self.identifiers = ()
            return True
        def WaitForSingleObject(self, handle, milliseconds):
            if handle == 701:
                events.append(('leader-signal', handle, milliseconds))
                return 0
            events.append(('wait-member', handle, milliseconds))
            if self.on_wait is not None:
                self.on_wait(milliseconds)
            return self.wait_result
        def CloseHandle(self, handle):
            events.append(('close', handle))
            return not ((self.fail == 'member-close' and handle not in (101, 201))
                        or (self.fail == 'port-close' and handle == 201))

    kernel = Kernel()
    def native_error():
        error = OSError('Synthetic native drain failure')
        error.winerror = kernel.error_code
        return error
    monkeypatch.setattr(module, '_kernel',
        lambda: SimpleNamespace(kernel=kernel, error=native_error))
    job = module._Job()
    job.assign(701)
    def wait(*, timeout):
        assert 0 < timeout <= 5
        events.append(('wait-leader',))
    class Handle(int):
        def Close(self):
            events.append(('close-leader',))
    leader = SimpleNamespace(wait=wait, pid=11, _handle=Handle(701))
    return job, kernel, leader, events


def test_zero_accounting_does_not_release_an_unsignaled_member(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.wait_result = 0x102
    with pytest.raises(TimeoutError, match='process has not confirmed exit'):
        job.close(leader=leader)
    assert kernel.active == 0
    assert job.handle == 101 and job._members == {22: (1022, True)}
    assert ('close', 1022) not in events and ('close', 101) not in events
    kernel.wait_result = 0
    assert job.close(leader=leader) is True
    assert job.handle is None and not job._members
    assert events.count(('open-member', 22)) == 1
    assert events.index(('close', 1022)) < events.index(('close', 101))


@pytest.mark.parametrize('boundary', ['terminate', 'query-after-termination', 'member-close', 'wait'])
def test_failed_member_drain_retains_exact_native_handles_for_retry(monkeypatch, boundary):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.fail = boundary
    if boundary == 'wait':
        kernel.wait_result = 0xFFFFFFFF
    with pytest.raises(OSError, match='native drain failure'):
        job.close(leader=leader)
    assert job.handle == 101 and job._members == {22: (1022, True)}
    assert ('close', 101) not in events
    kernel.fail, kernel.wait_result = None, 0
    assert job.close(leader=leader)
    assert job.handle is None and not job._members
    assert events.count(('open-member', 22)) == 1


def test_pid_reuse_never_adopts_or_waits_for_an_unrelated_process(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.member = False
    with pytest.raises(OSError, match='membership changed'):
        job.close(leader=leader)
    assert job.handle == 101 and not job._members
    assert ('close', 1022) in events
    assert ('terminate-job', 101) in events
    assert not any(event[0] == 'wait-member' for event in events)
    # Stop only the exact owned job. An empty later inventory cannot replace
    # the missing historical proof or authorize adopting the unrelated PID.
    kernel.identifiers, kernel.active = (), 0
    with pytest.raises(OSError, match='membership changed'):
        job.close(leader=leader)
    assert job.handle == 101 and ('close', 101) not in events
    assert events.count(('open-member', 22)) == 1


def test_late_member_does_not_rebase_cleanup_onto_empty_accounting(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.late_member = True
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert kernel.active == 0 and kernel.identifiers == ()
    assert job.handle == 101 and job._members == {22: (1022, True)}
    kernel.late_member = False
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert job.handle == 101 and job._members == {22: (1022, True)}
    assert events.count(('query', 3)) == 1
    assert ('close', 101) not in events and ('close', 1022) not in events


def test_native_member_inventory_is_bounded_before_opening_handles(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.inventory_count = module._MAX_JOB_MEMBERS + 1
    with pytest.raises(OSError, match='bounded membership'):
        job.close(leader=leader)
    assert job.handle == 101
    assert ('terminate-job', 101) in events
    assert not any(event[0] in ('open-member', 'close') for event in events)


def test_enumeration_and_member_wait_share_the_existing_five_second_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    job, kernel, leader, events = drain_job(monkeypatch)
    def enumerate_time(kind):
        if kind == 3:
            now[0] += 4.8
    kernel.on_query = enumerate_time
    def wait_time(milliseconds):
        assert 0 <= milliseconds <= 200
        now[0] += .3
    kernel.on_wait = wait_time
    with pytest.raises(TimeoutError, match='job has not confirmed shutdown'):
        job.close(leader=leader)
    assert job.handle == 101 and ('close', 101) not in events
    # The observed member is signaled and closed, but the unproved job is still
    # owned; an explicit cleanup retry does not reopen the old PID.
    kernel.on_query = kernel.on_wait = None
    assert job.close(leader=leader)
    assert events.count(('open-member', 22)) == 1



def test_initial_empty_inventory_does_not_prove_a_historical_descendant_exited(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.active = (), 0
    kernel.wait_result = 0x102  # The external descendant handle is still unsignaled.
    job.record_leader_exit(701)
    with pytest.raises(OSError, match='historical.*no retained exit proof'):
        job.close(leader=leader)
    assert job._leader_signaled and kernel.total == 2
    assert kernel.wait_result == 0x102
    assert job.handle == 101 and not job._members
    assert ('terminate-job', 101) in events and ('close', 101) not in events
    assert not any(event[0] in ('open-member', 'wait-member') for event in events)
    with pytest.raises(OSError, match='historical.*no retained exit proof'):
        job.close(leader=leader)
    assert events.count(('query', 3)) == 1
    assert job.handle == 101  # Never rebase onto empty post-termination accounting.


def test_exact_leader_signal_proves_a_single_process_empty_inventory(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.total, kernel.active = (), 1, 0
    assert job.close(leader=leader)
    assert job.handle is None and job._leader_signaled
    assert events.count(('wait-leader',)) == 1
    assert events.count(('leader-signal', 701, 0)) == 1
    assert events.index(('leader-signal', 701, 0)) < events.index(('close-leader',))
    assert not any(event[0] == 'open-member' for event in events)


def test_inventory_query_failure_stops_owned_job_without_discarding_uncertainty(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.fail = 'initial-query'
    with pytest.raises(OSError, match='native drain failure'):
        job.close(leader=leader)
    assert ('terminate-job', 101) in events
    assert job.handle == 101 and ('close', 101) not in events
    kernel.fail, kernel.identifiers = None, ()
    with pytest.raises(OSError, match='native drain failure'):
        job.close(leader=leader)
    assert events.count(('query', 1)) == 1
    assert events.count(('terminate-job', 101)) == 2
    assert job.handle == 101


def test_assigned_job_without_retained_leader_exit_proof_remains_owned(monkeypatch):
    job, kernel, _leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.total, kernel.active = (), 1, 0
    with pytest.raises(OSError, match='leader has no recorded exit proof'):
        job.close()
    assert job.handle == 101 and ('close', 101) not in events
    job.record_leader_exit(701)
    assert job.close() and job.handle is None


def test_aborted_launch_records_exact_leader_signal_before_releasing_handles(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.total, kernel.active = (), 1, 0
    class Native:
        def TerminateProcess(self, process, status):
            assert process == 701 and status == 1
            events.append(('abort-terminate', process))
        def WaitForSingleObject(self, process, milliseconds):
            assert process == 701 and milliseconds == 5000
            events.append(('abort-wait', process))
            return 0
        def CloseHandle(self, handle):
            assert handle == 702
            events.append(('abort-thread-close', handle))
    proxy = module._OwnedWinAPI(Native(), job)
    proxy.process = (701, 702, 11, 12)
    leader._child_created, leader._carr_job = True, job
    assert proxy.abort(leader)
    assert leader._child_created is False and leader._carr_job is None
    assert job.handle is None and job._leader_signaled
    assert events.index(('abort-wait', 701)) < events.index(('leader-signal', 701, 0))
    assert events.index(('leader-signal', 701, 0)) < events.index(('close-leader',))
    assert events.index(('close-leader',)) < events.index(('close', 101))


def test_completion_port_is_private_and_associated_before_assignment(monkeypatch):
    job, _kernel, leader, events = drain_job(monkeypatch)
    assert events.index(('port-noninheritable', 201)) < events.index(('port-associate', 101))
    assert events.index(('port-associate', 101)) < events.index(('assign-job', 701))
    assert job.close(leader=leader)
    assert events.index(('close', 101)) < events.index(('close', 201))


def test_positive_notice_accounts_for_a_short_child_that_no_longer_exists(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.active, kernel.open_error = (), 0, 87
    kernel.notifications.extend([(6, 22, 101), (7, 22, 101), (4, None, 101)])
    assert job.close(leader=leader)
    assert job.handle is None and job.port is None and job._completed_members == {22}
    assert events.count(('open-member', 22)) == 1
    assert not any(event[0] == 'wait-member' for event in events)


def test_positive_notice_never_credits_an_opened_nonmember_as_completed(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.active, kernel.member = (), 0, False
    kernel.notifications.append((6, 22, 101))
    with pytest.raises(OSError, match='membership changed'):
        job.close(leader=leader)
    assert not job._completed_members
    assert job.handle == 101 and job.port == 201
    assert events.count(('close', 1022)) == 1
    assert not any(event[0] == 'wait-member' for event in events)
    assert ('terminate-job', 101) in events
    assert ('close', 101) not in events and ('close', 201) not in events


def test_duplicate_notifications_cannot_invent_missing_lifetime_coverage(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.total, kernel.active, kernel.open_error = (), 3, 0, 87
    kernel.notifications.extend([(6, 22, 101), (6, 22, 101), (7, 22, 101), (4, None, 101)])
    with pytest.raises(OSError, match='historical.*no retained exit proof'):
        job.close(leader=leader)
    assert job._completed_members == {22} and job._shutdown_total == 3
    assert job.handle == 101 and job.port == 201
    assert events.count(('open-member', 22)) == 1
    assert ('close', 101) not in events and ('close', 201) not in events


def test_exit_and_zero_notifications_do_not_supply_missing_creation_evidence(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.active = (), 0
    kernel.notifications.extend([(7, 22, 101), (4, None, 101)])
    with pytest.raises(OSError, match='historical.*no retained exit proof'):
        job.close(leader=leader)
    assert job.handle == 101 and not job._completed_members
    assert not any(event[0] == 'open-member' for event in events)


def test_foreign_completion_key_stops_only_owned_job_and_retains_uncertainty(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.notifications.append((6, 22, 999))
    with pytest.raises(OSError, match='foreign completion key'):
        job.close(leader=leader)
    assert job.handle == 101 and job.port == 201
    assert ('terminate-job', 101) in events
    assert not any(event[0] in ('open-member', 'close') for event in events)


@pytest.mark.parametrize('pid', [0, 2**32], ids=['zero', 'beyond-dword'])
def test_notified_process_identity_is_bounded_before_open(monkeypatch, pid):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.notifications.append((6, pid, 101))
    with pytest.raises(OSError, match='invalid process identity'):
        job.close(leader=leader)
    assert job.handle == 101 and job.port == 201
    assert not any(event[0] == 'open-member' for event in events)


def test_notification_inventory_has_a_total_bound_across_cleanup_retries(monkeypatch):
    monkeypatch.setattr(module, '_MAX_JOB_NOTIFICATIONS', 2)
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.notifications.extend([(6, 22, 101), (7, 22, 101)])
    with pytest.raises(OSError, match='bounded notification inventory'):
        job.close(leader=leader)
    assert job.handle == 101 and job.port == 201
    assert job._members == {22: (1022, True)}
    assert ('terminate-job', 101) in events
    assert not any(event[0] == 'close' for event in events)


def test_failed_port_close_retains_exact_port_after_completed_process_proofs(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.fail = 'port-close'
    with pytest.raises(OSError, match='native drain failure'):
        job.close(leader=leader)
    assert job.handle is None and job.port == 201 and not job._members
    assert job._leader_signaled and job._completed_members == {22}
    kernel.fail = None
    assert job.close(leader=leader) and job.port is None
    assert events.count(('terminate-job', 101)) == 1
    assert events.count(('close', 201)) == 2


def test_delayed_positive_notice_can_complete_preserved_historical_proof(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.identifiers, kernel.active = (), 0
    with pytest.raises(OSError, match='historical.*no retained exit proof'):
        job.close(leader=leader)
    assert job._shutdown_total == 2 and job.handle == 101
    kernel.notifications.append((6, 22, 101))
    kernel.open_error = 87
    assert job.close(leader=leader)
    assert job.handle is None and job.port is None and job._shutdown_total == 2
    assert events.count(('query', 3)) == 1  # No empty-inventory rebasing.


@pytest.mark.parametrize('failure', ['create', 'inheritance', 'association'])
def test_notification_setup_failure_cleans_only_created_native_objects(monkeypatch, failure):
    events = []
    class Kernel:
        def CreateJobObjectW(self, *_args): return 101
        def CreateIoCompletionPort(self, *_args):
            events.append(('port-create',))
            return None if failure == 'create' else 201
        def SetHandleInformation(self, port, mask, flags):
            assert (port, mask, flags) == (201, 1, 0)
            return failure != 'inheritance'
        def SetInformationJobObject(self, job, kind, pointer, size):
            assert job == 101 and kind in (7, 9)
            return not (failure == 'association' and kind == 7)
        def TerminateJobObject(self, job, status):
            assert (job, status) == (101, 1)
            events.append(('terminate', job))
            return True
        def CloseHandle(self, handle):
            events.append(('close', handle))
            return True
    monkeypatch.setattr(module, '_kernel',
        lambda: SimpleNamespace(kernel=Kernel(), error=lambda: OSError('Explicit port setup failure')))
    with pytest.raises(OSError, match='port setup failure'):
        module._Job()
    assert ('terminate', 101) in events and ('close', 101) in events
    assert events.count(('close', 201)) == (0 if failure == 'create' else 1)


def membership_replies(monkeypatch, kernel, events, replies):
    """Return recorded native structures through the actual query wrapper."""
    original = kernel.QueryInformationJobObject
    observed = []

    def query(job, kind, pointer, size, length):
        result = original(job, kind, pointer, size, length)
        if kind != 3:
            return result
        assert length is not None and size == ctypes.sizeof(module._ProcessIds)
        reply = replies[min(len(observed), len(replies) - 1)]
        value = ctypes.cast(pointer, ctypes.POINTER(module._ProcessIds)).contents
        value.assigned, value.listed = reply['assigned'], reply['listed']
        for index, pid in enumerate(reply.get('identifiers', ())):
            value.processes[index] = pid
        returned = reply.get('returned_length',
            module._ProcessIds.processes.offset
            + min(value.listed, module._MAX_JOB_MEMBERS) * ctypes.sizeof(ctypes.c_size_t))
        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents.value = returned
        observed.append((int(value.assigned), int(value.listed), returned))
        events.append(('membership-reply', len(observed), int(value.assigned), int(value.listed)))
        if reply.get('winerror') is not None:
            kernel.error_code = reply['winerror']
            return False
        return result

    monkeypatch.setattr(kernel, 'QueryInformationJobObject', query)
    return observed


def test_partial_membership_retries_before_termination_and_retains_complete_handles(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    observed = membership_replies(monkeypatch, kernel, events, [
        {'assigned': 2, 'listed': 1, 'identifiers': (0,)},
        {'assigned': 2, 'listed': 2, 'identifiers': (11, 22)},
    ])
    assert job.close(leader=leader)
    assert [(a, n) for a, n, _length in observed] == [(2, 1), (2, 2)]
    complete = events.index(('membership-reply', 2, 2, 2))
    assert complete < events.index(('open-member', 22)) < events.index(('terminate-job', 101))
    assert events.count(('open-member', 22)) == 1
    member_waits = [event for event in events if event[:2] == ('wait-member', 1022)]
    assert len(member_waits) == 1
    assert events.index(('close', 1022)) < events.index(('close', 101))
    assert job._shutdown_total == 2 and job._admission_failure is None
    assert job.handle is None and job.port is None and not job._members
    assert job._membership_query == {
        'attempt': 2, 'api_success': 1, 'assigned': 2, 'listed': 2,
        'capacity': module._MAX_JOB_MEMBERS, 'buffer_bytes': ctypes.sizeof(module._ProcessIds),
        'returned_length': module._ProcessIds.processes.offset + 2 * ctypes.sizeof(ctypes.c_size_t),
        'winerror': None,
    }


def test_permanently_partial_inventory_consumes_original_deadline_and_stays_latched(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    job, kernel, leader, events = drain_job(monkeypatch)
    replies = [{'assigned': 2, 'listed': 1, 'identifiers': (0,)}]
    observed = membership_replies(monkeypatch, kernel, events, replies)
    def advance(kind):
        if kind == 3:
            clock[0] += 1.0
    kernel.on_query = advance
    with pytest.raises(TimeoutError, match='job has not confirmed shutdown') as failure:
        job.close(leader=leader)
    assert 105.0 <= clock[0] < 106.0
    assert len(observed) == 5
    assert job._shutdown_total is None and job._admission_failure is failure.value
    assert job.handle == 101 and job.port == 201 and not job._members
    assert events.count(('terminate-job', 101)) == 1
    assert not any(event[0] in ('open-member', 'wait-member', 'close') for event in events)
    note = failure.value.__notes__[-1]
    assert "'attempt': 5" in note and "'assigned': 2" in note and "'listed': 1" in note
    assert "'api_success': 1" in note and "'winerror': None" in note
    # A later complete reply cannot replace failed pre-termination admission.
    replies[:] = [{'assigned': 0, 'listed': 0}]
    with pytest.raises(TimeoutError) as again:
        job.close(leader=leader)
    assert again.value is failure.value
    assert len(observed) == 5 and events.count(('terminate-job', 101)) == 2
    assert job.handle == 101 and job.port == 201


@pytest.mark.parametrize('assigned,listed', [
    (0, 1), (1, 2), (4097, 0), (4097, 4097), (4096, 4097), (0, 4097),
])
def test_invalid_or_over_cap_inventory_never_retries_or_opens_a_pid(monkeypatch, assigned, listed):
    job, kernel, leader, events = drain_job(monkeypatch)
    replies = [{'assigned': assigned, 'listed': listed, 'identifiers': (0,)}]
    observed = membership_replies(monkeypatch, kernel, events, replies)
    message = 'bounded membership' if max(assigned, listed) > module._MAX_JOB_MEMBERS else 'invalid membership'
    with pytest.raises(OSError, match=message) as failure:
        job.close(leader=leader)
    assert len(observed) == 1 and job._shutdown_total is None
    assert job._admission_failure is failure.value and job.handle == 101 and job.port == 201
    assert not any(event[0] in ('open-member', 'wait-member', 'close') for event in events)
    assert events.count(('terminate-job', 101)) == 1
    assert f"'assigned': {assigned}" in failure.value.__notes__[-1]
    assert f"'listed': {listed}" in failure.value.__notes__[-1]
    replies[:] = [{'assigned': 0, 'listed': 0}]
    with pytest.raises(OSError) as again:
        job.close(leader=leader)
    assert again.value is failure.value and len(observed) == 1


@pytest.mark.parametrize('identifiers', [(22, 22), (0,), (0x100000000,)])
def test_complete_retry_still_rejects_invalid_process_identities(monkeypatch, identifiers):
    job, kernel, leader, events = drain_job(monkeypatch)
    observed = membership_replies(monkeypatch, kernel, events, [
        {'assigned': 2, 'listed': 1, 'identifiers': (0,)},
        {'assigned': len(identifiers), 'listed': len(identifiers), 'identifiers': identifiers},
    ])
    with pytest.raises(OSError, match='invalid process inventory') as failure:
        job.close(leader=leader)
    assert len(observed) == 2 and job._admission_failure is failure.value
    assert job._shutdown_total is None and job.handle == 101 and job.port == 201
    assert not any(event[0] in ('open-member', 'wait-member', 'close') for event in events)
    assert events.count(('terminate-job', 101)) == 1


@pytest.mark.parametrize('boundary', ['coverage', 'fence', 'membership'])
def test_complete_retry_preserves_lifetime_coverage_fence_and_member_authority(monkeypatch, boundary):
    job, kernel, leader, events = drain_job(monkeypatch)
    complete = ({'assigned': 0, 'listed': 0} if boundary == 'coverage' else
                {'assigned': 1, 'listed': 1, 'identifiers': (22,)})
    observed = membership_replies(monkeypatch, kernel, events, [
        {'assigned': 2, 'listed': 1, 'identifiers': (0,)}, complete,
    ])
    kernel.late_member = boundary == 'fence'
    kernel.member = boundary != 'membership'
    message = {
        'coverage': 'historical.*no retained exit proof',
        'fence': 'membership changed across termination',
        'membership': 'membership changed during shutdown admission',
    }[boundary]
    with pytest.raises(OSError, match=message):
        job.close(leader=leader)
    assert len(observed) == 2 and job.handle == 101 and job.port == 201
    assert ('close', 101) not in events and ('close', 201) not in events
    assert not any(event[0] == 'wait-member' for event in events)
    assert job._shutdown_total == (None if boundary == 'membership' else 2)
    if boundary == 'fence':
        assert job._members == {22: (1022, True)}
        assert ('close', 1022) not in events
    with pytest.raises(OSError, match=message):
        job.close(leader=leader)
    assert len(observed) == 2  # No requery/rebasing after termination.


@pytest.mark.parametrize('winerror', [5, 234])
def test_membership_query_api_error_is_captured_and_never_retried(monkeypatch, winerror):
    job, kernel, leader, events = drain_job(monkeypatch)
    observed = membership_replies(monkeypatch, kernel, events, [
        {'assigned': 2, 'listed': 1, 'identifiers': (0,),
         'returned_length': module._ProcessIds.processes.offset, 'winerror': winerror},
    ])
    original_error = job.api.error
    def native_error():
        events.append(('capture-error', kernel.error_code))
        return original_error()
    monkeypatch.setattr(job.api, 'error', native_error)
    with pytest.raises(OSError, match='native drain failure') as failure:
        job.close(leader=leader)
    assert failure.value.winerror == winerror
    assert events.index(('capture-error', winerror)) < events.index(('terminate-job', 101))
    assert len(observed) == 1 and job._admission_failure is failure.value
    assert job._membership_query == {
        'attempt': 1, 'api_success': 0, 'assigned': 2, 'listed': 1,
        'capacity': module._MAX_JOB_MEMBERS, 'buffer_bytes': ctypes.sizeof(module._ProcessIds),
        'returned_length': module._ProcessIds.processes.offset, 'winerror': winerror,
    }
    assert str(job._membership_query) in failure.value.__notes__[-1]
    assert not any(event[0] in ('open-member', 'wait-member', 'close') for event in events)
    with pytest.raises(OSError) as again:
        job.close(leader=leader)
    assert again.value is failure.value and len(observed) == 1
    assert job.handle == 101 and job.port == 201


def test_successful_membership_query_records_returned_length_without_reading_stale_error(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.error_code = 87  # Deliberately stale native error after a successful query.
    original_error = job.api.error
    def no_error_read():
        raise AssertionError('Successful QueryInformationJobObject must not read LastError.')
    monkeypatch.setattr(job.api, 'error', no_error_read)
    try:
        value = job._query(3, module._ProcessIds())
        assert value.assigned == value.listed == 1 and value.processes[0] == 22
        assert job._membership_query == {
            'attempt': 1, 'api_success': 1, 'assigned': 1, 'listed': 1,
            'capacity': module._MAX_JOB_MEMBERS, 'buffer_bytes': ctypes.sizeof(module._ProcessIds),
            'returned_length': module._ProcessIds.processes.offset + ctypes.sizeof(ctypes.c_size_t),
            'winerror': None,
        }
    finally:
        monkeypatch.setattr(job.api, 'error', original_error)
    assert job.close(leader=leader)
    assert job.handle is None and job.port is None


def _add_members_across_termination(monkeypatch, kernel, *, pids=(33,),
                                    increment=None, notices=None):
    """Only the real owner logic is under test; native replies are explicit."""
    original = kernel.TerminateJobObject
    introduced = []
    def terminate(handle, status):
        result = original(handle, status)
        if result and not introduced:
            introduced.extend(pids)
            kernel.total += len(pids) if increment is None else increment
            kernel.identifiers = ()  # Empty active lists never establish exit.
            kernel.notifications.extend(
                [(6, pid, 101) for pid in pids] if notices is None else notices)
        return result
    monkeypatch.setattr(kernel, 'TerminateJobObject', terminate)
    return introduced


def test_late_verified_handles_extend_complete_proof_then_require_actual_waits(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    assert job.close(leader=leader) is True
    assert job._shutdown_total == 3
    assert job._shutdown_members == frozenset({11, 22, 33})
    assert job._completed_members == {22, 33}
    assert job.handle is None and job.port is None and not job._members
    assert events.count(('query', 3)) == 1
    for pid in (22, 33):
        assert events.count(('open-member', pid)) == 1
        admitted = events.index(('membership', 1000 + pid))
        waited = next(i for i, event in enumerate(events)
                      if event[0] == 'wait-member' and event[1] == 1000 + pid)
        assert admitted < waited < events.index(('close', 1000 + pid))
        assert events.index(('close', 1000 + pid)) < events.index(('close', 101))
    assert events.index(('terminate-job', 101)) < events.index(('open-member', 33))


def test_late_member_timeout_retains_augmented_proof_and_same_native_handle(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    original_wait = kernel.WaitForSingleObject
    released = False
    def wait(handle, milliseconds):
        if handle == 1033 and not released:
            events.append(('wait-member', handle, milliseconds))
            return 0x102
        return original_wait(handle, milliseconds)
    monkeypatch.setattr(kernel, 'WaitForSingleObject', wait)
    with pytest.raises(TimeoutError, match='process has not confirmed exit'):
        job.close(leader=leader)
    assert kernel.active == 0 and kernel.identifiers == ()
    assert job._shutdown_total == 3 and job._shutdown_members == frozenset({11, 22, 33})
    assert job._members == {33: (1033, True)} and job._completed_members == {22}
    assert job.handle == 101 and job.port == 201
    assert ('close', 1033) not in events and ('close', 101) not in events
    released = True
    assert job.close(leader=leader) is True
    assert job.handle is None and job.port is None and job._completed_members == {22, 33}
    assert events.count(('open-member', 33)) == 1
    assert events.count(('query', 3)) == 1


@pytest.mark.parametrize('failure', ['wait', 'close'])
def test_late_member_native_failure_never_releases_the_exact_job(monkeypatch, failure):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    original_wait, original_close = kernel.WaitForSingleObject, kernel.CloseHandle
    released = False
    def wait(handle, milliseconds):
        if handle == 1033 and not released and failure == 'wait':
            events.append(('wait-member', handle, milliseconds))
            return 0xFFFFFFFF
        return original_wait(handle, milliseconds)
    def close(handle):
        if handle == 1033 and not released and failure == 'close':
            events.append(('failed-close-late', handle))
            return False
        return original_close(handle)
    monkeypatch.setattr(kernel, 'WaitForSingleObject', wait)
    monkeypatch.setattr(kernel, 'CloseHandle', close)
    with pytest.raises(OSError, match='native drain failure'):
        job.close(leader=leader)
    assert job._shutdown_total == 3 and job._members == {33: (1033, True)}
    assert job.handle == 101 and job.port == 201 and ('close', 101) not in events
    assert job._completed_members == ({22, 33} if failure == 'close' else {22})
    released = True
    assert job.close(leader=leader) is True
    assert events.count(('open-member', 33)) == 1
    assert events.count(('close', 1033)) == 1 and events.count(('query', 3)) == 1


@pytest.mark.parametrize('notice', ['missing', 'exit', 'zero', 'duplicate-old'])
def test_lifetime_growth_without_new_retained_objects_remains_owned(monkeypatch, notice):
    job, kernel, leader, events = drain_job(monkeypatch)
    notices = {
        'missing': [], 'exit': [(7, 33, 101)], 'zero': [(4, None, 101)],
        'duplicate-old': [(6, 22, 101), (6, 22, 101)],
    }[notice]
    _add_members_across_termination(monkeypatch, kernel, notices=notices)
    with pytest.raises(OSError, match='membership changed across termination') as caught:
        job.close(leader=leader)
    assert job._shutdown_total == 2 and job._shutdown_members == frozenset({11, 22})
    assert kernel.active == 0 and kernel.identifiers == ()
    assert job._members == {22: (1022, True)} and not job._completed_members
    assert job.handle == 101 and job.port == 201
    assert ('open-member', 33) not in events and ('close', 101) not in events
    assert not any(event[0] == 'wait-member' for event in events)
    assert "'admitted_total': 2" in caught.value.__notes__[0]
    assert "'current_total': 3" in caught.value.__notes__[0]
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert events.count(('query', 3)) == 1


@pytest.mark.parametrize('winerror', [87, 5], ids=['notified-but-gone', 'access-denied'])
def test_late_open_failure_cannot_add_a_process_object_proof(monkeypatch, winerror):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    original_open = kernel.OpenProcess
    def open_process(access, inherit, pid):
        if pid == 33:
            assert access == 0x00101000 and inherit is False
            events.append(('open-member', pid))
            kernel.error_code = winerror
            return 0
        return original_open(access, inherit, pid)
    monkeypatch.setattr(kernel, 'OpenProcess', open_process)
    message = 'membership changed across termination' if winerror == 87 else 'native drain failure'
    with pytest.raises(OSError, match=message):
        job.close(leader=leader)
    assert job._shutdown_total == 2 and job._shutdown_members == frozenset({11, 22})
    assert job._members == {22: (1022, True)}
    assert job._completed_members == ({33} if winerror == 87 else set())
    assert job.handle == 101 and job.port == 201
    assert not any(event[0] == 'wait-member' for event in events)
    assert ('close', 101) not in events and ('close', 201) not in events
    # The historical-only notice is still insufficient on a later close.
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert job.handle == 101 and events.count(('query', 3)) == 1


def test_recycled_late_pid_never_counts_as_a_verified_owned_object(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    original_membership = kernel.IsProcessInJob
    def membership(handle, expected_job, pointer):
        if handle == 1033:
            events.append(('membership', handle))
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = 0
            return True
        return original_membership(handle, expected_job, pointer)
    monkeypatch.setattr(kernel, 'IsProcessInJob', membership)
    with pytest.raises(OSError, match='membership changed during shutdown admission'):
        job.close(leader=leader)
    assert job._shutdown_total == 2 and job._shutdown_members == frozenset({11, 22})
    assert job._members == {22: (1022, True)} and not job._completed_members
    assert events.count(('close', 1033)) == 1  # Only the foreign query handle is closed.
    assert not any(event[0] == 'wait-member' for event in events)
    assert job.handle == 101 and job.port == 201
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert events.count(('open-member', 33)) == 1 and ('close', 101) not in events


@pytest.mark.parametrize('increment', [-1, 2, module._MAX_JOB_MEMBERS],
                         ids=['decreasing-total', 'uncovered-additional-member', 'over-cap'])
def test_late_exact_handle_does_not_excuse_invalid_or_incomplete_lifetime_count(
        monkeypatch, increment):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel, increment=increment)
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert job._shutdown_total == 2 and job._shutdown_members == frozenset({11, 22})
    assert job._members == {22: (1022, True), 33: (1033, True)}
    assert job.handle == 101 and job.port == 201
    assert not any(event[0] == 'wait-member' for event in events)
    assert ('close', 101) not in events and events.count(('query', 3)) == 1


def test_late_exact_handles_cannot_repair_an_incomplete_original_lifetime_proof(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    kernel.total = 3  # Leader plus member22 plus one unobserved historical member.
    _add_members_across_termination(monkeypatch, kernel)
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert job._shutdown_total == 3 and job._shutdown_members == frozenset({11, 22})
    assert kernel.total == 4 and job._members == {22: (1022, True), 33: (1033, True)}
    assert job.handle == 101 and job.port == 201
    assert not any(event[0] == 'wait-member' for event in events)
    assert ('close', 101) not in events and events.count(('query', 3)) == 1


def test_delayed_new_object_notice_can_supply_complete_proof_on_retry(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel, notices=[])
    with pytest.raises(OSError, match='membership changed across termination'):
        job.close(leader=leader)
    assert job._shutdown_total == 2 and job._members == {22: (1022, True)}
    kernel.notifications.append((6, 33, 101))
    assert job.close(leader=leader) is True
    assert job._shutdown_total == 3 and job._completed_members == {22, 33}
    assert job.handle is None and job.port is None
    assert events.count(('query', 3)) == 1
    assert events.count(('open-member', 22)) == events.count(('open-member', 33)) == 1


def test_a_second_verified_growth_keeps_previously_completed_objects_on_retry(monkeypatch):
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    original_wait = kernel.WaitForSingleObject
    released = False
    def wait(handle, milliseconds):
        if handle == 1033 and not released:
            events.append(('wait-member', handle, milliseconds))
            return 0x102
        return original_wait(handle, milliseconds)
    monkeypatch.setattr(kernel, 'WaitForSingleObject', wait)
    with pytest.raises(TimeoutError, match='process has not confirmed exit'):
        job.close(leader=leader)
    assert job._completed_members == {22} and job._shutdown_total == 3
    kernel.total += 1
    kernel.notifications.append((6, 44, 101))
    released = True
    assert job.close(leader=leader) is True
    assert job._shutdown_members == frozenset({11, 22, 33, 44})
    assert job._completed_members == {22, 33, 44}
    assert job.handle is None and job.port is None and not job._members
    for pid in (22, 33, 44):
        assert events.count(('open-member', pid)) == 1
    assert events.count(('query', 3)) == 1


def test_late_object_retention_and_waits_consume_the_original_shutdown_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    job, kernel, leader, events = drain_job(monkeypatch)
    _add_members_across_termination(monkeypatch, kernel)
    original_open = kernel.OpenProcess
    def open_process(access, inherit, pid):
        if pid == 33:
            now[0] += 4.8
        return original_open(access, inherit, pid)
    monkeypatch.setattr(kernel, 'OpenProcess', open_process)
    def waited(milliseconds):
        assert 0 <= milliseconds <= 200
        now[0] += .3
    kernel.on_wait = waited
    with pytest.raises(TimeoutError, match='job has not confirmed shutdown'):
        job.close(leader=leader)
    assert job._shutdown_total == 3 and job.handle == 101 and job.port == 201
    assert job._completed_members == {22} and job._members == {33: (1033, True)}
    assert ('close', 1033) not in events and ('close', 101) not in events
    assert events.count(('query', 3)) == 1
