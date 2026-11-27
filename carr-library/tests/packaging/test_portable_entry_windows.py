"""Actual Windows admission/owner-death tests; never counted as Linux passes."""
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import venv

import pytest

import carr_library
from carr_library import installation_lease as leases, portable_entry
from carr_library.audio import windows_job

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows Job and sharing semantics required')


_ENTRY = r'''
import ctypes, json, os, pathlib, subprocess, sys, time
from carr_library.installation_lease import installed_entrypoint
from carr_library.audio import windows_job

def write_json(path, value):
    temporary = path.with_name(path.name + '.pending')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temporary, path)

@installed_entrypoint
def main(arguments=None):
    mode, folder, *forwarded = arguments
    root = pathlib.Path(folder)
    if mode == 'stdin':
        write_json(root / 'callback.json',
                   {'pid': os.getpid(), 'arguments': forwarded, 'stdin': sys.stdin.read()})
        return 0
    child = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c',
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.write_text('ready'); time.sleep(30)",
        str(root / 'grandchild-ready')], close_fds=True)
    write_json(root / 'callback.json', {'pid': os.getpid(), 'child': child.pid})
    deadline = time.monotonic() + 30
    while not (root / 'release-parent').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('Fixture callback was not released')
        time.sleep(.02)
    return 0

if __name__ == '__main__':
    mode, folder = sys.argv[1:3]
    root = pathlib.Path(folder)
    write_json(root / 'guardian.json', {'pid': os.getpid()})
    if mode in ('before-assign', 'after-assign', 'failed-assign', 'wrong-job', 'wrong-lease',
                'missing-receipt', 'wrong-receipt-owner'):
        original = windows_job.LifetimeJob.assign
        def barrier(job, handle):
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetProcessId.argtypes, kernel.GetProcessId.restype = (ctypes.c_void_p,), ctypes.c_uint32
            if mode in ('after-assign', 'wrong-job', 'wrong-lease', 'missing-receipt', 'wrong-receipt-owner'):
                original(job, handle)
            write_json(root / 'admission-child.json', {'pid': kernel.GetProcessId(handle)})
            deadline = time.monotonic() + 30
            while not (root / 'release-assignment').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('Fixture admission barrier was not released')
                time.sleep(.02)
            if mode == 'failed-assign':
                raise OSError('Synthetic Job assignment failure')
            if mode == 'before-assign':
                original(job, handle)
        windows_job.LifetimeJob.assign = barrier
    if mode in ('cleanup-barrier', 'cleanup-failure'):
        original_cleanup = windows_job.LifetimeJob.stop_and_wait
        def cleanup(job, handle=None):
            (root / 'cleanup-ready').write_text('ready')
            deadline = time.monotonic() + 30
            while not (root / 'release-cleanup').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Cleanup observation barrier was not released')
                time.sleep(.01)
            if mode == 'cleanup-failure':
                raise TimeoutError('Synthetic unconfirmed Job cleanup')
            return original_cleanup(job, handle)
        windows_job.LifetimeJob.stop_and_wait = cleanup
    if mode == 'failed-spawn':
        from carr_library import portable_entry
        original_popen = subprocess.Popen
        def failed_spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            write_json(root / 'admission-child.json', {'pid': process.pid})
            deadline = time.monotonic() + 30
            while not (root / 'release-assignment').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Post-create failure barrier was not released')
                time.sleep(.01)
            raise OSError('Synthetic failure after native creation before Popen returned')
        portable_entry.subprocess.Popen = failed_spawn
    wrong_job = wrong_lease = None
    if mode in ('wrong-job', 'wrong-lease', 'missing-receipt', 'wrong-receipt-owner'):
        import msvcrt
        from carr_library import portable_entry
        original_json = json.dumps
        if mode == 'wrong-job':
            wrong_job = windows_job.LifetimeJob()
        elif mode == 'wrong-lease':
            wrong_lease = (root / 'unrelated-lease').open('wb')
        def changed_packet(value, *args, **kwargs):
            if isinstance(value, dict) and 'owner' in value and value['owner'].get('protocol') == 1:
                value = dict(value, owner=dict(value['owner']))
                if wrong_job is not None:
                    value['owner']['job'] = int(wrong_job.handle)
                elif wrong_lease is not None:
                    value['owner']['lease'] = msvcrt.get_osfhandle(wrong_lease.fileno())
                else:
                    value['owner']['receipt'] = 'f' * 32
                    if mode == 'wrong-receipt-owner':
                        control = portable_entry.control_root(pathlib.Path(value['owner']['program']))
                        receipt = {'schema': 'carr-portable-owner/1', 'id': 'f' * 32,
                                   'program': value['owner']['program'], 'owner_pid': os.getpid() + 1}
                        (control / ('owner-' + 'f' * 32 + '.json')).write_text(
                            original_json(receipt), encoding='utf-8')
            return original_json(value, *args, **kwargs)
        portable_entry.json.dumps = changed_packet
    try:
        raise SystemExit(main())
    finally:
        if wrong_job is not None:
            wrong_job.close()
        if wrong_lease is not None:
            wrong_lease.close()
'''


@pytest.fixture
def owned_fixture(tmp_path):
    assert sys.version_info[:2] == (3, 11) and ctypes.sizeof(ctypes.c_void_p) == 8, \
        'This native gate requires the supported Windows x64 / Python 3.11 profile.'
    root = tmp_path / 'owned-portable-Україна' / 'program'
    root.mkdir(parents=True)
    environment = root / '.venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    executable = environment / 'Scripts/python.exe'
    site = Path(subprocess.check_output(
        [str(executable), '-I', '-B', '-X', 'utf8', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        text=True, encoding='utf-8', timeout=30).strip())
    package = site / 'carr_library'
    (package / 'audio').mkdir(parents=True)
    # Synthetic CLI work; the admission, lease and Job implementations below
    # are exact production source bytes, not mocks or altered alternatives.
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'audio/__init__.py').write_text('', encoding='utf-8')
    for name in ('installation_lease.py', 'portable_entry.py', 'portable_bootstrap.py',
                 'durable_state.py'):
        shutil.copyfile(Path(carr_library.__file__).resolve().parent / name, package / name)
    shutil.copyfile(Path(windows_job.__file__), package / 'audio/windows_job.py')
    (package / '__main__.py').write_text(_ENTRY, encoding='utf-8')
    # Each process records actual site initialization. Preadmission children
    # must have no such file, even while the guardian has initialized normally.
    (site / 'owner-site-probe.pth').write_text(
        "import os,pathlib; location=os.environ.get('CARR_PORTABLE_TEST_SITE_ROOT'); "
        "location and pathlib.Path(location, 'site-' + str(os.getpid())).write_text('initialized')\n",
        encoding='utf-8')
    (environment / '.installation-complete').touch()
    marker = root / portable_entry.PORTABLE_MARKER
    marker.write_bytes(portable_entry.PORTABLE_CONTRACT)
    manifest = {'schema': 1, 'bundled_python': True, 'bundled_launcher': True,
                'source_commit': 'a' * 40,
                'files': {marker.name: sha256(marker.read_bytes()).hexdigest()}}
    (root / 'bundle-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root, executable


def wait_file(path, process, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            raise AssertionError('Fixture process exited before its barrier: ' +
                                 process.stderr.read().decode(errors='replace'))
        if time.monotonic() >= deadline:
            raise AssertionError('Actual child did not reach its deterministic barrier')
        time.sleep(.02)
    return path


def kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes, api.OpenProcess.restype = (
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = (
        wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD
    api.CloseHandle.argtypes, api.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
    api.TerminateProcess.argtypes, api.TerminateProcess.restype = (
        wintypes.HANDLE, wintypes.UINT), wintypes.BOOL
    return api


def process_handle(pid, *, terminate=False):
    api = kernel()
    handle = api.OpenProcess(0x00100000 | (0x0001 if terminate else 0), False, pid)
    assert handle, 'The exact test child was unavailable before termination'
    return api, handle


def launch_guardian(folder, *arguments, **keywords):
    process = subprocess.Popen(*arguments, **keywords)
    try:
        # A Windows venv launcher PID can differ from the Python lifetime owner.
        identity = json.loads(wait_file(folder / 'guardian.json', process).read_bytes())['pid']
        process._carr_test_owner_pid = identity
        process._carr_test_owner = process_handle(identity, terminate=True)
        return process
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        raise


def kill_guardian(process):
    api, handle = process._carr_test_owner
    assert api.WaitForSingleObject(handle, 0) == 258
    assert api.TerminateProcess(handle, 99), ctypes.get_last_error()


def assert_no_child_site(folder, process):
    assert {path.name for path in folder.glob('site-*')} == {
        'site-' + str(process._carr_test_owner_pid)}


def stop(process):
    owner = getattr(process, '_carr_test_owner', None)
    try:
        if owner is not None:
            api, handle = owner
            if api.WaitForSingleObject(handle, 0) == 258:
                assert api.TerminateProcess(handle, 99), ctypes.get_last_error()
            assert api.WaitForSingleObject(handle, 10000) == 0
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    finally:
        if owner is not None:
            owner[0].CloseHandle(owner[1])
            process._carr_test_owner = None


@pytest.mark.parametrize('phase', ['before-assign', 'after-assign'])
def test_parent_death_before_admission_runs_no_site_or_application_code(owned_fixture, tmp_path, phase):
    root, executable = owned_fixture
    parent = launch_guardian(tmp_path, [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library', phase, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    api = handle = None
    try:
        path = wait_file(tmp_path / 'admission-child.json', parent)
        child = json.loads(path.read_bytes())['pid']
        api, handle = process_handle(child)
        assert not (tmp_path / 'callback.json').exists()
        assert not (tmp_path / ('site-' + str(child))).exists()
        assert_no_child_site(tmp_path, parent)
        kill_guardian(parent)
        parent.wait(timeout=10)
        assert api.WaitForSingleObject(handle, 10000) == 0
        assert not (tmp_path / 'callback.json').exists()
        assert not (tmp_path / 'grandchild-ready').exists()
        assert not (tmp_path / ('site-' + str(child))).exists()
        assert_no_child_site(tmp_path, parent)
        with leases.installation_lease(portable_entry.control_root(root), exclusive=True):
            with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                portable_entry.require_clean_owners(root)
        assert list(portable_entry.control_root(root).glob('owner-*.json'))
    finally:
        (tmp_path / 'release-assignment').touch()
        stop(parent)
        if handle is not None:
            api.CloseHandle(handle)


def test_actual_assignment_failure_never_admits_application_work(owned_fixture, tmp_path):
    root, executable = owned_fixture
    parent = launch_guardian(tmp_path, [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library',
                               'failed-assign', str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    api = handle = None
    try:
        path = wait_file(tmp_path / 'admission-child.json', parent)
        child = json.loads(path.read_bytes())['pid']
        api, handle = process_handle(child)
        assert not (tmp_path / ('site-' + str(child))).exists()
        assert_no_child_site(tmp_path, parent)
        (tmp_path / 'release-assignment').touch()
        _out, errors = parent.communicate(timeout=15)
        assert parent.returncode == 1
        assert b'Synthetic Job assignment failure' in errors
        assert api.WaitForSingleObject(handle, 5000) == 0
        assert not (tmp_path / 'callback.json').exists()
        with leases.installation_lease(portable_entry.control_root(root), exclusive=True):
            portable_entry.require_clean_owners(root)
        assert not list(portable_entry.control_root(root).glob('owner-*.json'))
    finally:
        (tmp_path / 'release-assignment').touch()
        stop(parent)
        if handle is not None:
            api.CloseHandle(handle)


@pytest.mark.parametrize('proof', ['wrong-job', 'wrong-lease', 'missing-receipt', 'wrong-receipt-owner'])
def test_native_admission_rejects_unrelated_job_lease_or_cleanup_receipt(owned_fixture, tmp_path, proof):
    root, executable = owned_fixture
    parent = launch_guardian(tmp_path, [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library', proof, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    api = handle = None
    try:
        child = json.loads(wait_file(tmp_path / 'admission-child.json', parent).read_bytes())['pid']
        api, handle = process_handle(child)
        (tmp_path / 'release-assignment').touch()
        _output, errors = parent.communicate(timeout=15)
        assert parent.returncode != 0
        expected = {'wrong-job': b'exact lifetime owner', 'wrong-lease': b'does not own this portable',
                    'missing-receipt': b'FileNotFoundError',
                    'wrong-receipt-owner': b'cleanup receipt belongs to another owner'}[proof]
        assert expected in errors
        assert api.WaitForSingleObject(handle, 5000) == 0
        assert not (tmp_path / ('site-' + str(child))).exists()
        assert_no_child_site(tmp_path, parent)
        assert not (tmp_path / 'callback.json').exists()
        control = portable_entry.control_root(root)
        with leases.installation_lease(control, exclusive=True):
            if proof == 'wrong-receipt-owner':
                with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                    portable_entry.require_clean_owners(root)
                forged = control / ('owner-' + 'f' * 32 + '.json')
                assert json.loads(forged.read_bytes())['owner_pid'] == parent._carr_test_owner_pid + 1
                assert list(control.glob('owner-*.json')) == [forged]
                # Delete only this test's known inert forged row; product
                # cleanup must not delete any unrelated owner evidence.
                forged.unlink()
            portable_entry.require_clean_owners(root)
        assert not list(control.glob('owner-*.json'))
    finally:
        (tmp_path / 'release-assignment').touch()
        stop(parent)
        if handle is not None:
            api.CloseHandle(handle)


@pytest.mark.parametrize('finish', ['guardian-death', 'normal-return'])
def test_lease_and_job_cover_actual_descendants_until_application_shutdown(owned_fixture, tmp_path, finish):
    root, executable = owned_fixture
    unrelated = subprocess.Popen([str(executable), '-I', '-S', '-B', '-c',
                                  'import time; time.sleep(30)'])
    parent = launch_guardian(tmp_path, [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library', 'work', str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    handles = []
    try:
        path = wait_file(tmp_path / 'callback.json', parent)
        data = json.loads(path.read_bytes())
        wait_file(tmp_path / 'grandchild-ready', parent)
        handles = [process_handle(data[name]) for name in ('pid', 'child')]
        assert (tmp_path / ('site-' + str(data['pid']))).exists()
        with pytest.raises(RuntimeError, match='running jobs'):
            with leases.installation_lease(portable_entry.control_root(root), exclusive=True):
                pytest.fail('Active owned work did not retain the installation lease')
        if finish == 'guardian-death':
            kill_guardian(parent)
        else:
            (tmp_path / 'release-parent').touch()
        parent.wait(timeout=15)
        for api, handle in handles:
            assert api.WaitForSingleObject(handle, 10000) == 0
        assert unrelated.poll() is None
        if finish == 'normal-return':
            assert parent.returncode == 0, parent.stderr.read().decode(errors='replace')
        with leases.installation_lease(portable_entry.control_root(root), exclusive=True):
            if finish == 'guardian-death':
                with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                    portable_entry.require_clean_owners(root)
            else:
                portable_entry.require_clean_owners(root)
        assert bool(list(portable_entry.control_root(root).glob('owner-*.json'))) == (finish == 'guardian-death')
    finally:
        (tmp_path / 'release-parent').touch()
        stop(parent)
        stop(unrelated)
        for api, handle in handles:
            api.CloseHandle(handle)


def test_private_admission_pipe_preserves_stdin_and_exact_cli_arguments(owned_fixture, tmp_path):
    _root, executable = owned_fixture
    arguments = ['текст із пробілами', '"quoted"', 'literal-$-and-semicolon;']
    answer = subprocess.run(
        [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library', 'stdin', str(tmp_path), *arguments],
        input='Original stdin. Незмінний текст.\n'.encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    assert answer.returncode == 0, answer.stderr.decode(errors='replace')
    result = json.loads((tmp_path / 'callback.json').read_bytes())
    assert result['arguments'] == arguments
    assert result['stdin'] == 'Original stdin. Незмінний текст.\n'


def test_compiled_launcher_obeys_same_portable_lease_before_python_start(owned_fixture, tmp_path):
    root, _executable = owned_fixture
    directory = os.environ.get('CARR_NATIVE_LAUNCHER_DIR')
    assert directory, 'The native gate must provide its exact freshly built CARR_NATIVE_LAUNCHER_DIR.'
    source = Path(directory)
    receipt = json.loads((source / 'build-evidence.json').read_text(encoding='utf-8-sig'))
    binary = (source / 'ResearchLibrary.exe').read_bytes()
    assert sha256(binary).hexdigest() == receipt['pe']['sha256']
    expected = Path(__file__).resolve().parents[2] / 'packaging/native-launcher/launcher.c'
    assert sha256(expected.read_bytes()).hexdigest() == receipt['source_sha256']['launcher.c']
    launcher = root / 'ResearchLibrary.exe'
    launcher.write_bytes(binary)
    control = portable_entry.control_root(root, create=True)
    with leases.installation_lease(control, exclusive=True):
        refused = subprocess.run([str(launcher), '--launcher-check'], capture_output=True, timeout=10)
        assert refused.returncode != 0
        assert b'maintenance is in progress' in refused.stderr
    available = subprocess.run([str(launcher), '--launcher-check'], capture_output=True, timeout=10)
    assert available.returncode == 0, available.stderr.decode(errors='replace')
    assert json.loads(available.stdout)['state'] == 'ready'


@pytest.mark.parametrize('mode', ['cleanup-barrier', 'cleanup-failure'])
def test_exclusive_activation_never_uses_direct_child_exit_as_tree_cleanup(
        owned_fixture, tmp_path, mode):
    root, executable = owned_fixture
    parent = launch_guardian(tmp_path, [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library', mode, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    handles = []
    try:
        data = json.loads(wait_file(tmp_path / 'callback.json', parent).read_bytes())
        wait_file(tmp_path / 'grandchild-ready', parent)
        handles = [process_handle(data[name]) for name in ('pid', 'child')]
        control = portable_entry.control_root(root)
        (tmp_path / 'release-parent').touch()
        wait_file(tmp_path / 'cleanup-ready', parent)
        # The direct entry has returned. A real descendant is still live,
        # exactly the interval missed by a direct Popen.wait() alone.
        assert handles[0][0].WaitForSingleObject(handles[0][1], 0) == 0
        assert handles[1][0].WaitForSingleObject(handles[1][1], 0) == 258
        assert parent.poll() is None
        with pytest.raises(RuntimeError, match='running jobs'):
            with leases.installation_lease(control, exclusive=True):
                pytest.fail('Guardian released its lease before descendant acknowledgement')
        assert len(list(control.glob('owner-*.json'))) == 1
        (tmp_path / 'release-cleanup').touch()
        _output, errors = parent.communicate(timeout=15)
        if mode == 'cleanup-barrier':
            assert parent.returncode == 0, errors.decode(errors='replace')
            # No wait for either descendant occurs before this zero-time
            # assertion and the actual exclusive activation admission.
            assert all(api.WaitForSingleObject(handle, 0) == 0 for api, handle in handles)
            with leases.installation_lease(control, exclusive=True):
                portable_entry.require_clean_owners(root)
            assert not list(control.glob('owner-*.json'))
        else:
            assert parent.returncode != 0
            assert b'Synthetic unconfirmed Job cleanup' in errors
            # Even if the fallback Job close has already stopped the children,
            # unconfirmed cleanup must not be silently relabelled as idle.
            with leases.installation_lease(control, exclusive=True):
                with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                    portable_entry.require_clean_owners(root)
            assert len(list(control.glob('owner-*.json'))) == 1
    finally:
        (tmp_path / 'release-parent').touch()
        (tmp_path / 'release-cleanup').touch()
        stop(parent)
        for api, handle in handles:
            try:
                assert api.WaitForSingleObject(handle, 10000) == 0
            finally:
                api.CloseHandle(handle)


def test_post_create_launch_failure_keeps_uncertain_owner_receipt(owned_fixture, tmp_path):
    root, executable = owned_fixture
    parent = launch_guardian(tmp_path, [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library',
                               'failed-spawn', str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    api = handle = None
    try:
        child = json.loads(wait_file(tmp_path / 'admission-child.json', parent).read_bytes())['pid']
        api, handle = process_handle(child)
        assert api.WaitForSingleObject(handle, 0) == 258
        assert not (tmp_path / ('site-' + str(child))).exists()
        assert_no_child_site(tmp_path, parent)
        (tmp_path / 'release-assignment').touch()
        _output, errors = parent.communicate(timeout=15)
        assert parent.returncode == 1
        assert b'failure after native creation' in errors
        # The test can observe this process. The failed production Popen call
        # never returned its handle, so production must retain uncertainty.
        assert api.WaitForSingleObject(handle, 10000) == 0
        assert not (tmp_path / ('site-' + str(child))).exists()
        assert_no_child_site(tmp_path, parent)
        assert not (tmp_path / 'callback.json').exists()
        control = portable_entry.control_root(root)
        with leases.installation_lease(control, exclusive=True):
            with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
                portable_entry.require_clean_owners(root)
        assert len(list(control.glob('owner-*.json'))) == 1
    finally:
        (tmp_path / 'release-assignment').touch()
        stop(parent)
        if handle is not None:
            api.CloseHandle(handle)


def test_canonical_local_audio_setup_runs_through_actual_owned_entry_without_download(
        owned_fixture, tmp_path):
    from carr_library import local_audio_setup

    root, executable = owned_fixture
    site = root / '.venv/Lib/site-packages'
    package = site / 'carr_library'
    source = Path(carr_library.__file__).resolve().parent
    for name in ('local_audio_setup.py', 'audio_runtime.py', 'native_runtime.py'):
        shutil.copyfile(source / name, package / name)
    packaging = Path(__file__).resolve().parents[2] / 'packaging'
    manifest_path = root / 'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    for name in local_audio_setup.TOOL_FILES:
        target = root / 'tools/audio-setup' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(packaging / name, target)
        manifest['files']['tools/audio-setup/' + name] = sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    before = {name: (root / name).read_bytes() for name in manifest['files']}
    before_manifest = manifest_path.read_bytes()
    output = tmp_path / 'declined local audio runtime'
    result = subprocess.run(
        [str(executable), '-I', '-B', '-X', 'utf8', '-m', 'carr_library.local_audio_setup',
         '--interactive', '--output', str(output)],
        input='NO\n', encoding='utf-8', capture_output=True, timeout=30,
        env=dict(os.environ, CARR_PORTABLE_TEST_SITE_ROOT=str(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert '95294294' in result.stdout
    assert 'Скасовано: нічого не завантажено й не записано.' in result.stdout
    assert not output.exists()
    assert {name: (root / name).read_bytes() for name in manifest['files']} == before
    assert manifest_path.read_bytes() == before_manifest
    # Both the ordinary guardian and the admitted child initialized site; the
    # selected callback therefore did not silently bypass the owned bootstrap.
    assert len(list(tmp_path.glob('site-*'))) == 2
    control = portable_entry.control_root(root)
    with leases.installation_lease(control, exclusive=True):
        portable_entry.require_clean_owners(root)
    assert not list(control.glob('owner-*.json'))
