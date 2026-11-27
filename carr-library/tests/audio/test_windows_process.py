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

from carr_library.audio import engines
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


@pytest.mark.parametrize('exited', [False, True])
def test_windows_stop_closes_owned_job_even_after_helper_exit(monkeypatch, exited):
    from carr_library.audio import recording_process

    calls = []
    monkeypatch.setattr(engines, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(recording_process, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(recording_process, 'time', SimpleNamespace(
        monotonic=lambda: 10.0,
        sleep=lambda seconds: calls.append(('sleep', seconds))))
    monkeypatch.setattr(engines.subprocess, 'run',
                        lambda *args, **kwargs: pytest.fail('Cleanup must not launch a PID-based tree killer.'))

    # The current owner must retain the job while a descendant remains active,
    # including when the leader had already exited before cancellation began.
    active = iter((1, 0))

    class Query:
        def __call__(self, handle, kind, pointer, size, returned):
            assert handle == 101 and kind == 1 and returned is None
            assert process._carr_job.handle == handle
            assert size == ctypes.sizeof(recording_process._Accounting)
            accounting = ctypes.cast(
                pointer, ctypes.POINTER(recording_process._Accounting)).contents
            accounting.active_processes = next(active)
            calls.append(('active-processes', accounting.active_processes))
            return True

    class Kernel:
        QueryInformationJobObject = Query()

        def TerminateJobObject(self, handle, status):
            assert handle == 101 and status == 1
            calls.append(('terminate-job', handle, status))
            if process.returncode is None:
                process.returncode = 1
            return True

        def CloseHandle(self, handle):
            assert handle == 101 and process._carr_job.handle == handle
            assert calls[-1] == ('active-processes', 0)
            calls.append(('close-job', handle))
            return True

    class Process:
        returncode = 0 if exited else None
        _handle = SimpleNamespace(Close=lambda: calls.append('leader-handle'))

        def poll(self):
            return self.returncode

        def kill(self):
            pytest.fail('Cleanup must stop the retained job, not a leader PID.')

        def wait(self, *, timeout):
            assert timeout == 5 and self.returncode is not None
            calls.append(('wait', timeout))
            return self.returncode

    process = Process()
    process._carr_job = SimpleNamespace(handle=101, api=SimpleNamespace(
        kernel=Kernel(), error=lambda: AssertionError('Synthetic native API failed.')),
        close=lambda: pytest.fail('The retained job must not close before native accounting settles.'))
    engines._stop(process)
    assert calls == [
        ('terminate-job', 101, 1), ('wait', 5), 'leader-handle',
        ('active-processes', 1), ('sleep', .02), ('active-processes', 0),
        ('close-job', 101)]
    assert process._carr_job.handle is None
    assert process.returncode == (0 if exited else 1)


@pytest.mark.parametrize('failure', ['reader', 'writer'])
def test_stream_thread_start_failure_stops_actual_worker(tmp_path, monkeypatch, failure):
    original_thread, original_start = engines.threading.Thread, engines._start_process
    processes = []

    def thread(*args, **kwargs):
        result = original_thread(*args, **kwargs)
        if failure in kwargs['name']:
            def fail():
                raise RuntimeError('thread startup failure')
            result.start = fail
        return result

    def launch(*args, **kwargs):
        result = original_start(*args, **kwargs)
        processes.append(result)
        return result

    monkeypatch.setattr(engines.threading, 'Thread', thread)
    monkeypatch.setattr(engines, '_start_process', launch)
    engine = object.__new__(engines.DordiaiVoicesEngine)
    engine.manifest = SimpleNamespace(root=tmp_path)
    engine.timeout = 5
    try:
        with pytest.raises(RuntimeError, match='thread startup failure'):
            engine._stream([str(Path(sys.executable).resolve()), '-I', '-c', 'import time;time.sleep(30)'],
                           {'text': 'Synthetic startup failure'}, tmp_path / 'discard.wav', lambda: False)
        assert len(processes) == 1
        assert processes[0].poll() is not None, 'Failed thread startup leaked the actual worker.'
        assert processes[0].stdin.closed and processes[0].stdout.closed
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()


@pytest.mark.skipif(os.name != 'nt', reason='Requires native Windows process handles.')
@pytest.mark.parametrize('boundary', ['run', 'dordiai_stream'])
def test_windows_cancellation_stops_descendant_after_its_helper_exits(tmp_path, monkeypatch, boundary):
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    synchronize, process_terminate = 0x00100000, 0x0001
    wait_object, wait_timeout = 0, 0x00000102
    record = tmp_path / 'windows-child.json'
    code = '''
import json, subprocess, sys
from pathlib import Path
child = subprocess.Popen(
    [sys.executable, '-I', '-c', 'import time;time.sleep(30)'],
    stdout=sys.stdout, stderr=sys.stderr,
    creationflags=subprocess.CREATE_NO_WINDOW)
record = Path(sys.argv[1])
stage = record.with_suffix('.tmp')
stage.write_text(json.dumps({'pid': child.pid}), encoding='utf-8')
stage.replace(record)
'''
    started = []
    original_start = engines._start_process
    child_handle = None

    def launch(*args, **kwargs):
        process = original_start(*args, **kwargs)
        started.append(process)
        return process

    def cancelled():
        nonlocal child_handle
        if not started or not record.exists():
            return False
        if child_handle is None:
            pid = json.loads(record.read_text(encoding='utf-8'))['pid']
            child_handle = kernel.OpenProcess(synchronize | process_terminate, False, pid)
            if not child_handle:
                raise ctypes.WinError(ctypes.get_last_error())
        if started[0].poll() is None:
            return False
        assert kernel.WaitForSingleObject(child_handle, 0) == wait_timeout, \
            'The fixture descendant must still be alive when cancellation begins.'
        return True

    monkeypatch.setattr(engines, '_start_process', launch)
    try:
        command = [Path(sys.executable).resolve(), '-I', '-c', code, record]
        with pytest.raises(InterruptedError):
            if boundary == 'run':
                engines._run(command, cancelled=cancelled, timeout=10)
            else:
                engine = object.__new__(engines.DordiaiVoicesEngine)
                engine.manifest = SimpleNamespace(root=tmp_path)
                engine.timeout = 10
                engine._stream([str(item) for item in command], {'text': 'Synthetic cleanup test'},
                               tmp_path / 'discard.wav', cancelled)
        assert child_handle and started[0].poll() is not None
        assert kernel.WaitForSingleObject(child_handle, 2000) == wait_object, \
            'Cancellation left the exited Windows helper\'s owned descendant alive.'
    finally:
        if child_handle:
            try:
                if kernel.WaitForSingleObject(child_handle, 0) == wait_timeout:
                    terminated = kernel.TerminateProcess(child_handle, 1)
                    if not terminated and kernel.WaitForSingleObject(child_handle, 0) != wait_object:
                        raise ctypes.WinError(ctypes.get_last_error())
                    assert kernel.WaitForSingleObject(child_handle, 5000) == wait_object
            finally:
                kernel.CloseHandle(child_handle)


@pytest.mark.skipif(os.name != 'nt', reason='Requires actual concurrent Windows processes.')
def test_native_concurrent_unrelated_process_survives_owned_cancellation(tmp_path, monkeypatch):
    assigned, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original_assign = module._Job.assign
    outcomes = []
    unrelated = None

    def pause_assignment(self, process):
        original_assign(self, process)
        assigned.set()
        if not release.wait(10):
            raise TimeoutError('Native test did not release suspended launch.')

    monkeypatch.setattr(module._Job, 'assign', pause_assignment)

    def run():
        try:
            engines._run([Path(sys.executable).resolve(), '-I', '-c', 'import time;time.sleep(30)'],
                         cancelled=cancelled.is_set, timeout=20)
        except BaseException as error:
            outcomes.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert assigned.wait(10)
        marker = tmp_path / 'unrelated-ready.txt'
        code = 'from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text("ready");time.sleep(30)'
        unrelated = subprocess.Popen([sys.executable, '-I', '-c', code, str(marker)],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists(), 'An unrelated concurrent process was suspended or captured.'
        release.set()
        cancelled.set()
        worker.join(10)
        assert not worker.is_alive()
        assert len(outcomes) == 1 and isinstance(outcomes[0], InterruptedError)
        assert unrelated.poll() is None, 'Owned cancellation terminated an unrelated process.'
    finally:
        release.set()
        cancelled.set()
        worker.join(10)
        if unrelated is not None:
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)
            unrelated._handle.Close()
