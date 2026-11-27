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

