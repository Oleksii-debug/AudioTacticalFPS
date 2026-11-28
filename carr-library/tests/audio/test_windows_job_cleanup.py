"""Controlled native API boundaries; actual family exit has separate OS tests.

These tests do not claim Windows execution. The unchanged Calibre nested-child
and owned-process suites wait on actual native process handles on Windows.
"""
import ctypes
import subprocess
import threading
from types import SimpleNamespace

import pytest

from carr_library.audio import windows_job as module


class Kernel:
    def __init__(self):
        self.active = [21]
        self.processes = {101: 11, 201: 21}
        self.signaled = {101}
        self.owned = {201}
        self.opened = []
        self.closed = []
        self.stopped = []
        self.waited = []
        self.bulk = []
        self.allow_exit = True
        self.fail_close = None
        self.wrong_member = False
        self.next_child = False

    def QueryInformationJobObject(self, job, kind, output, _size, _length):
        assert job == 100
        if kind == 1:
            ctypes.cast(output, ctypes.POINTER(module._Accounting)).contents.ActiveProcesses = len(self.active)
        else:
            assert kind == 3
            value = ctypes.cast(output, ctypes.POINTER(module._ProcessIds)).contents
            value.assigned = value.count = len(self.active)
            for index, identity in enumerate(self.active):
                value.identities[index] = identity
        return 1

    def GetProcessId(self, handle):
        return self.processes[handle]

    def OpenProcess(self, access, inherit, identity):
        assert access == 0x00100000 | 0x1000 | 0x0001 and inherit is False
        handle = next(key for key, value in self.processes.items() if value == identity)
        self.opened.append(handle)
        return handle

    def IsProcessInJob(self, handle, job, output):
        assert job == 100 and handle in self.processes
        ctypes.cast(output, ctypes.POINTER(module.BOOL)).contents.value = not self.wrong_member
        return 1

    def TerminateProcess(self, handle, code):
        assert handle in self.owned and code == 1
        self.stopped.append(handle)
        # Model the observed boundary: current membership disappears before
        # the retained process object signals. No grace sleep can prove exit.
        self.active = []
        if self.next_child and handle == 201:
            self.next_child = False
            self.processes[202] = 22
            self.owned.add(202)
            self.active = [22]
        return 1

    def TerminateJobObject(self, job, _code):
        assert job == 100
        self.bulk.append(job)
        self.active = []
        return 1

    def WaitForSingleObject(self, handle, milliseconds):
        assert handle not in self.closed
        self.waited.append((handle, milliseconds))
        if handle in self.signaled:
            return 0
        if milliseconds and self.allow_exit:
            assert handle in self.stopped
            self.signaled.add(handle)
            return 0
        return 258

    def GetExitCodeProcess(self, handle, output):
        assert handle in self.signaled
        ctypes.cast(output, ctypes.POINTER(module.DWORD)).contents.value = 17
        return 1

    def CloseHandle(self, handle):
        assert handle not in self.closed
        if self.fail_close == handle:
            self.fail_close = None
            return 0
        if handle == 100:
            assert self.owned <= self.signaled, 'Job released before exact descendants signaled'
        self.closed.append(handle)
        return 1


@pytest.fixture
def kernel(monkeypatch):
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, 'WinError',
                        lambda code: OSError('Synthetic WinError ' + str(code)), raising=False)
    return Kernel()


def owner(kernel, kind):
    if kind == 'audio':
        result = object.__new__(module.JobProcess)
        result._api, result._job = kernel, 100
        result._process = module._ProcessInfo()
        result._process.hProcess = 101
        result._process.hThread = 102
        result.args, result.returncode = ['synthetic.exe'], None
        result._cleanup_lock = threading.Lock()
    else:
        result = object.__new__(module.LifetimeJob)
        result.api = SimpleNamespace(kernel=kernel, error=lambda: OSError('Synthetic native error'))
        result.handle = 100
    result._member_handles = {}
    return result


@pytest.mark.parametrize('kind', ['audio', 'lifetime'])
def test_exact_descendant_is_waited_before_job_release_even_after_empty_membership(kernel, kind):
    selected = owner(kernel, kind)
    assert selected.close() is True
    assert kernel.opened == [201]
    assert kernel.stopped == [201]
    assert any(handle == 201 and milliseconds > 0 for handle, milliseconds in kernel.waited)
    assert kernel.closed.index(201) < kernel.closed.index(100)
    assert kernel.bulk == []
    assert selected._member_handles == {}
    assert selected.close() is True


@pytest.mark.parametrize('kind', ['audio', 'lifetime'])
def test_unconfirmed_exit_keeps_exact_handle_for_retry_when_pid_list_is_empty(kernel, kind):
    selected = owner(kernel, kind)
    kernel.allow_exit = False
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        selected.close()
    assert kernel.active == [] and kernel.opened == [201]
    assert kernel.closed == []
    assert selected._member_handles == {21: (201, True)}
    kernel.allow_exit = True
    assert selected.close() is True
    assert kernel.opened == [201], 'Retry must use its retained handle, not rediscover a PID'
    assert kernel.closed.count(201) == kernel.closed.count(100) == 1
    assert kernel.bulk == []


@pytest.mark.parametrize('kind', ['audio', 'lifetime'])
def test_failed_native_handle_release_remains_owned_until_retry(kernel, kind):
    selected = owner(kernel, kind)
    kernel.fail_close = 201
    with pytest.raises(OSError, match='Synthetic'):
        selected.close()
    assert selected._member_handles == {21: (201, True)}
    assert kernel.closed == [] and 201 in kernel.signaled
    assert selected.close() is True
    assert kernel.opened == [201]
    assert kernel.closed.count(201) == kernel.closed.count(100) == 1


@pytest.mark.parametrize('kind', ['audio', 'lifetime'])
def test_changed_membership_never_authorizes_stopping_foreign_handle(kernel, kind):
    selected = owner(kernel, kind)
    kernel.wrong_member = True
    with pytest.raises(OSError, match='identity changed'):
        selected.close()
    assert kernel.stopped == [] and kernel.closed == []
    assert selected._member_handles == {21: (201, False)}
    # The unrelated handle may only be released. The next enumeration is empty,
    # so no PID is reopened or silently reclassified as an owned process.
    kernel.active = []
    kernel.owned.clear()
    assert selected.close() is True
    assert kernel.stopped == [] and kernel.opened == [201]
    assert kernel.closed.count(201) == kernel.closed.count(100) == 1


@pytest.mark.parametrize('kind', ['audio', 'lifetime'])
def test_child_created_during_stop_is_captured_and_confirmed_in_same_job(kernel, kind):
    selected = owner(kernel, kind)
    kernel.next_child = True
    assert selected.close() is True
    assert kernel.opened == [201, 202]
    assert kernel.stopped == [201, 202]
    assert kernel.owned <= kernel.signaled
    assert kernel.closed.index(202) < kernel.closed.index(100)
    assert kernel.bulk == []


def test_audio_terminate_retains_job_until_explicit_close(kernel):
    selected = owner(kernel, 'audio')
    selected.terminate()
    assert kernel.owned <= kernel.signaled
    assert kernel.closed == [201]
    assert selected._job == 100 and selected._process.hProcess == 101
    assert selected.close() is True
    assert kernel.closed == [201, 101, 102, 100]
    assert kernel.bulk == []


def test_lifetime_borrows_assigned_root_without_closing_callers_handle(kernel):
    selected = owner(kernel, 'lifetime')
    selected.stop_and_wait(101)
    assert kernel.closed == [201]
    assert 101 not in kernel.closed
    assert selected.close() is True
    assert kernel.closed == [201, 100]


@pytest.mark.parametrize('kind', ['audio', 'lifetime'])
def test_incomplete_job_list_is_requeried_before_exact_members_are_stopped(kernel, kind, monkeypatch):
    selected = owner(kernel, kind)
    original = kernel.QueryInformationJobObject
    queries = []
    def incomplete_once(job, info, output, size, length):
        if info == 3 and not queries:
            queries.append('incomplete')
            value = ctypes.cast(output, ctypes.POINTER(module._ProcessIds)).contents
            value.assigned, value.count = 1, 0
            assert not kernel.opened and not kernel.stopped and not kernel.closed
            return 1
        queries.append('complete')
        return original(job, info, output, size, length)
    monkeypatch.setattr(kernel, 'QueryInformationJobObject', incomplete_once)
    assert selected.close() is True
    assert queries[0] == 'incomplete' and len(queries) >= 3
    assert kernel.opened == kernel.stopped == [201]
    assert kernel.closed.index(201) < kernel.closed.index(100)
    assert kernel.bulk == []


@pytest.mark.parametrize('counts', [(1, 0), (0, 1)])
def test_permanently_incomplete_job_list_retains_exact_owner_for_explicit_retry(kernel, counts, monkeypatch):
    import time
    selected = owner(kernel, 'audio')
    original = kernel.QueryInformationJobObject
    def incomplete(job, info, output, size, length):
        assert job == 100 and info == 3
        value = ctypes.cast(output, ctypes.POINTER(module._ProcessIds)).contents
        value.assigned, value.count = counts
        return 1
    monkeypatch.setattr(kernel, 'QueryInformationJobObject', incomplete)
    with pytest.raises(TimeoutError, match='complete process list'):
        module._stop_and_wait_members(kernel, selected._job, selected._member_handles,
            process=selected._process.hProcess, deadline=time.monotonic() + .03)
    assert selected._job == 100 and not kernel.opened and not kernel.stopped and not kernel.closed
    monkeypatch.setattr(kernel, 'QueryInformationJobObject', original)
    assert selected.close() is True
    assert kernel.opened == kernel.stopped == [201] and kernel.bulk == []


@pytest.mark.parametrize('counts', [(257, 0), (0, 257)])
def test_job_member_capacity_is_still_refused_without_stopping_or_releasing(kernel, counts, monkeypatch):
    selected = owner(kernel, 'audio')
    original = kernel.QueryInformationJobObject
    def oversized(job, info, output, size, length):
        assert job == 100 and info == 3
        value = ctypes.cast(output, ctypes.POINTER(module._ProcessIds)).contents
        value.assigned, value.count = counts
        return 1
    monkeypatch.setattr(kernel, 'QueryInformationJobObject', oversized)
    with pytest.raises(OSError, match='capacity=256'):
        selected.close()
    assert selected._job == 100 and not kernel.opened and not kernel.stopped and not kernel.closed
    monkeypatch.setattr(kernel, 'QueryInformationJobObject', original)
    assert selected.close() is True and kernel.bulk == []
