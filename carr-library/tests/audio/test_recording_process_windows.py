"""Actual Windows Job Object lifetime; selected only in Windows gates."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows capture Job Object lifetime requires Windows.')
def test_windows_capture_is_killed_after_abrupt_owner_exit(tmp_path):
    import ctypes
    from ctypes import wintypes
    from carr_library.audio import process as module

    marker = tmp_path / 'capture.pid'
    child_code = ('import os,time; from pathlib import Path; '
                  f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)')
    owner_code = ('import os,sys; from pathlib import Path; '
        'from carr_library.audio import process as module\n'
        f'assert Path(module.__file__).resolve() == Path({str(Path(module.__file__).resolve())!r})\n'
        'run_process = module.run_process\n'
        'def check():\n'
        f'    if Path({str(marker)!r}).exists(): os._exit(23)\n'
        '    return False\n'
        f'run_process([{str(Path(sys.executable).resolve())!r},"-I","-B","-c",{child_code!r}],'
        'stop_requested=lambda: False,cancelled=check)\n')
    result = subprocess.run([sys.executable, '-B', '-c', owner_code],
                            cwd=Path(module.__file__).resolve().parents[2], capture_output=True, timeout=10, check=False)
    assert result.returncode == 23, result.stderr.decode('utf-8', errors='replace')
    identity = int(marker.read_text())
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, arguments, returned in (
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL)):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, returned
    handle = kernel.OpenProcess(0x00100000 | 0x0001, False, identity)
    if not handle:
        assert ctypes.get_last_error() == 87  # The terminated process no longer exists.
    else:
        try:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
        finally:
            # Never kill by a marker PID: a reused PID could identify another
            # process. The synthetic child has its own fixed 30-second lifetime.
            kernel.CloseHandle(handle)

