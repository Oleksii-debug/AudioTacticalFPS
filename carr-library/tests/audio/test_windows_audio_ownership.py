"""Actual subprocess-family cleanup on the native operating system under test."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.audio.models import AudioBudget, AudioCancelled, AudioError
from carr_library.audio.process import run_process

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Native Win32 Job ownership requires Windows.')


CHILD = '''import os, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
(root / 'child.pid').write_text(str(os.getpid()))
while True:
    (root / 'heartbeat').write_text(str(time.monotonic()))
    time.sleep(.05)
'''

PARENT = '''import pathlib, subprocess, sys, time
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
child = subprocess.Popen([sys.executable, '-I', '-B', '-c', sys.argv[3], str(root)])
while not (root/'heartbeat').exists():
    time.sleep(.01)
if mode == 'orphan':
    raise SystemExit(0)
if mode == 'overflow':
    sys.stderr.write('x'*16384)
    sys.stderr.flush()
time.sleep(60)
'''


def _running(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    status = Path('/proc') / str(pid) / 'stat'
    if status.exists():
        return status.read_text().split()[2] != 'Z'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize('mode', ['cancel', 'timeout', 'overflow', 'orphan'])
def test_owned_descendant_cannot_outlive_parent_or_failure(tmp_path, mode):
    selected = Path(sys.executable).resolve()
    budget = AudioBudget(timeout=3 if mode == 'timeout' else 15,
                         max_log_bytes=1024, reserve_disk_bytes=0)
    def cancelled():
        return mode == 'cancel' and (tmp_path / 'heartbeat').exists()
    expected = AudioCancelled if mode == 'cancel' else AudioError
    started = time.monotonic()
    with pytest.raises(expected):
        run_process([str(selected), '-I', '-B', '-c', PARENT, str(tmp_path), mode, CHILD],
                    budget=budget, cancelled=cancelled, owned_windows_job=True, watch=(tmp_path,))
    assert time.monotonic() - started < 15
    pid = int((tmp_path / 'child.pid').read_text())
    deadline = time.monotonic() + 3
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not _running(pid), 'An owned inference descendant survived worker cleanup.'


def test_owned_process_preserves_exact_diagnostic_bytes_and_environment(tmp_path):
    result = run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c',
        "import os,sys;sys.stdout.buffer.write(os.environ['CARR_PROCESS_TEST'].encode());sys.stderr.buffer.write(b'err')"],
        owned_windows_job=True, cwd=tmp_path,
        env={**os.environ, 'CARR_PROCESS_TEST': 'контрольовані байти'},
        budget=AudioBudget(timeout=15, reserve_disk_bytes=0))
    assert result.stdout == 'контрольовані байти'.encode()
    assert result.stderr == b'err'
