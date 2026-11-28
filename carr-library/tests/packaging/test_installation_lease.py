"""Actual cooperating OS leases; native Inno lifecycle has its separate probe."""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library import installation_lease as leases
from carr_library.audio.engines import _start_process, _stop


def installed(tmp_path, layout='versions', *, ready=True, generation='first'):
    root = tmp_path / 'installed'
    root.mkdir(exist_ok=True)
    if layout == 'versions':
        (root / leases.VERSION_MARKER).write_bytes(leases.VERSION_LAYOUT + b'\r\n')
        environment = root / 'versions' / generation / '.venv'
    else:
        (root / leases.MARKER).write_text('[installation]\n', encoding='utf-8')
        environment = root / 'app' / '.venv'
    environment.mkdir(parents=True, exist_ok=True)
    if ready:
        (environment / '.installation-complete').write_bytes(b'complete\n')
    lease = root / leases.LEASE
    if not lease.exists():
        lease.write_bytes(b'')
    return root, environment


def cannot_retire(root):
    with pytest.raises(RuntimeError, match='running jobs'):
        with leases.installation_lease(root, exclusive=True):
            pytest.fail('An active application must prevent destructive maintenance.')


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_current_managed_layout_holds_actual_shared_lease(tmp_path, layout):
    root, environment = installed(tmp_path, layout)
    assert leases.installation_root(environment) == root.resolve()
    with leases.application_lease(prefix=environment):
        cannot_retire(root)
        assert (environment / '.installation-complete').read_bytes() == b'complete\n'
    with leases.installation_lease(root, exclusive=True):
        pass
    assert (root / leases.LEASE).read_bytes() == b''


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_primary_entrypoint_holds_lease_during_real_callback(tmp_path, monkeypatch, layout):
    main = importlib.import_module('carr_library.__main__')
    runtime = importlib.import_module('carr_library.native_runtime')
    root, environment = installed(tmp_path, layout)
    called = []

    def self_test():
        cannot_retire(root)
        called.append(True)
        return {'synthetic': 'complete'}

    monkeypatch.setattr(sys, 'prefix', str(environment))
    monkeypatch.setattr(main, 'self_test', self_test)
    monkeypatch.setattr(runtime, 'activate_bundled_runtime', lambda: None)
    assert main.main(['--self-test']) == 0
    assert called == [True]
    with leases.installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize('marker', [b'', b'foreign-layout', b'x' * 129, b'\x00\xff'],
                         ids=['empty', 'foreign', 'oversize', 'binary'])
def test_observed_version_marker_cannot_silently_become_portable(tmp_path, marker):
    root, environment = installed(tmp_path)
    (root / leases.VERSION_MARKER).write_bytes(marker)
    with pytest.raises(ValueError, match='marker'):
        with leases.application_lease(prefix=environment):
            pytest.fail('Malformed managed state must not skip coordination.')
    assert environment.is_dir() and (root / leases.LEASE).read_bytes() == b''


@pytest.mark.parametrize('layout', ['app', 'versions'])
@pytest.mark.parametrize('arguments', [[], ['--document-formats'], ['--self-test', '--list-libraries']],
                         ids=['desktop', 'ordinary-cli', 'combined-self-test'])
def test_incomplete_generation_refuses_ordinary_entry_without_side_effects(tmp_path, layout, arguments):
    root, environment = installed(tmp_path, layout, ready=False)
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    with pytest.raises(RuntimeError, match='incomplete'):
        with leases.application_lease(arguments, prefix=environment, allow_bootstrap_self_test=True):
            pytest.fail('An incomplete environment cannot open ordinary application work.')
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_exact_bootstrap_check_preserves_each_existing_installer_contract(tmp_path, layout):
    root, environment = installed(tmp_path, layout, ready=False)
    with leases.application_lease(['--self-test'], prefix=environment, allow_bootstrap_self_test=True):
        if layout == 'versions':
            cannot_retire(root)  # Side-by-side bootstrap also owns its live files.
        else:
            # The existing legacy installer retains its exclusive parent handle.
            with leases.installation_lease(root, exclusive=True):
                pass
    assert not (environment / '.installation-complete').exists()


def test_bootstrap_requires_existing_plain_coordination_file(tmp_path):
    root, environment = installed(tmp_path, ready=False)
    (root / leases.LEASE).unlink()
    with pytest.raises(FileNotFoundError):
        with leases.application_lease(['--self-test'], prefix=environment, allow_bootstrap_self_test=True):
            pytest.fail('Bootstrap must not create or bypass missing managed coordination.')


@pytest.mark.parametrize('relative', ['portable/.venv', 'app/.venv', 'versions/first/.venv'])
def test_portable_runtime_creates_no_installation_state(tmp_path, relative):
    environment = tmp_path / relative
    environment.mkdir(parents=True)
    before = tuple(tmp_path.rglob('*'))
    with leases.application_lease(prefix=environment):
        assert leases.installation_root(environment) is None
    assert tuple(tmp_path.rglob('*')) == before


def test_nested_dispatch_keeps_one_os_hold_until_outer_exit(tmp_path):
    root, environment = installed(tmp_path)
    with leases.application_lease(prefix=environment):
        with leases.application_lease(prefix=environment):
            cannot_retire(root)
        cannot_retire(root)
    with leases.installation_lease(root, exclusive=True):
        pass


def test_concurrent_version_generations_share_root_until_last_operation_finishes(tmp_path):
    root, first = installed(tmp_path)
    _, second = installed(tmp_path, generation='second')
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def work(index, environment):
        with leases.application_lease(prefix=environment):
            started[index].set()
            if not release[index].wait(10):
                raise TimeoutError('Synthetic operation release did not arrive.')

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(work, i, environment) for i, environment in enumerate((first, second))]
        try:
            assert all(event.wait(10) for event in started)
            cannot_retire(root)
            release[0].set()
            futures[0].result(timeout=10)
            cannot_retire(root)
            release[1].set()
            futures[1].result(timeout=10)
            with leases.installation_lease(root, exclusive=True):
                pass
        finally:
            for event in release:
                event.set()


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_real_owned_child_preserves_running_generation_during_fresh_preparation(tmp_path, layout):
    root, environment = installed(tmp_path, layout)
    program = environment.parent / 'retained-program.txt'
    program.write_bytes(b'old installed bytes remain in use')
    ready, release = tmp_path / 'child-ready', tmp_path / 'child-release'
    script = (
        'from pathlib import Path; import sys,time\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'from carr_library.installation_lease import application_lease\n'
        'with application_lease(prefix=Path(sys.argv[2])):\n'
        '    Path(sys.argv[3]).write_bytes(b"ready")\n'
        '    deadline=time.monotonic()+25\n'
        '    while not Path(sys.argv[4]).exists():\n'
        '        if time.monotonic()>deadline: raise TimeoutError("release timeout")\n'
        '        time.sleep(.01)\n'
    )
    child = _start_process([sys.executable, '-I', '-B', '-c', script,
        str(Path(leases.__file__).resolve().parents[1]), str(environment), str(ready), str(release)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists() and child.poll() is None
        cannot_retire(root)
        if layout == 'versions':
            # Repair publishes a distinct generation without overwriting or
            # demanding exclusive access to a still-running older one.
            new_environment = root / 'versions' / 'fresh-repair' / '.venv'
            new_environment.mkdir(parents=True)
            (new_environment / '.installation-complete').write_bytes(b'complete\n')
            with leases.application_lease(prefix=new_environment):
                cannot_retire(root)
        assert program.read_bytes() == b'old installed bytes remain in use'
        assert child.poll() is None
        release.write_bytes(b'release')
        assert child.wait(timeout=10) == 0
        with leases.installation_lease(root, exclusive=True):
            assert program.read_bytes() == b'old installed bytes remain in use'
    finally:
        release.touch(exist_ok=True)
        _stop(child)


@pytest.mark.parametrize('module', [
    'carr_library.__main__', 'carr_library.bibliography_cli', 'carr_library.operations_cli',
    'carr_library.assistant.cli', 'carr_library.sync.cli',
    'carr_library.extract.ocr_model_catalog', 'carr_library.transcription_cli',
    'carr_library.security.__main__',
], ids=['main', 'bibliography', 'operations', 'assistant', 'sync', 'ocr-models', 'transcription', 'trust'])
def test_public_cli_cannot_enter_while_managed_files_are_exclusively_owned(tmp_path, monkeypatch, capsys, module):
    main = importlib.import_module(module).main
    root, environment = installed(tmp_path)
    monkeypatch.setattr(sys, 'prefix', str(environment))
    with leases.installation_lease(root, exclusive=True):
        assert main(['--help']) == 1
    assert 'Resonance:' in capsys.readouterr().err
    assert (environment / '.installation-complete').read_bytes() == b'complete\n'


def test_callback_error_releases_lease_without_changing_original_exception(tmp_path, monkeypatch):
    root, environment = installed(tmp_path)
    monkeypatch.setattr(sys, 'prefix', str(environment))
    failure = ValueError('Synthetic operation failed after admission.')

    @leases.installed_entrypoint
    def main(arguments):
        cannot_retire(root)
        raise failure

    with pytest.raises(ValueError) as observed:
        main([])
    assert observed.value is failure
    with leases.installation_lease(root, exclusive=True):
        pass


def test_generator_arguments_are_delivered_once_inside_the_same_lease(tmp_path, monkeypatch):
    root, environment = installed(tmp_path)
    monkeypatch.setattr(sys, 'prefix', str(environment))
    delivered = []

    @leases.installed_entrypoint
    def main(arguments):
        cannot_retire(root)
        delivered.extend(arguments)
        return 37

    assert main(value for value in ['--synthetic', 'значення']) == 37
    assert delivered == ['--synthetic', 'значення']


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_hardlinked_coordination_file_is_rejected_without_touching_other_bytes(tmp_path, layout):
    root, environment = installed(tmp_path, layout)
    other = tmp_path / 'unowned.txt'
    other.write_bytes(b'not installer state')
    (root / leases.LEASE).unlink()
    os.link(other, root / leases.LEASE)
    with pytest.raises(ValueError, match='unlinked'):
        with leases.application_lease(prefix=environment):
            pytest.fail('An alias must not become managed coordination.')
    assert other.read_bytes() == b'not installer state'


@pytest.mark.parametrize('layout', ['app', 'versions'])
def test_reparse_parent_metadata_is_rejected_before_any_lease(tmp_path, monkeypatch, layout):
    root, environment = installed(tmp_path, layout)
    original = Path.lstat
    parent = environment.parent

    def reparse(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_mode=value.st_mode, st_nlink=value.st_nlink,
                st_file_attributes=getattr(value, 'st_file_attributes', 0) | 0x400)
        return value

    monkeypatch.setattr(Path, 'lstat', reparse)
    with pytest.raises(ValueError, match='unlinked'):
        with leases.application_lease(prefix=environment):
            pytest.fail('A redirected generation must not enter.')
    assert (root / leases.LEASE).read_bytes() == b''


def test_actual_desktop_worker_keeps_lease_after_parent_lifecycle_returns(tmp_path, monkeypatch):
    root, environment = installed(tmp_path)
    origin = importlib.util.find_spec('carr_library.ui.desktop_app').origin
    parsed = ast.parse(Path(origin).read_text(encoding='utf-8'))
    desktop = next(node for node in parsed.body if isinstance(node, ast.ClassDef)
                   and node.name == 'DesktopFrame')
    methods = [node for node in desktop.body if isinstance(node, ast.FunctionDef)
               and node.name in {'run_job', '_finish_job_controls'}]
    assert len(methods) == 2
    wrapper = ast.parse('class ObservedFrame:\n    pass\n')
    wrapper.body[0].body = methods
    queued, futures = [], []
    namespace = {'__name__': 'carr_library.ui._lease_test', '__package__': 'carr_library.ui',
                 'wx': SimpleNamespace(CallAfter=queued.append)}
    exec(compile(ast.fix_missing_locations(wrapper), origin, 'exec'), namespace)
    frame = namespace['ObservedFrame']()
    frame.working = frame.busy = frame._closing = False
    frame.cancel_event = threading.Event()
    frame.cancel_button, frame.choice, frame.book = Mock(), Mock(), None
    frame.menu_bar, frame.report = Mock(), Mock()
    frame.menu_bar.GetMenuCount.return_value = 0
    started, release = threading.Event(), threading.Event()
    output, received = tmp_path / 'durable-output.txt', []
    monkeypatch.setattr(sys, 'prefix', str(environment))

    def operation():
        started.set()
        if not release.wait(10):
            raise TimeoutError('Synthetic worker release did not arrive.')
        output.write_bytes(b'completed exactly once')
        return {'state': 'complete'}

    with ThreadPoolExecutor(max_workers=1) as pool:
        def submit(callback):
            future = pool.submit(callback)
            futures.append(future)
            return future
        frame.executor = SimpleNamespace(submit=submit)
        try:
            with leases.application_lease(prefix=environment):
                frame.run_job('Synthetic durable job', operation, received.append)
                assert started.wait(10)
            cannot_retire(root)
            assert not output.exists() and not received
            release.set()
            result = futures[0].result(timeout=10)
            deadline = time.monotonic() + 10
            while not queued and time.monotonic() < deadline:
                time.sleep(.01)
            assert len(queued) == 1
            with leases.installation_lease(root, exclusive=True):
                pass
            queued.pop()()
            assert received == [result]
            assert output.read_bytes() == b'completed exactly once'
            assert not frame.working
        finally:
            release.set()
