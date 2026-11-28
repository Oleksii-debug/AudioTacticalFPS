"""Actual managed bootstrap child ownership; no signed setup acceptance claim.

The offline installer script is an explicitly synthetic fixture. Its launcher,
Job membership, process handles, descendant-held files and installation leases
are real. The separately built Inno lifecycle gate remains required.
"""
import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import venv

import pytest

import carr_library
from carr_library import installation_lease as leases
from carr_library.audio import windows_process


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows managed bootstrap ownership required')
ROOT = Path(__file__).resolve().parents[2]


def maintenance():
    path = ROOT / 'packaging/windows_installer/maintenance.py'
    spec = importlib.util.spec_from_file_location('managed_bootstrap_native_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HELPER = r'''
import importlib.util, os, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from carr_library import installation_lease as leases
from carr_library.audio import windows_process
source, mode, selected, folder = sys.argv[2:]
program, root = pathlib.Path(selected), pathlib.Path(folder)
spec = importlib.util.spec_from_file_location('managed_bootstrap_native_helper', source)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
(root / 'helper-owner-pid').write_text(str(os.getpid()), encoding='utf-8')

def barrier(name, release):
    (root / name).write_text('ready', encoding='utf-8')
    deadline = time.monotonic() + 30
    while not (root / release).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('The managed fixture barrier was not released.')
        time.sleep(.01)

if mode in ('normal', 'unconfirmed'):
    original_cleanup = windows_process._Job.stop_and_wait
    def cleanup(job, handle=None):
        barrier('cleanup-ready', 'release-cleanup')
        if mode == 'unconfirmed':
            raise TimeoutError('Synthetic unconfirmed managed descendant cleanup')
        return original_cleanup(job, handle)
    windows_process._Job.stop_and_wait = cleanup

original_process = windows_process.WindowsJobProcess
def start(command, **keywords):
    assert command == [str(program / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
                       str(program / 'install_windows.py')]
    # The only substituted input is the fixture runtime executable. The exact
    # production bootstrap argv, environment and owned launcher remain in use.
    process = original_process([sys.executable, *command[1:]], **keywords)
    if mode == 'constructor-failure':
        barrier('constructor-ready', 'release-constructor')
        raise OSError('Synthetic post-create managed bootstrap constructor failure')
    return process
windows_process.WindowsJobProcess = start

def process_module(name, source_path):
    assert name == 'resonance_installer_owned_process' and source_path == service.process_source
    assert pathlib.Path(source_path).read_bytes() == pathlib.Path(windows_process.__file__).read_bytes()
    return windows_process
service._module = process_module

with leases.installation_lease(program.parent, exclusive=True):
    with service._bootstrap_owner(program):
        service._bootstrap(program)
(root / 'completed').write_text('cleanup acknowledged', encoding='utf-8')
'''

_INSTALLER = r'''
import json, os, pathlib, subprocess, sys, time
program = pathlib.Path(__file__).parent
root = program.parent.parent
stream = (program / 'bootstrap-held.bin').open('wb')
stream.write(b'actual installer child'); stream.flush()
environment = program / '.venv'
environment.mkdir()
(environment / '.installation-complete').touch()
grand_code = """
import os, pathlib, sys, time
program, root = map(pathlib.Path, sys.argv[1:])
stream = (program / 'grandchild-held.bin').open('wb')
stream.write(b'actual owned grandchild'); stream.flush()
(root / 'grandchild-ready').write_text(str(os.getpid()))
time.sleep(30)
"""
child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-X', 'utf8', '-c', grand_code,
                          str(program), str(root)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
deadline = time.monotonic() + 30
while not (root / 'grandchild-ready').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('The actual managed descendant did not open its file.')
    time.sleep(.01)
temporary = root / 'child-ready.pending'
temporary.write_text(json.dumps({'direct': os.getpid(),
                                'descendant': int((root / 'grandchild-ready').read_text())}))
os.replace(temporary, root / 'child-ready.json')
while not (root / 'release-worker').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('The actual managed worker was not released.')
    time.sleep(.01)
'''


def kernel():
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


def ready(path, owner):
    deadline = time.monotonic() + 15
    while not path.exists():
        if owner.poll() is not None:
            raise AssertionError('Actual managed helper exited before its barrier: ' +
                                 owner.stderr.read().decode(errors='replace'))
        if time.monotonic() >= deadline:
            raise AssertionError('Actual managed helper did not reach its bounded barrier.')
        time.sleep(.01)
    return path


@pytest.mark.parametrize('mode', ['normal', 'unconfirmed', 'helper-death', 'constructor-failure'])
def test_managed_bootstrap_retains_lease_or_durable_veto_until_actual_tree_exit(tmp_path, mode):
    assert sys.version_info[:2] == (3, 11) and ctypes.sizeof(ctypes.c_void_p) == 8
    service = maintenance()
    installed = tmp_path / 'managed-Україна'
    service.preflight(installed, 'a' * 64, 'b' * 64)
    program = installed / 'app'
    program.mkdir()
    (program / 'install_windows.py').write_text(_INSTALLER, encoding='utf-8')
    package_parent = Path(carr_library.__file__).resolve().parents[1]
    owner = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', '-c', _HELPER,
                              str(package_parent), str(service.HERE / 'maintenance.py'),
                              mode, str(program), str(tmp_path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, close_fds=True)
    unrelated = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c',
                                  'import time; time.sleep(30)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
    api, handles, owner_handle = kernel(), [], None
    try:
        owner_pid = int(ready(tmp_path / 'helper-owner-pid', owner).read_text(encoding='utf-8'))
        owner_handle = api.OpenProcess(0x00100000 | 0x0001, False, owner_pid)
        assert owner_handle, ctypes.get_last_error()
        value = json.loads(ready(tmp_path / 'child-ready.json', owner).read_bytes())
        for key in ('direct', 'descendant'):
            handle = api.OpenProcess(0x00100000 | 0x0001, False, value[key])
            assert handle, ctypes.get_last_error()
            handles.append(handle)
        assert all(api.WaitForSingleObject(handle, 0) == 258 for handle in handles)
        receipt = installed / leases.BOOTSTRAP_OWNER
        assert json.loads(receipt.read_bytes())['owner_pid'] == owner_pid
        with pytest.raises(RuntimeError, match='running jobs'):
            with leases.installation_lease(installed, exclusive=True):
                pytest.fail('Managed bootstrap released its lease with live native children.')
        if mode in ('normal', 'unconfirmed'):
            (tmp_path / 'release-worker').touch()
            ready(tmp_path / 'cleanup-ready', owner)
            assert api.WaitForSingleObject(handles[0], 0) == 0
            assert api.WaitForSingleObject(handles[1], 0) == 258
            with pytest.raises(RuntimeError, match='running jobs'):
                with leases.installation_lease(installed, exclusive=True):
                    pytest.fail('The direct child was mistaken for the complete bootstrap tree.')
            (tmp_path / 'release-cleanup').touch()
        elif mode == 'constructor-failure':
            ready(tmp_path / 'constructor-ready', owner)
            (tmp_path / 'release-constructor').touch()
        else:
            assert api.TerminateProcess(owner_handle, 99), ctypes.get_last_error()
        _output, errors = owner.communicate(timeout=15)
        assert api.WaitForSingleObject(owner_handle, 0) == 0
        assert unrelated.poll() is None
        if mode == 'normal':
            assert owner.returncode == 0, errors.decode(errors='replace')
            # No waiting for child handles occurs before this return assertion.
            assert all(api.WaitForSingleObject(handle, 0) == 0 for handle in handles)
            assert (tmp_path / 'completed').read_text() == 'cleanup acknowledged'
            with leases.installation_lease(installed, exclusive=True):
                leases.require_clean_bootstrap(installed)
            assert not receipt.exists()
            (program / 'bootstrap-held.bin').unlink()
            (program / 'grandchild-held.bin').unlink()
        else:
            assert owner.returncode != 0 and not (tmp_path / 'completed').exists()
            if mode == 'unconfirmed':
                assert b'Native bootstrap child cleanup is unconfirmed' in errors
            elif mode == 'constructor-failure':
                assert b'Native bootstrap launch cleanup is unconfirmed' in errors
            # The native Job's fallback may still be terminating. Never wait for
            # it first and then retrospectively declare this return safe.
            with leases.installation_lease(installed, exclusive=True):
                with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                    leases.require_clean_bootstrap(installed)
            with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                with leases.application_lease(prefix=program / '.venv'):
                    pytest.fail('A new managed operation ignored unconfirmed bootstrap cleanup.')
            assert json.loads(receipt.read_bytes())['owner_pid'] == owner_pid
    finally:
        for name in ('release-worker', 'release-cleanup', 'release-constructor'):
            (tmp_path / name).touch()
        if owner_handle is not None:
            if api.WaitForSingleObject(owner_handle, 0) == 258:
                api.TerminateProcess(owner_handle, 99)
            assert api.WaitForSingleObject(owner_handle, 10000) == 0
            api.CloseHandle(owner_handle)
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)
        for handle in handles:
            try:
                if api.WaitForSingleObject(handle, 0) != 0:
                    api.TerminateProcess(handle, 99)
                assert api.WaitForSingleObject(handle, 10000) == 0
            finally:
                api.CloseHandle(handle)
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)


def test_exact_compiled_managed_launcher_refuses_unclean_bootstrap_before_site(tmp_path):
    source = Path(os.environ['CARR_NATIVE_LAUNCHER_DIR'])
    receipt = json.loads((source / 'build-evidence.json').read_text(encoding='utf-8-sig'))
    binary = (source / 'ResearchLibrary.exe').read_bytes()
    assert sha256(binary).hexdigest() == receipt['pe']['sha256']
    assert sha256((ROOT / 'packaging/native-launcher/launcher.c').read_bytes()).hexdigest() == \
        receipt['source_sha256']['launcher.c']
    service = maintenance()
    installed = tmp_path / 'managed-native-Україна'
    service.preflight(installed, 'a' * 64, 'b' * 64)
    program = installed / 'app'
    program.mkdir()
    environment = program / '.venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    executable = environment / 'Scripts/python.exe'
    site = Path(subprocess.check_output(
        [str(executable), '-I', '-B', '-X', 'utf8', '-c',
         'import sysconfig; print(sysconfig.get_path("purelib"))'],
        text=True, encoding='utf-8', timeout=30).strip())
    package = site / 'carr_library'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    callback = tmp_path / 'native-callback'
    site_marker = tmp_path / 'native-site'
    (package / '__main__.py').write_text(
        'from pathlib import Path\n' + f'Path({str(callback)!r}).write_text("ran")\n',
        encoding='utf-8')
    (site / 'managed-native-proof.pth').write_text(
        'import pathlib; ' + f'pathlib.Path({str(site_marker)!r}).write_text("site")\n',
        encoding='utf-8')
    (environment / '.installation-complete').touch()
    launcher = program / 'ResearchLibrary.exe'
    launcher.write_bytes(binary)
    owner = installed / leases.BOOTSTRAP_OWNER
    owner.write_bytes(b'explicit inert uncertainty fixture; never production recovery authority')
    refused = subprocess.run([str(launcher), '--self-test'], capture_output=True, timeout=15)
    assert refused.returncode != 0
    assert b'previous installer cleanup was not confirmed' in refused.stderr
    assert not callback.exists() and not site_marker.exists()
    # Removing this known inert fixture is a control, not a product recovery API.
    owner.unlink()
    accepted = subprocess.run([str(launcher), '--self-test'], capture_output=True, timeout=15)
    assert accepted.returncode == 0, accepted.stderr.decode(errors='replace')
    assert callback.read_text() == 'ran' and site_marker.read_text() == 'site'
