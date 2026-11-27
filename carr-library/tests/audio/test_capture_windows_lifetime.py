"""Actual Windows owner-death barriers; require a strict zero-skip Windows gate.

The child is a fixed synthetic command, not FFmpeg or a physical microphone.
The post-admission case retains the prepared upstream Job Object regression.
The pre-admission case checks the newly identified unowned-start interval.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.audio import process as module


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows capture ownership requires Windows.')


def _kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, arguments, returned in (
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ('TerminateProcess', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL)):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, returned
    return kernel


def _owner_death(tmp_path, *, before_admission):
    activated = tmp_path / 'capture.pid'
    launcher = tmp_path / 'launcher.pid'
    ready = tmp_path / 'owner-ready'
    release = tmp_path / 'release-owner'
    child_code = (
        'import os,time; from pathlib import Path; '
        f'Path({str(activated)!r}).write_text(str(os.getpid())); time.sleep(30)')
    # Package root comes from the actual imported application, including when
    # the test runs outside a checkout against a sealed installed wheel.
    package_root = str(Path(module.__file__).resolve().parents[2])
    owner_code = f"""
import os,sys,time
from pathlib import Path
sys.path.insert(0, {package_root!r})
from carr_library.audio import process as module
real_protect = module._capture_lifetime
def depart():
    Path({str(ready)!r}).write_text('ready')
    deadline = time.monotonic() + 10
    while not Path({str(release)!r}).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Owner-death barrier was not released')
        time.sleep(.01)
    os._exit(23)
def protect(child):
    Path({str(launcher)!r}).write_text(str(child.pid))
    if {before_admission!r}:
        depart()
    return real_protect(child)
module._capture_lifetime = protect
def cancelled():
    if not {before_admission!r} and Path({str(activated)!r}).exists():
        depart()
    return False
module.run_process(
    [{str(Path(sys.executable).resolve())!r}, '-I', '-B', '-c', {child_code!r}],
    stop_requested=lambda: False, cancelled=cancelled)
"""
    owner = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', '-c', owner_code],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    kernel, handles = _kernel(), []
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'The actual owner did not reach its controlled lifetime boundary.'
        identities = {int(launcher.read_text())}
        if not before_admission:
            assert activated.exists()
            identities.add(int(activated.read_text()))
        # Hold exact process handles before owner death, avoiding PID reuse or
        # "not found" as a substitute for observing actual process termination.
        for identity in identities:
            handle = kernel.OpenProcess(0x00100000 | 0x0001, False, identity)
            assert handle, ctypes.get_last_error()
            handles.append(handle)
        release.write_text('exit')
        stdout, stderr = owner.communicate(timeout=5)
        assert owner.returncode == 23, (stdout, stderr)
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        if before_admission:
            assert not activated.exists(), 'Unowned capture activated before its owner exited.'
    finally:
        release.write_text('cleanup')
        if owner.poll() is None:
            owner.kill()
        try:
            owner.communicate(timeout=5)
        finally:
            for handle in handles:
                kernel.TerminateProcess(handle, 99)  # Reap owned fixtures even on regression.
                kernel.WaitForSingleObject(handle, 5000)
                kernel.CloseHandle(handle)


def test_windows_owner_exit_before_admission_never_activates_capture(tmp_path):
    _owner_death(tmp_path, before_admission=True)


def test_windows_capture_is_killed_after_abrupt_owner_exit(tmp_path):
    _owner_death(tmp_path, before_admission=False)


class _ObservedFunction:
    """Delegate to the real WinAPI while inspecting a cleanup boundary."""
    def __init__(self, function, *, before=None, after=None):
        object.__setattr__(self, '_function', function)
        object.__setattr__(self, '_before', before)
        object.__setattr__(self, '_after', after)

    def __setattr__(self, name, value):
        setattr(self._function, name, value)

    def __getattr__(self, name):
        return getattr(self._function, name)

    def __call__(self, *arguments):
        if self._before is not None:
            self._before(arguments)
        result = self._function(*arguments)
        if self._after is not None:
            self._after(result)
        return result


def test_windows_capture_cancel_waits_for_descendant_handles_before_job_release(
        tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from carr_library.audio import AudioBudget, AudioCancelled

    ready = tmp_path / 'capture-ready.json'
    grand_ready = tmp_path / 'grandchild-ready'
    held_file = tmp_path / 'capture.wav'
    grand_file = tmp_path / 'capture-child.wav'
    grand_code = (
        'import os,time; from pathlib import Path; '
        f'stream=open({str(grand_file)!r},"wb"); stream.write(b"grandchild"); stream.flush(); '
        f'Path({str(grand_ready)!r}).write_text("ready"); time.sleep(30)')
    child_code = f'''
import json,os,subprocess,sys,time
from pathlib import Path
stream=open({str(held_file)!r},"wb")
stream.write(b"unfinished capture"); stream.flush()
child=subprocess.Popen([sys.executable,"-I","-B","-c",{grand_code!r}],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
deadline=time.monotonic()+10
while not Path({str(grand_ready)!r}).exists():
    if time.monotonic()>=deadline:
        raise TimeoutError("The synthetic descendant did not open its file")
    time.sleep(.01)
ready=Path({str(ready)!r})
staging=ready.with_suffix(".pending")
staging.write_text(json.dumps([os.getpid(),child.pid]))
os.replace(staging,ready)
child.wait()
'''
    kernel = _kernel()
    actual_windll = ctypes.WinDLL
    observed = SimpleNamespace(job=None, states=[], handles=[])

    def created(handle):
        observed.job = handle

    def closing(arguments):
        if arguments[0] == observed.job and observed.handles:
            # Observe actual kernel handles before CloseHandle can request
            # asynchronous Job termination. The old implementation reaches
            # this point with live descendants, even if they exit soon after.
            observed.states.append([kernel.WaitForSingleObject(handle, 0)
                                    for handle in observed.handles])

    class KernelProxy:
        CreateJobObjectW = _ObservedFunction(kernel.CreateJobObjectW, after=created)
        CloseHandle = _ObservedFunction(kernel.CloseHandle, before=closing)

        def __getattr__(self, name):
            return getattr(kernel, name)

    proxy = KernelProxy()
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: proxy
                        if args[0].casefold() == 'kernel32' else actual_windll(*args, **kwargs))
    unrelated = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def cancelled():
        if not ready.exists():
            return False
        if not observed.handles:
            identities = json.loads(ready.read_text())
            assert len(identities) == 2
            for identity in identities:
                handle = kernel.OpenProcess(0x00100000 | 0x0001, False, identity)
                assert handle, ctypes.get_last_error()
                observed.handles.append(handle)
            assert all(kernel.WaitForSingleObject(handle, 0) == 258
                       for handle in observed.handles)
        return True

    try:
        with pytest.raises(AudioCancelled):
            module.run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', child_code],
                stop_requested=lambda: False, cancelled=cancelled, budget=AudioBudget(timeout=15))
        assert len(observed.handles) == 2
        assert observed.states == [[0, 0]], (
            'Capture cleanup released the Job before exact descendant process handles signaled',
            observed.states)
        assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in observed.handles)
        assert unrelated.poll() is None, 'Capture cleanup terminated an unrelated process.'
        # No retry, delay, or ignored permission error: files are available
        # immediately after the confirmed cleanup boundary.
        held_file.unlink()
        grand_file.unlink()
    finally:
        for handle in observed.handles:
            kernel.TerminateProcess(handle, 99)
            kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        if unrelated.poll() is None:
            unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize('finish_on_wait', [True, False], ids=['exit-in-progress', 'still-running'])
def test_windows_refused_stop_requires_bounded_exact_handle_exit(
        tmp_path, monkeypatch, finish_on_wait):
    """Control the exit race while retaining real Windows processes and waits.

    Only the failed stop request is injected. Successful cleanup must still
    wait for the actual process object to signal; a still-live object is never
    accepted merely because TerminateProcess reports access denied.
    """
    from types import SimpleNamespace

    ready = tmp_path / 'owned-child-ready'
    held_file = tmp_path / 'capture-owned.wav'
    code = (
        'import sys; from pathlib import Path; '
        f'stream=open({str(held_file)!r},"wb"); stream.write(b"pending"); stream.flush(); '
        f'Path({str(ready)!r}).write_text("ready"); '
        'assert sys.stdin.buffer.read(1) == b"x"; stream.close()')
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    kernel = _kernel()
    actual_windll = ctypes.WinDLL
    observed = SimpleNamespace(owned=None, job=None, refused=[], waits=[], closing=[])
    proof = kernel.OpenProcess(0x00100000 | 0x0001, False, child.pid)
    assert proof, ctypes.get_last_error()

    def opened(handle):
        observed.owned = handle
    def created(handle):
        observed.job = handle
    class RefusedStop(_ObservedFunction):
        def __call__(self, handle, exit_code):
            assert handle == observed.owned
            assert kernel.WaitForSingleObject(proof, 0) == 258
            observed.refused.append(handle)
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED during concurrent exit
            return 0
    class ObservedWait(_ObservedFunction):
        def __call__(self, handle, milliseconds):
            if handle == observed.owned and milliseconds:
                assert observed.refused and 0 < milliseconds <= 5000
                assert kernel.WaitForSingleObject(proof, 0) == 258
                with pytest.raises(PermissionError):
                    held_file.unlink()
                if finish_on_wait:
                    # Let the real child complete only once cleanup has entered
                    # its bounded wait, never after an immediate zero-time poll.
                    child.stdin.write(b'x')
                    child.stdin.flush()
                    child.stdin.close()
                value = self._function(handle, milliseconds)
                observed.waits.append(value)
                return value
            return self._function(handle, milliseconds)
    def closing(arguments):
        if arguments[0] == observed.job:
            observed.closing.append(kernel.WaitForSingleObject(proof, 0))
    class KernelProxy:
        OpenProcess = _ObservedFunction(kernel.OpenProcess, after=opened)
        CreateJobObjectW = _ObservedFunction(kernel.CreateJobObjectW, after=created)
        TerminateProcess = RefusedStop(kernel.TerminateProcess)
        WaitForSingleObject = ObservedWait(kernel.WaitForSingleObject)
        CloseHandle = _ObservedFunction(kernel.CloseHandle, before=closing)
        def __getattr__(self, name):
            return getattr(kernel, name)

    release = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: KernelProxy()
            if args[0].casefold() == 'kernel32' else actual_windll(*args, **kwargs))
        release = module._capture_lifetime(child)
        if finish_on_wait:
            release()
            assert observed.refused == [observed.owned]
            assert observed.waits == [0] and observed.closing == [0]
            assert kernel.WaitForSingleObject(proof, 0) == 0
            assert child.wait(5) == 0
            held_file.unlink()
        else:
            started = time.monotonic()
            with pytest.raises(module.CaptureCleanupError, match='confirm complete') as failure:
                release()
            assert time.monotonic() - started < 8
            assert observed.refused == [observed.owned] and observed.waits == [258]
            assert 'remains unconfirmed' in str(failure.value.__cause__)
            assert 'WinError 5' in str(failure.value.__cause__)
            # The fail-closed Job fallback can stop the child afterwards. It
            # does not turn this operation into an accepted cleanup receipt.
            assert kernel.WaitForSingleObject(proof, 5000) == 0
        release()  # Original idempotent handle ownership is preserved.
        assert len(observed.closing) == 1
    finally:
        if kernel.WaitForSingleObject(proof, 0) == 258:
            kernel.TerminateProcess(proof, 99)
        kernel.WaitForSingleObject(proof, 5000)
        if release is not None:
            try:
                release()
            except module.CaptureCleanupError:
                pass
        kernel.CloseHandle(proof)
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=5)
