"""Real child processes prove the adapter inherits the existing native family.

The executables here are Python fixtures. This does not qualify any Calibre
installation or document format; that is the separate acceptance script.
"""
import ctypes
import errno
import os
from pathlib import Path
import sys

import pytest

from carr_library.audio.capture_process import run_process, wait_for_cleanup
from carr_library.audio.capture_types import AudioBudget, AudioCleanupPending


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
        except (FileNotFoundError, ProcessLookupError):
            # procfs may report ESRCH after open when the process just exited.
            return False
        return text.rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    # The process may have exited between the first signal probe and stat.
    # An unavailable procfs alone is not evidence that a live PID disappeared.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize('mode', ['completed-leader', 'diagnostic-cap', 'adapter-timeout'])
def test_inner_converter_keeps_descendant_in_existing_outer_owner(tmp_path, mode):
    import carr_library
    package_root = str(Path(carr_library.__file__).resolve().parent.parent)
    ready = tmp_path / 'descendant.pid'
    child = (
        "import pathlib,subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(12)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid))\n"
        "mode=sys.argv[2]\n"
        "if mode=='diagnostic-cap':\n"
        " sys.stdout.buffer.write(b'x'*300000);sys.stdout.buffer.flush();time.sleep(8)\n"
        "elif mode=='adapter-timeout': time.sleep(8)\n"
    )
    outer = (
        "import pathlib,sys,time\n"
        "sys.path.insert(0,sys.argv[1])\n"
        "from carr_library.conversion.calibre import _run,_environment\n"
        "stage=pathlib.Path(sys.argv[2]);mode=sys.argv[3]\n"
        "real=time.monotonic;deadline=real()+5\n"
        "if mode=='adapter-timeout':\n"
        " time.monotonic=lambda: deadline if (stage/'descendant.pid').is_file() else real()\n"
        "try:\n"
        " _run([sys.executable,'-I','-B','-c',sys.argv[4],str(stage/'descendant.pid'),mode],"
        "stage,deadline,"
        "environment=_environment(stage,stage))\n"
        "except (ValueError,TimeoutError):\n"
        " assert mode!='completed-leader'\n"
        "else: assert mode=='completed-leader'\n"
        "assert (stage/'descendant.pid').is_file()\n"
        "print('adapter settled')\n"
    )
    args = [str(Path(sys.executable).resolve()), '-I', '-B', '-c', outer,
            package_root, str(tmp_path), mode, child]
    try:
        result = run_process(args, owned_tree=True, cwd=tmp_path, watch=(tmp_path,),
            budget=AudioBudget(timeout=15, max_log_bytes=1024 * 1024,
                               max_output_bytes=2 * 1024 * 1024))
    except AudioCleanupPending as error:
        wait_for_cleanup(error)
        raise
    assert 'adapter settled' in result.stdout.decode('utf-8')
    pid = int(ready.read_text())
    assert not _alive(pid), 'An inner Calibre-style child escaped the existing native family.'


@pytest.mark.parametrize('error_type, code, absent', [
    (FileNotFoundError, errno.ENOENT, True),
    (ProcessLookupError, errno.ESRCH, True),
    (PermissionError, errno.EACCES, False),
    (OSError, errno.EIO, False),
], ids=['gone-before-open', 'gone-during-read', 'permission-denied', 'io-failure'])
def test_process_observation_race_does_not_hide_other_read_errors(
        tmp_path, monkeypatch, error_type, code, absent):
    from types import SimpleNamespace

    pid = 12345
    status = tmp_path / str(pid) / 'stat'
    status.parent.mkdir()
    status.write_text(f'{pid} (observed process) S 0\n', encoding='utf-8')
    signals = []

    def observe(process, signal):
        assert (process, signal) == (pid, 0)
        signals.append((process, signal))

    # Exercise the POSIX observation branch on both CI platforms without
    # changing the process-wide os.name or sending a signal to an unrelated PID.
    monkeypatch.setitem(_alive.__globals__, 'os',
                        SimpleNamespace(name='posix', kill=observe))
    monkeypatch.setitem(_alive.__globals__, 'Path',
                        lambda name: tmp_path if name == '/proc' else pytest.fail(name))
    real_read = type(status).read_text
    failure = error_type(code, 'injected procfs observation boundary')

    def read_after_exit(path, *args, **kwargs):
        if path != status:
            return real_read(path, *args, **kwargs)
        assert signals == [(pid, 0)] and path.is_file()
        if absent:
            path.unlink()
        raise failure

    monkeypatch.setattr(type(status), 'read_text', read_after_exit)
    if absent:
        assert _alive(pid) is False
        assert not status.exists()
    else:
        with pytest.raises(error_type) as observed:
            _alive(pid)
        assert observed.value is failure
        assert status.is_file()
    assert signals == [(pid, 0)]


@pytest.mark.parametrize('proc_present, error_type, code, expected', [
    (True, ProcessLookupError, errno.ESRCH, False),
    (True, None, None, True),
    (False, None, None, True),
    (True, PermissionError, errno.EACCES, None),
], ids=['exit-before-stat', 'live-pid-without-stat', 'no-procfs-live-pid', 'denied-recheck'])
def test_missing_proc_stat_rechecks_the_same_pid_without_assuming_exit(
        tmp_path, monkeypatch, proc_present, error_type, code, expected):
    from types import SimpleNamespace

    pid = 12345
    proc = tmp_path / 'proc'
    status = proc / str(pid) / 'stat'
    if proc_present:
        status.parent.mkdir(parents=True)
        status.write_text(f'{pid} (observed process) S 0\n', encoding='utf-8')
    calls = []
    failure = error_type(code, 'injected second signal observation') if error_type else None

    def observe(process, signal):
        assert (process, signal) == (pid, 0)
        calls.append((process, signal))
        if len(calls) == 1 and proc_present:
            # The first signal observation succeeds, then the proc entry
            # disappears before the helper can inspect its state.
            status.unlink()
        elif len(calls) == 2 and failure is not None:
            raise failure

    monkeypatch.setitem(_alive.__globals__, 'os',
                        SimpleNamespace(name='posix', kill=observe))
    monkeypatch.setitem(_alive.__globals__, 'Path',
                        lambda name: proc if name == '/proc' else pytest.fail(name))
    if error_type is PermissionError:
        with pytest.raises(PermissionError) as observed:
            _alive(pid)
        assert observed.value is failure
    else:
        assert _alive(pid) is expected
    assert calls == [(pid, 0), (pid, 0)]
