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
    calls = []
    class Kernel:
        def CreateJobObjectW(self, security, name):
            assert security is None and name is None
            return 101
        def SetInformationJobObject(self, job, kind, pointer, size):
            limits = ctypes.cast(pointer, ctypes.POINTER(module._ExtendedLimits)).contents
            calls.append(('limits', job, kind, size, limits.basic.flags))
            return configure
        def TerminateJobObject(self, job, status):
            calls.append(('terminate-job', job))
            return True
        def CloseHandle(self, job):
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
