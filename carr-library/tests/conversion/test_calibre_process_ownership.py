"""Real child processes prove the adapter inherits the existing native family.

The executables here are Python fixtures. This does not qualify any Calibre
installation or document format; that is the separate acceptance script.
"""
import ctypes
import os
from pathlib import Path
import sys

import pytest

from carr_library.audio.process import run_process
from carr_library.audio.models import AudioBudget, AudioCleanupPending


def _alive(pid):
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # No such process.
                return False
            raise ctypes.WinError(error)
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            assert kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path('/proc') / str(pid) / 'stat'
    if status.is_file():
        try:
            text = status.read_text()
        except FileNotFoundError:
            return False
        return text.rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    return True


@pytest.mark.parametrize('mode', ['completed-leader', 'diagnostic-cap', 'adapter-timeout'])
def test_inner_converter_keeps_descendant_in_existing_outer_owner(tmp_path, mode):
    import carr_library
    package_file = str(Path(carr_library.__file__).resolve())
    ready = tmp_path / 'descendant.pid'
    child = (
        "import os,pathlib,subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(12)'])\n"
        "marker=pathlib.Path(sys.argv[1]);temporary=marker.with_suffix('.tmp')\n"
        "temporary.write_text(str(p.pid));os.replace(temporary,marker)\n"
        "mode=sys.argv[2]\n"
        "if mode=='diagnostic-cap':\n"
        " sys.stdout.buffer.write(b'x'*300000);sys.stdout.buffer.flush();time.sleep(8)\n"
        "elif mode=='adapter-timeout': time.sleep(8)\n"
    )
    outer = (
        "import pathlib,sys,time,carr_library\n"
        "assert pathlib.Path(carr_library.__file__).resolve()==pathlib.Path(sys.argv[1])\n"
        "from carr_library.conversion.calibre import _run,_environment\n"
        "stage=pathlib.Path(sys.argv[2]);mode=sys.argv[3]\n"
        "real=time.monotonic;deadline=real()+5\n"
        "if mode=='adapter-timeout':\n"
        " time.monotonic=lambda: deadline if (stage/'descendant.pid').is_file() else real()\n"
        "try:\n"
        " _run([sys.executable,'-I','-c',sys.argv[4],str(stage/'descendant.pid'),mode],"
        "stage,deadline,"
        "environment=_environment(stage,stage))\n"
        "except (ValueError,TimeoutError):\n"
        " assert mode!='completed-leader'\n"
        "else: assert mode=='completed-leader'\n"
        "assert (stage/'descendant.pid').is_file()\n"
        "print('adapter settled')\n"
    )
    # Ordinary imports inherit the selected source/installed environment;
    # no insertion, interpreter resolution or intercepted child launch.
    args = [str(Path(sys.executable).absolute()), '-B', '-c', outer,
            package_file, str(tmp_path), mode, child]
    try:
        result = run_process(args, owned_tree=True, cwd=tmp_path, watch=(tmp_path,),
            budget=AudioBudget(timeout=15, max_log_bytes=1024 * 1024,
                               max_output_bytes=2 * 1024 * 1024))
    except AudioCleanupPending as error:
        assert error.cleanup_owner.quiescent_event.wait(20), 'Owned descendant cleanup remained pending.'
        raise
    assert 'adapter settled' in result.stdout.decode('utf-8')
    pid = int(ready.read_text())
    assert not _alive(pid), 'An inner Calibre-style child escaped the existing native family.'
