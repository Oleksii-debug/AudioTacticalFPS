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


_NATIVE_CODEC_WORKER = r'''
import ctypes, json, pathlib, subprocess, sys, time
from ctypes import wintypes
job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
child = job / 'native_codec_fixture.py'
child.write_text(
    "import os,pathlib,sys,time\n"
    "root=pathlib.Path(sys.argv[1])\n"
    "(root/'native-child.pid').write_text(str(os.getpid()))\n"
    "deadline=time.monotonic()+45\n"
    "while time.monotonic()<deadline:\n"
    "    (root/'native-child-heartbeat').write_text(str(time.monotonic()))\n"
    "    time.sleep(.02)\n", encoding='utf-8')
class Startup(ctypes.Structure):
    _fields_ = [('cb',wintypes.DWORD),('lpReserved',wintypes.LPWSTR),
        ('lpDesktop',wintypes.LPWSTR),('lpTitle',wintypes.LPWSTR),
        ('dwX',wintypes.DWORD),('dwY',wintypes.DWORD),
        ('dwXSize',wintypes.DWORD),('dwYSize',wintypes.DWORD),
        ('dwXCountChars',wintypes.DWORD),('dwYCountChars',wintypes.DWORD),
        ('dwFillAttribute',wintypes.DWORD),('dwFlags',wintypes.DWORD),
        ('wShowWindow',wintypes.WORD),('cbReserved2',wintypes.WORD),
        ('lpReserved2',ctypes.POINTER(ctypes.c_ubyte)),
        ('hStdInput',wintypes.HANDLE),('hStdOutput',wintypes.HANDLE),
        ('hStdError',wintypes.HANDLE)]
class Process(ctypes.Structure):
    _fields_ = [('hProcess',wintypes.HANDLE),('hThread',wintypes.HANDLE),
                ('processId',wintypes.DWORD),('threadId',wintypes.DWORD)]
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.CreateProcessW.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,
    ctypes.c_void_p,ctypes.c_void_p,wintypes.BOOL,wintypes.DWORD,
    ctypes.c_void_p,wintypes.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)]
kernel.CreateProcessW.restype=wintypes.BOOL
kernel.CloseHandle.argtypes=[wintypes.HANDLE]
kernel.CloseHandle.restype=wintypes.BOOL
startup,process=Startup(),Process()
startup.cb=ctypes.sizeof(startup)
command=ctypes.create_unicode_buffer(subprocess.list2cmdline(
    [sys.executable,'-I','-B',str(child),str(job)]))
# Model the native child-codec launch seam, not a Python subprocess permission.
# The host's unchanged Python audit hook still forbids subprocess.Popen.
if not kernel.CreateProcessW(str(sys.executable),command,None,None,False,0,
                            None,str(job),ctypes.byref(startup),ctypes.byref(process)):
    raise ctypes.WinError(ctypes.get_last_error())
kernel.CloseHandle(process.hThread)
kernel.CloseHandle(process.hProcess)
# Deliberately ignore stop.flag: the host must terminate the actual native family.
time.sleep(45)
'''


@pytest.mark.parametrize('protocol', ['receipts-v1', 'whisper-v5-manifest-v1'])
def test_legacy_asr_stop_owns_actual_native_codec_and_resource_admission(tmp_path, protocol):
    from carr_library.application import LibraryApp
    from carr_library.operations import ResourceBroker, ResourceLimits
    from carr_library.transcription import TranscriptionService
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic Windows native-codec ownership')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    service = TranscriptionService(app, workspace, broker=broker)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    worker = runtime / 'worker.py'
    worker.write_text(_NATIVE_CODEC_WORKER, encoding='utf-8')
    for name in ('local_models.py', 'transcript_features.py'):
        (runtime / name).write_text('# Synthetic fixture identity; no inference.\n', encoding='utf-8')
    models = runtime / 'models'
    (models / 'small').mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (models / 'small' / name).write_bytes(b'Synthetic identity only.')
    source = tmp_path / 'selected.wav'
    source.write_bytes(b'Synthetic media, never decoded.')
    service.configure(Path(sys.executable).resolve(), worker, models, protocol=protocol)
    identity = service.submit([source])
    root = service._path(identity)

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.TerminateProcess.restype = ctypes.c_int32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int32
    owned, admitted = [], []

    def cancel_after_actual_child():
        if not (root / 'native-child-heartbeat').exists():
            return False
        if not owned:
            pid = int((root / 'native-child.pid').read_text(encoding='utf-8'))
            handle = kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
            assert handle, 'Could not retain the actual ready child process handle.'
            owned.append(handle)
            assert kernel.WaitForSingleObject(handle, 0) == 0x102
            leases = broker.snapshot()['leases']
            assert leases, 'The real native codec ran without ASR admission ownership.'
            admitted.append(leases)
        return True

    try:
        result = service.run(identity, cancelled=cancel_after_actual_child, timeout=20)
        assert owned and admitted, result
        assert result['status'] == 'stopped', result
        assert result['receipts'] == [] and result['artifacts'] == []
        assert kernel.WaitForSingleObject(owned[0], 0) == 0, (
            'The actual child codec survived Stop/resource-release.')
        assert not broker.snapshot()['leases']
        assert service.get(identity)['status'] == 'stopped'
    finally:
        # Retain/reuse the same process HANDLE, never terminate a bare PID
        # which Windows may already have assigned to an unrelated process.
        for handle in owned:
            try:
                if kernel.WaitForSingleObject(handle, 0) == 0x102:
                    assert kernel.TerminateProcess(handle, 1)
                    assert kernel.WaitForSingleObject(handle, 5000) == 0
            finally:
                assert kernel.CloseHandle(handle)
