"""Actual owned-process deadlines while caller-side validation is still running.

The child is an authored Python peer, not an ASR engine. No timeout, native
process result, held handle, or payload read is fabricated.
"""
from contextlib import contextmanager
from hashlib import sha256
import ctypes
import os
from pathlib import Path
import select
import sys
import threading
import time

import pytest

from carr_library.audio import process as module
from carr_library.audio.cleanup import retained_state_lock
from carr_library.audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError


OBSERVER = 'carr-audio-native-deadline'


def _wait(predicate, seconds=5):
    end = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError('The actual native test condition did not become observable.')
        threading.Event().wait(.01)


def _root_finished(child):
    if os.name == 'nt':
        return child.poll() is not None
    # The product owns reaping. Observe the exact unreaped child without
    # releasing the group leader's identity before its cleanup owner.
    return os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


@pytest.fixture
def native(monkeypatch):
    children, observers = [], []
    if os.name == 'nt':
        from carr_library.audio import windows_job
        original = windows_job.JobProcess
        def launch(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append((child, time.monotonic()))
            return child
        monkeypatch.setattr(windows_job, 'JobProcess', launch)
    else:
        original = module.subprocess.Popen
        def launch(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append((child, time.monotonic()))
            return child
        monkeypatch.setattr(module.subprocess, 'Popen', launch)
    start = threading.Thread.start
    def begin(thread):
        if thread.name == OBSERVER:
            observers.append(thread)
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', begin)
    yield children, observers
    # Every negative path above still uses the real product cleanup owner.
    # This last guard makes a regression fail without abandoning its child.
    for child, _ in children:
        if os.name == 'nt':
            child.close()
        elif child.returncode is None:
            try:
                os.killpg(child.pid, 9)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
    for thread in observers:
        if thread.ident is not None:
            thread.join(timeout=7)


def _execute(code, *, budget, **options):
    return module.run_process(
        [str(Path(sys.executable).resolve()), '-I', '-B', '-X', 'utf8', '-c', code],
        owned_tree=True, budget=AudioBudget(timeout=budget, reserve_disk_bytes=0), **options)


def _closed(native):
    children, observers = native
    assert len(children) == 1 and observers
    child = children[0][0]
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed
    assert all(not observer.is_alive() for observer in observers)
    if os.name == 'nt':
        assert child._job is None and not child._process.hProcess


def _slow_post_exit_verifier(native, path, budget, *, ending='success'):
    children, _ = native
    expected = sha256(path.read_bytes()).hexdigest()
    calls, verified = [], []
    def validate():
        calls.append(True)
        if not children or verified:
            return False
        child, started = children[0]
        _wait(lambda: _root_finished(child))
        # Keep this real caller validation outstanding past the unchanged
        # native deadline after observing the actual successful root exit.
        while time.monotonic() <= started + budget + .25:
            threading.Event().wait(.01)
        if ending == 'changed':
            path.write_bytes(b'Actual changed retained input')
        digest = sha256()
        with path.open('rb') as stream:
            while block := stream.read(65536):
                digest.update(block)
        verified.append(digest.hexdigest())
        if digest.hexdigest() != expected:
            raise ValueError('The retained validation payload changed.')
        return ending == 'cancel'
    return validate, calls, verified, expected


def test_timely_real_root_is_not_reclassified_by_slow_post_exit_validation(tmp_path, native):
    selected = tmp_path / 'retained-source'
    selected.write_bytes(b'Exact selected bytes\n' * 65536)
    budget = 2
    validate, calls, verified, expected = _slow_post_exit_verifier(native, selected, budget)
    result = _execute("import sys; sys.stdout.buffer.write(b'actual completed protocol\\n'); "
                      "sys.stdout.buffer.flush()", budget=budget, cancelled=validate)
    assert result.returncode == 0 and result.stdout == b'actual completed protocol\n'
    assert result.elapsed > budget and len(calls) >= 2
    assert verified == [expected] and sha256(selected.read_bytes()).hexdigest() == expected
    _closed(native)


@pytest.mark.parametrize('ending', ['cancel', 'changed'])
def test_post_exit_deadline_does_not_suppress_actual_cancel_or_changed_input(tmp_path, native, ending):
    selected = tmp_path / 'retained-source'
    selected.write_bytes(b'Exact retained bytes')
    validate, _, verified, expected = _slow_post_exit_verifier(native, selected, 2, ending=ending)
    failure = AudioCancelled if ending == 'cancel' else ValueError
    with pytest.raises(failure, match='cancelled|payload changed'):
        _execute("print('real native exit before validation')", budget=2, cancelled=validate)
    assert len(verified) == 1
    assert (verified[0] == expected) is (ending == 'cancel')
    _closed(native)


def test_live_root_hits_unchanged_native_bound_while_parent_callback_is_held(tmp_path, native):
    children, _ = native
    marker = tmp_path / 'started'
    observed = []
    code = ("from pathlib import Path; import time; "
            f"Path({str(marker)!r}).write_bytes(b'actual child'); time.sleep(30)")
    def validate():
        if not children or observed:
            return False
        child, started = children[0]
        _wait(marker.exists)
        assert not _root_finished(child)
        # The callback has not returned. Only the same owner's observer can
        # enforce the native deadline while this genuine caller work waits.
        _wait(lambda: _root_finished(child), seconds=5)
        observed.append(time.monotonic() - started)
        return False
    with pytest.raises(AudioError, match='time allowance'):
        _execute(code, budget=2, cancelled=validate)
    assert marker.read_bytes() == b'actual child'
    assert len(observed) == 1 and 1.8 <= observed[0] < 4
    _closed(native)


@contextmanager
def _member_exit_handle(pid, owner):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.IsProcessInJob.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
        kernel.IsProcessInJob.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        assert handle
        membership = ctypes.c_int()
        try:
            assert kernel.IsProcessInJob(handle, owner._job, ctypes.byref(membership)) and membership.value
            def exited():
                result = kernel.WaitForSingleObject(handle, 0)
                assert result in (0, 0x102)
                return result == 0
            yield exited
        finally:
            assert kernel.CloseHandle(handle)
    else:
        assert os.getpgid(pid) == owner.pid
        handle = os.pidfd_open(pid, 0)
        observer = select.poll()
        observer.register(handle, select.POLLIN)
        try:
            def exited():
                events = observer.poll(0)
                assert not any(flags & (select.POLLERR | select.POLLNVAL) for _, flags in events)
                return any(flags & (select.POLLIN | select.POLLHUP) for _, flags in events)
            yield exited
        finally:
            os.close(handle)


def test_timely_root_cannot_leave_native_descendant_running_during_validation(tmp_path, native):
    children, _ = native
    marker = tmp_path / 'member.pid'
    child = ("import os, pathlib, time; "
             f"target=pathlib.Path({str(marker)!r}); "
             "temporary=target.with_suffix('.pending'); "
             "temporary.write_text(str(os.getpid()), encoding='ascii'); temporary.replace(target); "
             "time.sleep(30)")
    parent = ("import subprocess, sys, time, pathlib; "
              f"subprocess.Popen([sys.executable,'-I','-B','-c',{child!r}], "
              "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
              f"marker=pathlib.Path({str(marker)!r}); "
              "\nwhile not marker.exists(): time.sleep(.01)\n")
    seen = []
    def validate():
        if not children or seen:
            return False
        owner, started = children[0]
        _wait(marker.exists)
        pid = int(marker.read_text(encoding='ascii'))
        with _member_exit_handle(pid, owner) as exited:
            assert not exited()
            _wait(lambda: _root_finished(owner))
            _wait(exited, seconds=5)
            seen.append(time.monotonic() - started)
        return False
    result = _execute(parent, budget=2, cancelled=validate)
    assert result.returncode == 0 and len(seen) == 1
    assert 1.8 <= seen[0] < 4
    _closed(native)


@pytest.mark.parametrize('after_start', [False, True])
def test_observer_start_failure_keeps_the_real_child_inside_cleanup(native, monkeypatch, after_start):
    start = threading.Thread.start
    attempts = []
    def reject(thread):
        if thread.name == OBSERVER:
            attempts.append(thread)
            if after_start:
                start(thread)
            raise RuntimeError('Authored native deadline observer startup refusal.')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', reject)
    with pytest.raises(RuntimeError, match='observer startup refusal'):
        _execute('import time; time.sleep(30)', budget=2)
    children, observers = native
    assert len(children) == 1 and attempts
    assert children[0][0].poll() is not None
    assert children[0][0].stdout.closed and children[0][0].stderr.closed
    assert all(not thread.is_alive() for thread in attempts)
    assert all(not thread.is_alive() for thread in observers)


def test_observer_native_query_failure_is_not_reported_as_success(native, monkeypatch):
    failures = []
    if os.name == 'nt':
        from carr_library.audio import windows_job
        original = windows_job.JobProcess
        # native fixture replaced the factory, so wrap the actual selected
        # object's method after constructing that same real Job owner.
        def launch(*args, **kwargs):
            child = original(*args, **kwargs)
            poll = child.poll
            def observed():
                if threading.current_thread().name == OBSERVER and not failures:
                    failures.append(True)
                    raise OSError('Authored exact-handle query failure.')
                return poll()
            child.poll = observed
            return child
        monkeypatch.setattr(windows_job, 'JobProcess', launch)
    else:
        original = os.waitid
        def observed(*args, **kwargs):
            if threading.current_thread().name == OBSERVER and not failures:
                failures.append(True)
                raise OSError('Authored WNOWAIT query failure.')
            return original(*args, **kwargs)
        monkeypatch.setattr(os, 'waitid', observed)
    with pytest.raises(AudioError, match='deadline observation failed'):
        _execute('import time; time.sleep(30)', budget=2)
    assert failures == [True]
    _closed(native)


def test_interrupted_observer_join_retains_the_same_owner_and_state_lock(tmp_path, native, monkeypatch):
    from filelock import FileLock, Timeout
    allowed = threading.Event()
    joined, original = [], threading.Thread.join
    def interrupted(thread, *args, **kwargs):
        if thread.name == OBSERVER and not allowed.is_set():
            joined.append(thread)
            if len(joined) == 1:
                raise KeyboardInterrupt('Authored observer join interruption.')
            raise OSError('The same observer join is still unavailable.')
        return original(thread, *args, **kwargs)
    monkeypatch.setattr(threading.Thread, 'join', interrupted)
    competitor = FileLock(tmp_path / '.deadline-test.lock', timeout=0)
    owner = None
    try:
        with pytest.raises(AudioCleanupPending) as raised:
            with retained_state_lock(tmp_path, 'deadline-test'):
                _execute("print('actual final native result')", budget=2)
        owner = raised.value.cleanup_owner
        assert joined and not owner.quiescent
        child = native[0][0][0]
        if os.name == 'nt':
            assert child._job is not None and child._process.hProcess
        else:
            assert child.returncode is None and _root_finished(child)
        with pytest.raises(Timeout):
            competitor.acquire()
        assert owner.close() is False
        allowed.set()
        assert owner.quiescent_event.wait(7)
        assert owner.close() is True
        with competitor:
            pass
        _closed(native)
    finally:
        allowed.set()
        if owner is not None:
            owner.close()
