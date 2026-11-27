"""Actual installer/health child lifetime under the external activation owner.

These native cases are a separate strict Windows gate. The command is a labelled
synthetic installer with real child/grandchild processes and held files; this is
not signed release, microphone or audio quality acceptance.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import carr_library
from carr_library import installation_lease as leases, portable_entry


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows activation Job ownership required')


_HELPER = r'''
import json, os, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from carr_library import installation_lease as leases, portable_entry
from carr_library.audio import windows_process
from carr_library.security import installation
from carr_library.security.handoff import _activation_owner

mode, selected, folder = sys.argv[2:]
program, root = pathlib.Path(selected), pathlib.Path(folder)
(root / 'helper-owner-pid').write_text(str(os.getpid()), encoding='utf-8')

def barrier(name, release):
    (root / name).write_text('ready', encoding='utf-8')
    deadline = time.monotonic() + 30
    while not (root / release).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('The native activation fixture barrier was not released.')
        time.sleep(.01)

if mode in ('normal', 'unconfirmed'):
    original_cleanup = windows_process._Job.stop_and_wait
    def cleanup(job, handle=None):
        barrier('cleanup-ready', 'release-cleanup')
        if mode == 'unconfirmed':
            raise TimeoutError('Synthetic unconfirmed activation descendant cleanup')
        return original_cleanup(job, handle)
    windows_process._Job.stop_and_wait = cleanup
elif mode == 'constructor-failure':
    original_process = windows_process.WindowsJobProcess
    def failed_constructor(*args, **kwargs):
        process = original_process(*args, **kwargs)
        barrier('constructor-ready', 'release-constructor')
        raise OSError('Synthetic post-create activation constructor failure')
    windows_process.WindowsJobProcess = failed_constructor

grand_code = """
import pathlib, sys, time
program, root = map(pathlib.Path, sys.argv[1:])
stream = (program / 'grandchild-held.bin').open('wb')
stream.write(b'owned grandchild'); stream.flush()
(root / 'grandchild-ready').write_text('ready')
time.sleep(30)
"""
child_code = """
import json, os, pathlib, subprocess, sys, time
program, root = map(pathlib.Path, sys.argv[1:3])
stream = (program / 'activation-held.bin').open('wb')
stream.write(b'owned activation child'); stream.flush()
child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', sys.argv[3],
                          str(program), str(root)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
deadline = time.monotonic() + 30
while not (root / 'grandchild-ready').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('The actual descendant did not open its file.')
    time.sleep(.01)
temporary = root / 'child-ready.pending'
temporary.write_text(json.dumps({'direct': os.getpid(), 'descendant': child.pid}))
os.replace(temporary, root / 'child-ready.json')
while not (root / 'release-worker').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('The actual activation worker was not released.')
    time.sleep(.01)
"""
control = portable_entry.control_root(program, create=True)
with leases.installation_lease(control, exclusive=True):
    portable_entry.require_clean_owners(program, control)
    with _activation_owner(program, control):
        result = installation._run_owned(
            [sys.executable, '-I', '-S', '-B', '-c', child_code,
             str(program), str(root), grand_code],
            cwd=program, env=dict(os.environ), timeout=25)
        if result.returncode != 0:
            raise RuntimeError('Synthetic activation worker failed.')
(root / 'completed').write_text('cleanup acknowledged', encoding='utf-8')
'''


def _kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
        'WaitForSingleObject': ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
        'TerminateProcess': ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        'CloseHandle': ((wintypes.HANDLE,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def _ready(path, owner):
    deadline = time.monotonic() + 15
    while not path.exists():
        if owner.poll() is not None:
            raise AssertionError('Actual helper exited before its barrier: ' +
                                 owner.stderr.read().decode(errors='replace'))
        if time.monotonic() >= deadline:
            raise AssertionError('Actual helper did not reach its bounded fixture barrier.')
        time.sleep(.01)
    return path


@pytest.mark.parametrize('mode', ['normal', 'unconfirmed', 'helper-death', 'constructor-failure'])
def test_activation_owner_never_confuses_direct_exit_with_descendant_cleanup(tmp_path, mode):
    assert sys.version_info[:2] == (3, 11) and ctypes.sizeof(ctypes.c_void_p) == 8, \
        'This strict gate requires native Windows x64 / Python 3.11.'
    program = tmp_path / 'selected portable program'
    program.mkdir()
    package_parent = Path(carr_library.__file__).resolve().parents[1]
    owner = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', '-c', _HELPER,
                              str(package_parent), mode, str(program), str(tmp_path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             close_fds=True)
    unrelated = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c',
                                  'import time; time.sleep(30)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
    kernel, handles = _kernel(), []
    owner_handle = None
    try:
        owner_pid = int(_ready(tmp_path / 'helper-owner-pid', owner).read_text(encoding='utf-8'))
        owner_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, owner_pid)
        assert owner_handle, ctypes.get_last_error()
        value = json.loads(_ready(tmp_path / 'child-ready.json', owner).read_bytes())
        for key in ('direct', 'descendant'):
            handle = kernel.OpenProcess(0x00100000 | 0x0001, False, value[key])
            assert handle, ctypes.get_last_error()
            handles.append(handle)
        assert all(kernel.WaitForSingleObject(handle, 0) == 258 for handle in handles)
        control = portable_entry.control_root(program)
        assert len(list(control.glob('owner-*.json'))) == 1
        with pytest.raises(RuntimeError, match='running jobs'):
            with leases.installation_lease(control, exclusive=True):
                pytest.fail('The external activation owner released its exclusive lease.')
        if mode in ('normal', 'unconfirmed'):
            (tmp_path / 'release-worker').touch()
            _ready(tmp_path / 'cleanup-ready', owner)
            assert kernel.WaitForSingleObject(handles[0], 0) == 0
            assert kernel.WaitForSingleObject(handles[1], 0) == 258
            # This assertion happens while an actual descendant is still live.
            with pytest.raises(RuntimeError, match='running jobs'):
                with leases.installation_lease(control, exclusive=True):
                    pytest.fail('Direct child exit was treated as complete activation cleanup.')
            (tmp_path / 'release-cleanup').touch()
        elif mode == 'constructor-failure':
            _ready(tmp_path / 'constructor-ready', owner)
            (tmp_path / 'release-constructor').touch()
        else:
            assert kernel.TerminateProcess(owner_handle, 99), ctypes.get_last_error()
        _output, errors = owner.communicate(timeout=15)
        assert unrelated.poll() is None
        assert kernel.WaitForSingleObject(owner_handle, 0) == 0
        if mode == 'normal':
            assert owner.returncode == 0, errors.decode(errors='replace')
            # Do not wait for descendants before proving the return boundary.
            assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles)
            assert (tmp_path / 'completed').read_text(encoding='utf-8') == 'cleanup acknowledged'
            with leases.installation_lease(control, exclusive=True):
                portable_entry.require_clean_owners(program, control)
            assert not list(control.glob('owner-*.json'))
            (program / 'activation-held.bin').unlink()
            (program / 'grandchild-held.bin').unlink()
        else:
            assert owner.returncode != 0 and not (tmp_path / 'completed').exists()
            if mode == 'unconfirmed':
                assert b'Native activation child cleanup is unconfirmed' in errors
            elif mode == 'constructor-failure':
                assert b'Native activation launch cleanup is unconfirmed' in errors
            # No descendant wait precedes this real exclusive-lease/veto test.
            # Even asynchronous safety termination cannot erase uncertainty.
            with leases.installation_lease(control, exclusive=True):
                with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                    portable_entry.require_clean_owners(program, control)
            receipt, = control.glob('owner-*.json')
            assert json.loads(receipt.read_bytes())['owner_pid'] == owner_pid
    finally:
        for name in ('release-worker', 'release-cleanup', 'release-constructor'):
            (tmp_path / name).touch()
        if owner_handle is not None:
            if kernel.WaitForSingleObject(owner_handle, 0) == 258:
                kernel.TerminateProcess(owner_handle, 99)
            assert kernel.WaitForSingleObject(owner_handle, 10000) == 0
            kernel.CloseHandle(owner_handle)
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)
        for handle in handles:
            try:
                # Reap only exact fixture handles, including a regressed source.
                if kernel.WaitForSingleObject(handle, 0) != 0:
                    kernel.TerminateProcess(handle, 99)
                assert kernel.WaitForSingleObject(handle, 10000) == 0
            finally:
                kernel.CloseHandle(handle)
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize('finish_on_wait', [True, False], ids=['exit-in-progress', 'still-running'])
def test_activation_job_refused_stop_waits_for_actual_process_exit(tmp_path, finish_on_wait):
    """A failed request cannot acknowledge or preempt the actual exit wait."""
    from types import SimpleNamespace
    from carr_library.audio import windows_process

    ready = tmp_path / 'child-holds-file'
    held = tmp_path / 'active-installation-file'
    code = (
        'import pathlib,sys; '
        f'stream=open({str(held)!r}, "wb"); stream.write(b"owned"); stream.flush(); '
        f'pathlib.Path({str(ready)!r}).write_text("ready"); '
        'assert sys.stdin.buffer.read(1) == b"x"; stream.close()')
    child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', code],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        close_fds=True)
    job = windows_process._Job()
    kernel = _kernel()
    handle = int(child._handle)
    actual = job.api.kernel
    observed = {'refused': [], 'waits': [], 'released': False}

    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *arguments):
            return self.call(*arguments)

    def refused(value, exit_code):
        assert kernel.WaitForSingleObject(value, 0) == 258
        observed['refused'].append(value)
        ctypes.set_last_error(5)
        return 0

    def wait(value, milliseconds):
        if milliseconds and kernel.WaitForSingleObject(value, 0) == 258:
            assert value in observed['refused'] and 0 < milliseconds <= 5000
            with pytest.raises(PermissionError):
                held.unlink()
            if finish_on_wait and not observed['released']:
                observed['released'] = True
                child.stdin.write(b'x')
                child.stdin.flush()
                child.stdin.close()
            result = kernel.WaitForSingleObject(value, milliseconds)
            observed['waits'].append(result)
            return result
        return kernel.WaitForSingleObject(value, milliseconds)

    class Kernel:
        TerminateProcess = Function(refused)
        WaitForSingleObject = Function(wait)
        def __getattr__(self, name):
            return getattr(actual, name)

    try:
        job.assign(handle)
        _ready(ready, child)
        job.api = SimpleNamespace(kernel=Kernel(), error=job.api.error)
        if finish_on_wait:
            job.stop_and_wait(handle)
            assert observed['refused'] and observed['waits'] == [0] and observed['released']
            assert kernel.WaitForSingleObject(handle, 0) == 0
            assert child.wait(timeout=5) == 0
            held.unlink()
        else:
            started = time.monotonic()
            with pytest.raises(TimeoutError, match='remains unconfirmed.*WinError 5'):
                job.stop_and_wait(handle)
            assert time.monotonic() - started < 8
            assert observed['refused'] and observed['waits'] == [258] and not observed['released']
            assert kernel.WaitForSingleObject(handle, 0) == 258
            with pytest.raises(PermissionError):
                held.unlink()
    finally:
        job.close()
        if kernel.WaitForSingleObject(handle, 0) == 258:
            kernel.TerminateProcess(handle, 99)
        assert kernel.WaitForSingleObject(handle, 10000) == 0
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=5)
        child.stderr.close()
        child._handle.Close()
