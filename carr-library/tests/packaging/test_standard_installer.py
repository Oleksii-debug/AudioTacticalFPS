"""Real filesystem/lease guards; bootstrap and PE bytes are explicit fixtures.

No test here claims native Inno execution. The separate Windows acceptance
script compiles and installs the existing actual verified offline bundle.
"""
import errno
import hashlib
import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from carr_library.installation_lease import application_lease, installation_lease, plain


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load('standard_installer_builder_test', ROOT / 'packaging/windows_installer/build_setup.py')
service = build.maintenance


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def reseal(root):
    files = {p.relative_to(root).as_posix(): checksum(p.read_bytes())
             for p in root.rglob('*') if p.is_file() and p.name != 'bundle-manifest.json'}
    raw = (json.dumps(dict(schema=1, target='win_amd64', bundled_python=True,
        bundled_launcher=True, source_commit='a' * 40, files=files), sort_keys=True) + '\n').encode()
    (root / 'bundle-manifest.json').write_bytes(raw)
    return checksum(raw)


@pytest.fixture
def payload(tmp_path):
    root = tmp_path / 'synthetic sealed bundle Ω'
    root.mkdir()
    for name in ('install.lock', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd',
                 'README.txt', 'runtime/python.exe', 'runtime/Lib/os.py',
                 'ResearchLibrary.exe', 'native-launcher-build.json'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('SYNTHETIC, NOT EXECUTABLE: ' + name).encode())
    (root / 'install_windows.py').write_bytes((ROOT / 'packaging/install_windows.py').read_bytes())
    wheels = root / 'wheelhouse'
    wheels.mkdir()
    with ZipFile(wheels / 'universal_research_library-3.0.0-py3-none-any.whl', 'w') as archive:
        archive.writestr('universal_research_library-3.0.0.dist-info/METADATA',
                         'Metadata-Version: 2.3\nName: universal-research-library\nVersion: 3.0.0\n')
        for name in ('installation_lease.py', '__main__.py'):
            archive.writestr('carr_library/' + name, (ROOT / 'src/carr_library' / name).read_bytes())
    return root, reseal(root)


def synthetic_bootstrap(program):
    scripts = program / '.venv/Scripts'
    scripts.mkdir(parents=True)
    (scripts / 'python.exe').write_bytes(b'SYNTHETIC installed interpreter; never executed')
    (program / '.venv/pyvenv.cfg').write_text('home = synthetic fixture\n')
    (program / '.venv/.installation-complete').write_bytes(b'')


@pytest.fixture
def installed(tmp_path, payload):
    bundle, bundle_hash = payload
    root = tmp_path / 'Installed Resonance Ω'
    setup = b'explicit synthetic Inno setup placeholder, not executable'
    setup_hash = checksum(setup)
    service.preflight(root, bundle_hash, setup_hash)
    tools = root / 'maintenance'
    tools.mkdir()
    (tools / 'ResonanceSetup.exe').write_bytes(setup)
    for name, source in service.HELPERS.items():
        (tools / name).write_bytes(source.read_bytes())
    result = service.install(bundle, root, bundle_hash, setup_hash, runner=synthetic_bootstrap)
    assert result['operation'] == 'install'
    return root, bundle, bundle_hash, setup_hash


def test_install_records_exact_runtime_files_and_independent_receipt(installed):
    root, bundle, expected, setup = installed
    record = service.owned(root, expected, setup)
    assert record['files'] == service.inventory(root / 'app')[0]
    assert '.venv/Scripts/python.exe' in record['files']
    assert record['maintenance']['ResonanceSetup.exe'] == setup
    receipt = service.marker(root)
    assert receipt['owner_sha256'] == checksum((root / service.OWNER).read_bytes())
    assert receipt['state'] == 'complete'
    service.verifier.verify(root / 'app')


def test_repair_retains_changed_and_foreign_files_then_rebuilds(installed, tmp_path):
    root, payload, expected, setup = installed
    original = root / 'app/README.txt'
    original.write_bytes(b'changed by the user or damaged; retain these exact bytes')
    (root / 'app/foreign notes.txt').write_text('user notes Ω')
    external = tmp_path / 'outside-user-library'
    external.mkdir()
    (external / 'content.db').write_bytes(b'private-data sentinel')
    outside_hash = service.sha(external / 'content.db')
    result = service.install(payload, root, expected, setup, runner=synthetic_bootstrap)
    recovery = root.parent / result['retained_previous']
    assert result['operation'] == 'repair'
    assert (recovery / 'app/README.txt').read_bytes().startswith(b'changed by the user')
    assert (recovery / 'app/foreign notes.txt').read_text() == 'user notes Ω'
    assert (root / 'app/README.txt').read_bytes() == (payload / 'README.txt').read_bytes()
    assert not (root / 'app/foreign notes.txt').exists()
    assert service.sha(external / 'content.db') == outside_hash
    assert service.owned(root, expected, setup)['files'] == service.inventory(root / 'app')[0]


def test_failed_repair_restores_previous_complete_application_and_receipts(installed):
    root, payload, expected, setup = installed
    before = service.inventory(root / 'app')
    receipt = (root / service.MARKER).read_bytes()
    owned = (root / service.OWNER).read_bytes()

    def fail_after_partial_write(program):
        (program / 'partial-private-fixture.txt').write_bytes(b'partial bootstrap output')
        raise RuntimeError('synthetic interrupted offline installation')

    with pytest.raises(RuntimeError, match='interrupted'):
        service.install(payload, root, expected, setup, runner=fail_after_partial_write)
    assert service.inventory(root / 'app') == before
    assert (root / service.MARKER).read_bytes() == receipt
    assert (root / service.OWNER).read_bytes() == owned
    retained = list(root.parent.glob(root.name + '-incomplete-*'))
    assert len(retained) == 1
    assert (retained[0] / 'partial-private-fixture.txt').read_bytes() == b'partial bootstrap output'
    assert not (root / service.lease_module.BOOTSTRAP_OWNER).exists()


@pytest.mark.parametrize('phase', ['move-previous', 'copy-owner', 'copy-marker', 'publish-stage'])
def test_prebootstrap_repair_failure_restores_previous_without_allocating_receipts(installed, monkeypatch, phase):
    root, payload, expected, setup = installed
    before = service.inventory(root / 'app')
    receipts = {name: (root / name).read_bytes() for name in (service.OWNER, service.MARKER)}
    original_rename, original_copy = Path.rename, service.shutil.copyfile

    def rename(source, destination):
        if ((phase == 'move-previous' and source == root / 'app') or
                (phase == 'publish-stage' and source.name.startswith('staging-'))):
            raise OSError('synthetic pre-bootstrap filesystem failure')
        return original_rename(source, destination)

    def copy(source, destination, *args, **kwargs):
        selected = service.OWNER if phase == 'copy-owner' else service.MARKER
        if phase.startswith('copy-') and Path(source) == root / selected:
            raise OSError('synthetic pre-bootstrap filesystem failure')
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(Path, 'rename', rename)
    monkeypatch.setattr(service.shutil, 'copyfile', copy)
    monkeypatch.setattr(service, 'write', lambda *args: pytest.fail('Unchanged receipts need no allocation during rollback.'))
    with pytest.raises(OSError, match='pre-bootstrap filesystem failure'):
        service.install(payload, root, expected, setup,
                        runner=lambda *_: pytest.fail('Failed preparation must not reach bootstrap.'))
    assert service.inventory(root / 'app') == before
    assert {name: (root / name).read_bytes() for name in receipts} == receipts
    assert service.owned(root, expected, setup)['files'] == before[0]


@pytest.mark.parametrize('failed_receipt', [service.OWNER, service.MARKER])
def test_receipt_copy_failure_keeps_original_app_at_its_path(installed, monkeypatch, failed_receipt):
    root, payload, expected, setup = installed
    program = root / 'app'
    before = service.inventory(program)
    receipts = {name: (root / name).read_bytes() for name in (service.OWNER, service.MARKER)}
    original_copy, original_rename = service.shutil.copyfile, Path.rename
    attempted_moves, partial_receipts = [], []

    def copy(source, destination, *args, **kwargs):
        if Path(source) == root / failed_receipt:
            # A real destination receives partial bytes before the simulated
            # disk-full error. The old app must still be usable at this point;
            # recovering it afterward is a weaker guarantee during an outage.
            assert program.is_dir(), 'Repair moved the app before retaining its recovery receipts.'
            assert service.inventory(program) == before
            Path(destination).write_bytes(receipts[failed_receipt][:11])
            partial_receipts.append(Path(destination))
            raise OSError(errno.ENOSPC, 'synthetic receipt allocation failure')
        return original_copy(source, destination, *args, **kwargs)

    def rename(source, destination):
        if source == program:
            attempted_moves.append(Path(destination))
        return original_rename(source, destination)

    monkeypatch.setattr(service.shutil, 'copyfile', copy)
    monkeypatch.setattr(Path, 'rename', rename)
    monkeypatch.setattr(service, 'write', lambda *_: pytest.fail('Unchanged receipts require no replacement allocation.'))
    with pytest.raises(OSError, match='receipt allocation failure'):
        service.install(payload, root, expected, setup,
                        runner=lambda *_: pytest.fail('Unprepared recovery must not reach bootstrap.'))
    assert not attempted_moves
    assert len(partial_receipts) == 1
    assert partial_receipts[0].read_bytes() == receipts[failed_receipt][:11]
    assert service.inventory(program) == before
    assert {name: (root / name).read_bytes() for name in receipts} == receipts
    assert service.owned(root, expected, setup)['files'] == before[0]
    with installation_lease(root):
        pass


@pytest.mark.parametrize('change', ['modified', 'added', 'missing'])
def test_uninstall_refuses_changed_or_foreign_inventory_before_deleting_anything(installed, change):
    root, _, expected, setup = installed
    if change == 'modified':
        (root / 'app/README.txt').write_bytes(b'preserve my changed data')
    elif change == 'added':
        (root / 'app/user-document.txt').write_bytes(b'preserve my document')
    else:
        (root / 'app/README.txt').unlink()
    before = service.inventory(root / 'app')
    with pytest.raises(ValueError, match='changed|unowned'):
        service.uninstall(root, expected, setup)
    assert service.inventory(root / 'app') == before
    assert service.marker(root)['state'] == 'complete'


def test_uninstall_removes_only_owned_program_and_leaves_external_data(installed, tmp_path):
    root, _, expected, setup = installed
    external = tmp_path / 'userdata'
    external.mkdir()
    (external / 'book.txt').write_text('original Unicode prose Ω\f[pause 2]\r\n')
    before = (external / 'book.txt').read_bytes()
    # An unrelated root-level file is never in the app-owned deletion list.
    (root / 'keep-me.txt').write_bytes(b'not part of the installer')
    result = service.uninstall(root, expected, setup)
    assert result['operation'] == 'uninstall'
    assert not (root / 'app').exists()
    assert (external / 'book.txt').read_bytes() == before
    assert (root / 'keep-me.txt').read_bytes() == b'not part of the installer'
    assert service.marker(root)['state'] == 'uninstalled'
    assert (root / 'installation.lease').is_file()


def test_unknown_install_folder_is_not_claimed_or_changed(tmp_path, payload):
    root = tmp_path / 'already in use'
    root.mkdir()
    (root / 'my source.docx').write_bytes(b'unowned')
    with pytest.raises(ValueError, match='unowned'):
        service.preflight(root, payload[1], 'a' * 64)
    assert sorted(p.name for p in root.iterdir()) == ['my source.docx']


def test_data_directory_cannot_be_selected_as_install_target(tmp_path, payload, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    data = tmp_path / 'Universal Research Library'
    data.mkdir()
    with pytest.raises(ValueError, match='separate'):
        service.preflight(data, payload[1], 'a' * 64)
    with pytest.raises(ValueError, match='separate'):
        service.preflight(data / 'nested-app', payload[1], 'a' * 64)


@pytest.mark.parametrize('name', ['../escape', '/absolute', r'a\b', 'a//b', './a',
                                  'C:drive', 'a\x00b', 'NUL.txt', 'COM9.log',
                                  'a/../b', 'name.', 'name ', 'a?b', 'a"b'])
def test_owned_paths_reject_cross_platform_aliases(name):
    with pytest.raises(ValueError):
        service.relative(name)


def test_owned_manifest_tampering_fails_before_uninstall(installed):
    root, _, expected, setup = installed
    (root / service.OWNER).write_bytes(b'{"files":{"../private.txt":"' + b'a' * 64 + b'"}}')
    with pytest.raises(ValueError, match='manifest changed'):
        service.uninstall(root, expected, setup)
    assert (root / 'app/ResearchLibrary.exe').exists()


def test_cached_setup_tampering_is_not_overwritten_by_repair(installed):
    root, payload, expected, setup = installed
    cached = root / 'maintenance/ResonanceSetup.exe'
    cached.write_bytes(b'foreign bytes at a formerly owned path')
    with pytest.raises(ValueError, match='maintenance tools changed'):
        service.check(root, expected, setup)
    with pytest.raises(ValueError, match='maintenance tools changed'):
        service.install(payload, root, expected, setup, runner=synthetic_bootstrap)
    assert cached.read_bytes() == b'foreign bytes at a formerly owned path'


def test_shared_app_leases_exclude_maintenance_and_release_after_exception(installed):
    root, _, _, _ = installed
    with installation_lease(root), installation_lease(root):
        with pytest.raises(RuntimeError, match='Close Resonance'):
            with installation_lease(root, exclusive=True):
                pytest.fail('exclusive lease was incorrectly admitted')
    with pytest.raises(RuntimeError, match='fixture error'):
        with installation_lease(root, exclusive=True):
            with pytest.raises(RuntimeError, match='Close Resonance'):
                with installation_lease(root):
                    pytest.fail('a new app was incorrectly admitted during maintenance')
            raise RuntimeError('fixture error')
    with installation_lease(root):
        pass


def test_actual_application_entry_holds_shared_lease_for_the_whole_operation(installed, monkeypatch):
    from carr_library import __main__
    from carr_library import native_runtime
    root, _, _, _ = installed
    monkeypatch.setattr(sys, 'prefix', str(root / 'app/.venv'))
    # This suite's PE/interpreter files are explicit inert fixtures. Exercise
    # the real CLI/lease lifetime without claiming a native runtime load.
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: None)

    reached = []

    def run():
        reached.append('self-test')
        with pytest.raises(RuntimeError, match='Close Resonance'):
            with installation_lease(root, exclusive=True):
                pytest.fail('entrypoint did not retain the shared lease')
        return {'status': 'PASS'}

    monkeypatch.setattr(__main__, 'self_test', run)
    assert __main__.main(['--self-test']) == 0
    assert reached == ['self-test']
    with installation_lease(root, exclusive=True):
        assert __main__.main(['--self-test']) == 1
    assert reached == ['self-test']


def test_incomplete_bootstrap_allows_only_existing_isolated_self_test(installed):
    root, _, _, _ = installed
    (root / 'app/.venv/.installation-complete').unlink()
    with installation_lease(root, exclusive=True):
        with application_lease(['--self-test'], prefix=root / 'app/.venv', allow_bootstrap_self_test=True):
            pass
        with pytest.raises(RuntimeError, match='incomplete'):
            with application_lease(['--self-test'], prefix=root / 'app/.venv'):
                pytest.fail('A secondary entrypoint bypassed the incomplete installation.')
        for arguments in ([], ['--data-dir', 'foreign'], ['--self-test', '--data-dir', 'foreign']):
            with pytest.raises(RuntimeError, match='incomplete'):
                with application_lease(arguments, prefix=root / 'app/.venv'):
                    pytest.fail('unready installed entrypoint admitted user-data access')


def test_portable_prefix_does_not_create_installation_metadata(tmp_path):
    prefix = tmp_path / 'portable/.venv'
    with application_lease([], prefix=prefix):
        pass
    assert list(tmp_path.iterdir()) == []


def test_reparse_parent_refused_even_when_not_a_python_symlink(tmp_path, monkeypatch):
    parent = tmp_path / 'junction'
    parent.mkdir()
    child = parent / 'file'
    child.write_bytes(b'x')
    original = Path.lstat

    def redirected(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400, st_nlink=1)
        return info

    monkeypatch.setattr(Path, 'lstat', redirected)
    with pytest.raises(ValueError, match='unlinked'):
        plain(child)


def test_existing_hardlinked_lease_is_rejected_without_changing_target(installed, tmp_path):
    root, _, _, _ = installed
    target = tmp_path / 'foreign lock data'
    target.write_bytes(b'keep this exact content')
    (root / 'installation.lease').unlink()
    os.link(target, root / 'installation.lease')
    with pytest.raises(ValueError, match='unlinked'):
        with installation_lease(root, exclusive=True):
            pytest.fail('hardlinked lock admitted')
    assert target.read_bytes() == b'keep this exact content'


def test_template_uses_exact_manifest_no_recursive_uninstall_and_standard_per_user_entries(tmp_path, payload):
    root, expected = payload
    result = build.prepare(root, tmp_path / 'compiled source', manifest_sha256=expected)
    script = (tmp_path / 'compiled source/ResonanceSetup.iss').read_text(encoding='utf-8-sig')
    assert result['status'] == 'SOURCE_PREPARED'
    assert result['windows_installation_verified'] is False
    assert result['authenticode_verified'] is False
    assert 'PrivilegesRequired=lowest' in script
    assert 'CloseApplications=no' in script
    assert 'Repair Resonance' in script and 'ModifyPath' in script
    assert '[UninstallDelete]' not in script and 'filesandordirs' not in script
    assert ' --lease-owner ' in script
    assert script.count('Flags: dontcopy') == len(json.loads((root / 'bundle-manifest.json').read_bytes())['files']) + 1 + len(service.HELPERS)
    assert 'runtime\\python.exe' in script and 'CopyVerified' in script
    assert '@@' not in script


def test_bundle_extra_file_not_packaged(tmp_path, payload):
    root, expected = payload
    (root / 'private-user-document.txt').write_bytes(b'must not be packaged')
    with pytest.raises(ValueError, match='undeclared'):
        build.prepare(root, tmp_path / 'setup', manifest_sha256=expected)
    assert not (tmp_path / 'setup').exists()


def test_wrong_selected_bundle_identity_fails_before_output(tmp_path, payload):
    with pytest.raises(ValueError, match='installer identity'):
        build.prepare(payload[0], tmp_path / 'setup', manifest_sha256='a' * 64)
    assert not (tmp_path / 'setup').exists()


def test_binary_output_inside_git_is_refused(tmp_path):
    repository = tmp_path / 'repo'
    repository.mkdir()
    (repository / '.git').write_text('gitdir: shared-git')
    with pytest.raises(ValueError, match='outside Git'):
        build.outside_repository(repository / 'private-build')


def test_wrong_compiler_receipt_cannot_execute_any_program(tmp_path, monkeypatch):
    compiler = tmp_path / 'ISCC.exe'
    compiler.write_bytes(b'synthetic compiler')
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'schema': 1, 'version': '6.7.4', 'archive_sha256': '0' * 64, 'iscc_sha256': checksum(compiler.read_bytes())}))
    monkeypatch.setattr(build.subprocess, 'run', lambda *a, **k: pytest.fail('unverified compiler executed'))
    with pytest.raises(ValueError, match='pinned installation receipt'):
        build.compile_setup(tmp_path, iscc=compiler, toolchain_receipt=receipt)


def test_fresh_install_caches_exact_original_setup_and_reviewed_helpers(tmp_path, payload):
    setup = tmp_path / 'synthetic original setup.exe'
    setup.write_bytes(b'non-executable setup fixture')
    expected_setup = checksum(setup.read_bytes())
    root = tmp_path / 'new per-user Programs' / 'Resonance'
    result = service.install(payload[0], root, payload[1], expected_setup,
                             setup_source=setup, runner=synthetic_bootstrap)
    assert result['operation'] == 'install'
    recorded = service.owned(root, payload[1], expected_setup)
    assert (root / 'maintenance/ResonanceSetup.exe').read_bytes() == setup.read_bytes()
    for name, source in service.HELPERS.items():
        assert (root / 'maintenance' / name).read_bytes() == source.read_bytes()
        assert recorded['maintenance'][name] == checksum(source.read_bytes())
    assert service.HELPERS['windows_process.py'] == ROOT / 'src/carr_library/audio/windows_process.py'


def test_wrong_setup_source_is_rejected_before_any_cache_or_app_copy(tmp_path, payload):
    setup = tmp_path / 'wrong-setup.exe'
    setup.write_bytes(b'not the independently selected setup')
    root = tmp_path / 'new installation'
    with pytest.raises(ValueError, match='setup source changed'):
        service.install(payload[0], root, payload[1], 'a' * 64, setup_source=setup,
                        runner=lambda *_: pytest.fail('unverified setup reached bootstrap'))
    assert not (root / 'app').exists()
    assert not (root / 'maintenance').exists()
    assert not list(root.glob('staging-*'))


def test_source_cache_never_overwrites_retained_foreign_helper(tmp_path, payload):
    setup = tmp_path / 'setup.exe'
    setup.write_bytes(b'synthetic setup')
    setup_hash = checksum(setup.read_bytes())
    root = tmp_path / 'retained installation'
    service.preflight(root, payload[1], setup_hash)
    tools = root / 'maintenance'
    tools.mkdir()
    foreign = tools / 'maintenance.py'
    foreign.write_bytes(b'foreign content must remain')
    with pytest.raises(ValueError, match='will not be overwritten'):
        service.install(payload[0], root, payload[1], setup_hash, setup_source=setup,
                        runner=lambda *_: pytest.fail('changed helper reached bootstrap'))
    assert foreign.read_bytes() == b'foreign content must remain'
    assert not (root / 'app').exists()


def test_existing_venv_is_excluded_only_by_explicit_installed_bundle_capture(tmp_path, payload):
    root, expected = payload
    synthetic_bootstrap(root)
    private = root / '.venv/explicitly-unpackaged.txt'
    private.write_bytes(b'existing environment is never setup payload')
    with pytest.raises(ValueError, match='undeclared'):
        build.prepare(root, tmp_path / 'pristine-mode', manifest_sha256=expected)
    captured = tmp_path / 'sealed-only'
    result = build.prepare(root, captured, manifest_sha256=expected, installed_bundle=True)
    assert result['bundle_manifest_sha256'] == expected
    assert not (captured / 'payload/.venv').exists()
    assert (captured / 'payload/bundle-manifest.json').read_bytes() == (root / 'bundle-manifest.json').read_bytes()
    for name in service.HELPERS:
        assert (captured / 'helpers' / name).read_bytes() == service.HELPERS[name].read_bytes()


def test_installed_bundle_capture_still_refuses_private_files_outside_venv(tmp_path, payload):
    root, expected = payload
    synthetic_bootstrap(root)
    (root / 'research.txt').write_bytes(b'never package private extra files')
    with pytest.raises(ValueError, match='undeclared'):
        build.prepare(root, tmp_path / 'rejected', manifest_sha256=expected, installed_bundle=True)
    assert not (tmp_path / 'rejected').exists()


def test_native_install_success_is_gated_before_inno_registration(tmp_path, payload):
    build.prepare(payload[0], tmp_path / 'setup-source', manifest_sha256=payload[1])
    source = (tmp_path / 'setup-source/ResonanceSetup.iss').read_text(encoding='utf-8-sig')
    prepare = source.split('function PrepareToInstall', 1)[1].split('procedure CurStepChanged', 1)[0]
    assert "if not RunHelper('install', True) then begin" in prepare
    assert prepare.index("RunHelper('install', True)") < prepare.index('Prepared := True;')
    assert "Result := 'Offline installation did not complete." in prepare
    assert 'ssPostInstall' not in source
    assert 'ExecAndLogOutput' in source
    assert 'CopyFile(Source, Destination, True)' in source
    assert 'SuppressibleMsgBox' in source
    assert prepare.index('Result := CheckUserWrapperTargets;') < prepare.index("RunHelper('preflight', False)")
    assert "RegQueryStringValue(HKCU" in source


@pytest.mark.parametrize('outcome', ['success', 'nonzero', 'timeout'])
def test_bootstrap_uses_captured_owned_process_and_closes_it_on_all_exits(tmp_path, monkeypatch, outcome):
    events = []
    command_seen = {}

    class Handle(int):
        def Close(self):
            events.append('close exact process handle')

    class Child:
        def __init__(self):
            self.calls = 0
            self._handle = Handle(101)
            self._carr_job = SimpleNamespace(
                stop_and_wait=lambda value: events.append(('confirm owned tree exit', value)),
                close=lambda: events.append('close owned job'))

        def poll(self):
            return 0

        def kill(self):
            pytest.fail('Acknowledged fixture cleanup does not need fallback termination.')

        def wait(self, *, timeout):
            self.calls += 1
            events.append(('wait', timeout))
            if self.calls == 1:
                if outcome == 'timeout':
                    raise subprocess.TimeoutExpired('synthetic bootstrap', timeout)
                return 9 if outcome == 'nonzero' else 0
            return 0

    def start(command, **kwargs):
        command_seen.update(command=command, **kwargs)
        return Child()

    def module(name, path):
        assert name == 'resonance_installer_owned_process'
        assert path == service.process_source
        return SimpleNamespace(WindowsJobProcess=start)

    environment = {'PYTHONPATH': 'untrusted', 'PIP_INDEX_URL': 'https://invalid.test',
                   'SystemRoot': 'fixed platform directory'}
    monkeypatch.setattr(service, 'os', SimpleNamespace(name='nt', environ=environment))
    monkeypatch.setattr(service, '_module', module)
    program = tmp_path / 'program'
    if outcome == 'success':
        service._bootstrap(program)
    else:
        expected_error = subprocess.TimeoutExpired if outcome == 'timeout' else subprocess.CalledProcessError
        with pytest.raises(expected_error):
            service._bootstrap(program)
    assert events == [('wait', 1800), ('confirm owned tree exit', 101),
                      'close owned job', ('wait', 5), 'close exact process handle']
    assert command_seen['command'] == [str(program / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
                                      str(program / 'install_windows.py')]
    assert command_seen['cwd'] == program
    assert command_seen['stdin'] == subprocess.DEVNULL
    assert command_seen['env']['PIP_NO_INDEX'] == '1'
    assert 'PYTHONPATH' not in command_seen['env'] and 'PIP_INDEX_URL' not in command_seen['env']


probe = load('standard_installer_native_probe_test', ROOT / 'scripts/windows_standard_installer_smoke.py')


def test_native_probe_fixture_populates_a_real_library(tmp_path, monkeypatch):
    root = tmp_path / 'retained real SQLite library'
    monkeypatch.setattr(sys, 'argv', ['native-fixture', str(root)])
    exec(compile(probe.CREATE_LIBRARY, '<native-install-library-fixture>', 'exec'), {})
    assert list(root.glob('*.sqlite'))
    assert any(path.is_file() and 'Україна'.encode() in path.read_bytes()
               for path in root.rglob('*') if path.is_file() and path.suffix == '.txt')
    compile(probe.HOLD_APPLICATION, '<native-install-live-app-fixture>', 'exec')


def test_native_probe_waits_for_second_phase_before_closing_owned_job(tmp_path, monkeypatch):
    events = []
    counts = iter([1, 1, 0])
    child = SimpleNamespace(wait=lambda **_: events.append('parent exited') or 0)
    monkeypatch.setattr(probe, 'WindowsJobProcess', lambda *a, **k: child)
    monkeypatch.setattr(probe, 'stop_owned_process', lambda value: events.append('close whole owned job'))
    monkeypatch.setattr(probe, 'active_job_processes', lambda value: next(counts))
    monkeypatch.setattr(probe.time, 'sleep', lambda seconds: events.append('allow second-phase cleanup'))
    probe.owned_process(['synthetic-not-executed.exe'], cwd=tmp_path,
                        log=tmp_path / 'process.log', timeout=5)
    assert events == ['parent exited', 'allow second-phase cleanup', 'allow second-phase cleanup',
                      'close whole owned job']


def test_native_probe_stops_owned_tree_after_descendant_timeout(tmp_path, monkeypatch):
    events = []
    clock = iter([10, 15])
    child = SimpleNamespace(wait=lambda **_: 0)
    monkeypatch.setattr(probe, 'WindowsJobProcess', lambda *a, **k: child)
    monkeypatch.setattr(probe, 'stop_owned_process', lambda value: events.append('close whole owned job'))
    monkeypatch.setattr(probe, 'active_job_processes', lambda value: 1)
    monkeypatch.setattr(probe.time, 'monotonic', lambda: next(clock))
    with pytest.raises(TimeoutError, match='owned native installation tree'):
        probe.owned_process(['synthetic-not-executed.exe'], cwd=tmp_path,
                            log=tmp_path / 'process.log', timeout=2)
    assert events == ['close whole owned job']


def test_native_probe_never_queries_an_ambient_or_missing_job():
    with pytest.raises(RuntimeError, match='no live owned job'):
        probe.active_job_processes(SimpleNamespace(_carr_job=SimpleNamespace(handle=None)))


def test_nested_application_commands_share_one_actual_lease(installed, monkeypatch):
    from carr_library import installation_lease as leases
    root, _, _, _ = installed
    original, events = leases.installation_lease, []

    @contextmanager
    def observed(*args, **kwargs):
        events.append('enter')
        with original(*args, **kwargs):
            yield
        events.append('leave')

    monkeypatch.setattr(leases, 'installation_lease', observed)
    with leases.application_lease(prefix=root / 'app/.venv'):
        with leases.application_lease(prefix=root / 'app/.venv'):
            assert events == ['enter']
        assert events == ['enter']
        with pytest.raises(RuntimeError, match='Close Resonance'):
            with original(root, exclusive=True):
                pytest.fail('The outer operation lost its installation lease.')
    assert events == ['enter', 'leave']
    with original(root, exclusive=True):
        pass


def test_concurrent_application_jobs_keep_lease_until_the_last_finishes(installed):
    root, _, _, _ = installed
    ready = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def job(index):
        with application_lease(prefix=root / 'app/.venv'):
            ready[index].set()
            assert release[index].wait(5), 'The test must release every actual worker.'

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(job, i) for i in range(2)]
        try:
            assert all(event.wait(2) for event in ready)
            release[0].set()
            futures[0].result(2)
            with pytest.raises(RuntimeError, match='Close Resonance'):
                with installation_lease(root, exclusive=True):
                    pytest.fail('Maintenance was admitted while the second job was active.')
        finally:
            for event in release:
                event.set()
            for future in futures:
                future.result(3)
    with installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize('module,entry', [
    ('carr_library.__main__', 'main'),
    ('carr_library.extract.ocr_model_catalog', 'main'),
    ('carr_library.security.__main__', 'main'),
    ('carr_library.transcription_cli', 'main'),
    ('carr_library.studio.cli', 'main'),
    ('carr_library.studio.cli', 'assistant_main'),
], ids=['desktop-main', 'ocr-models', 'trust', 'transcription', 'studio', 'assistant'])
def test_every_supported_cli_refuses_work_during_maintenance(installed, monkeypatch, capsys, module, entry):
    root, _, _, _ = installed
    command = getattr(importlib.import_module(module), entry)
    monkeypatch.setattr(sys, 'prefix', str(root / 'app/.venv'))
    with installation_lease(root, exclusive=True):
        # Even argparse's successful --help exit must not run before this guard.
        assert command(['--help']) == 1
    assert 'Close Resonance' in capsys.readouterr().err


def test_shared_entrypoint_preserves_generator_arguments_and_operation_errors(installed, monkeypatch):
    from carr_library.installation_lease import installed_entrypoint
    root, _, _, _ = installed
    monkeypatch.setattr(sys, 'prefix', str(root / 'app/.venv'))

    @installed_entrypoint
    def command(arguments):
        assert arguments == ['literal', 'Україна']
        raise ValueError('Existing CLI error semantics are preserved.')

    with pytest.raises(ValueError, match='Existing CLI error'):
        command(value for value in ['literal', 'Україна'])
    with installation_lease(root, exclusive=True):
        pass


@pytest.mark.parametrize('state', ['absent-key', 'incomplete-key', 'complete-key'])
def test_native_probe_does_not_treat_incomplete_registry_as_an_empty_account(monkeypatch, state):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_key(root, name, reserved, flags):
        assert name == probe.REGISTRY
        if state == 'absent-key':
            raise FileNotFoundError('No product registration')
        return Key()

    def query(key, name):
        if state == 'incomplete-key':
            raise FileNotFoundError('An existing registration has no required value')
        return 'synthetic ' + name, 1

    module = SimpleNamespace(HKEY_CURRENT_USER=1, KEY_READ=2, KEY_WOW64_64KEY=4,
                             OpenKey=open_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, 'winreg', module)
    if state == 'incomplete-key':
        with pytest.raises(FileNotFoundError, match='existing registration'):
            probe.registry()
    elif state == 'absent-key':
        assert probe.registry() is None
    else:
        assert set(probe.registry()) == {'InstallLocation', 'UninstallString', 'ModifyPath', 'NoModify'}

@pytest.mark.parametrize('marker_name', ['portable-update.ini', 'PORTABLE-UPDATE.INI'])
@pytest.mark.parametrize('installed_bundle', [False, True])
def test_standard_setup_refuses_sealed_portable_profile_before_creating_output(
        tmp_path, payload, marker_name, installed_bundle):
    root, _expected = payload
    marker = root / marker_name
    marker.write_bytes(b'[Resonance portable updates]\nProtocol=1\nOwner=windows-job-v1\n')
    expected = reseal(root)
    if installed_bundle:
        synthetic_bootstrap(root)
    # The source inventory and manifest are genuinely sealed. Refusal must be
    # about incompatible lifecycle ownership, not an incidental hash mismatch.
    service.verifier.verify(root, manifest_bytes=(root / 'bundle-manifest.json').read_bytes())
    before = service.inventory(root)
    destination = tmp_path / 'must-not-create-setup'
    with pytest.raises(ValueError, match='Standard setup cannot wrap'):
        build.prepare(root, destination, manifest_sha256=expected,
                      installed_bundle=installed_bundle)
    assert not destination.exists()
    assert service.inventory(root) == before
    with pytest.raises(ValueError, match='Standard setup cannot wrap'):
        service.bundle(root, expected, allow_installed=installed_bundle)
    assert service.inventory(root) == before


@pytest.mark.parametrize('failure', ['launch', 'descendant-cleanup'])
def test_unconfirmed_bootstrap_retains_both_applications_receipts_and_durable_veto(
        installed, tmp_path, failure):
    """Real filesystem preservation; native acknowledgement has its own gate."""
    root, payload, expected, setup = installed
    (root / 'app/user-note.txt').write_bytes(b'retain user bytes with the old application')
    old_files = service.inventory(root / 'app')
    old_receipts = {name: (root / name).read_bytes() for name in (service.OWNER, service.MARKER)}
    library = tmp_path / 'external-library'
    library.mkdir()
    (library / 'content.sqlite').write_bytes(b'unchanged external library')
    library_before = service.inventory(library)

    def uncertain(program):
        receipt = json.loads((root / service.lease_module.BOOTSTRAP_OWNER).read_bytes())
        assert receipt['schema'] == 'carr-managed-bootstrap-owner/1'
        assert receipt['program'] == str(program.absolute()) and receipt['owner_pid'] == os.getpid()
        assert receipt['bundle_sha256'] == expected and receipt['setup_sha256'] == setup
        synthetic_bootstrap(program)
        (program / 'native-child-output.txt').write_bytes(b'unconfirmed output must stay at its path')
        raise service.BootstrapCleanupError('synthetic unconfirmed ' + failure)

    with pytest.raises(service.BootstrapCleanupError, match='unconfirmed'):
        service.install(payload, root, expected, setup, runner=uncertain)
    retained, = root.parent.glob(root.name + '-recovery-*')
    assert service.inventory(retained / 'app') == old_files
    assert {name: (retained / name).read_bytes() for name in old_receipts} == old_receipts
    assert {name: (root / name).read_bytes() for name in old_receipts} == old_receipts
    assert (root / 'app/native-child-output.txt').read_bytes() == b'unconfirmed output must stay at its path'
    assert not list(root.parent.glob(root.name + '-incomplete-*'))
    assert service.inventory(library) == library_before
    before = service.inventory(root)
    for operation in (
            lambda: service.check(root, expected, setup),
            lambda: service.install(payload, root, expected, setup, runner=synthetic_bootstrap),
            lambda: service.uninstall(root, expected, setup)):
        with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
            operation()
        assert service.inventory(root) == before
        assert service.inventory(retained / 'app') == old_files
        assert service.inventory(library) == library_before
    with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
        with application_lease(prefix=root / 'app/.venv'):
            pytest.fail('An unacknowledged native bootstrap cannot authorize application work.')
    assert service.inventory(root) == before


@pytest.mark.parametrize('name,kind', [
    ('bootstrap-owner.json', 'file'),
    ('BOOTSTRAP-OWNER.JSON', 'file'),
    ('bootstrap-owner.json', 'directory'),
])
def test_managed_bootstrap_receipt_presence_is_a_veto_not_pid_or_age_authority(
        installed, name, kind):
    root, payload, expected, setup = installed
    receipt = root / name
    if kind == 'directory':
        receipt.mkdir()
    else:
        # Malformed/stale data must never become an inferred cleanup receipt.
        receipt.write_bytes(b'{"owner_pid":999999999,"created":"1900-01-01"}')
    before = service.inventory(root)
    with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
        service.check(root, expected, setup)
    with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
        service.install(payload, root, expected, setup, runner=synthetic_bootstrap)
    with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
        service.uninstall(root, expected, setup)
    with pytest.raises(RuntimeError, match='did not confirm complete cleanup'):
        with application_lease(prefix=root / 'app/.venv'):
            pytest.fail('An unclean managed owner was admitted.')
    assert service.inventory(root) == before


def test_managed_bootstrap_cannot_remove_a_changed_owner_receipt(installed):
    root, payload, expected, setup = installed
    other_receipt = b'changed ownership proof must remain'
    old = service.inventory(root / 'app')

    def changed(program):
        synthetic_bootstrap(program)
        (root / service.lease_module.BOOTSTRAP_OWNER).write_bytes(other_receipt)

    with pytest.raises(service.BootstrapCleanupError, match='cleanup could not be recorded'):
        service.install(payload, root, expected, setup, runner=changed)
    assert (root / service.lease_module.BOOTSTRAP_OWNER).read_bytes() == other_receipt
    retained, = root.parent.glob(root.name + '-recovery-*')
    assert service.inventory(retained / 'app') == old
    assert (root / 'app/.venv/.installation-complete').is_file()
    assert not list(root.parent.glob(root.name + '-incomplete-*'))


def test_bootstrap_constructor_failure_is_uncertain_before_a_handle_is_returned(tmp_path, monkeypatch):
    def failed(*arguments, **keywords):
        raise OSError('synthetic creation failure after the native API boundary')
    monkeypatch.setattr(service, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(service, '_module',
                        lambda *args: SimpleNamespace(WindowsJobProcess=failed))
    with pytest.raises(service.BootstrapCleanupError, match='launch cleanup is unconfirmed') as failure:
        service._bootstrap(tmp_path / 'program')
    assert isinstance(failure.value.__cause__, OSError)


# CI orchestration is checked separately from the real Inno lifecycle above.
bundle_gate = load('windows_bundle_acceptance_test', ROOT / 'scripts/windows_bundle_acceptance.py')
BUNDLE_WORKFLOW = ROOT.parent / '.github/workflows/carr-windows.yml'


def bundle_exit_report():
    return dict(schema=1, checks=[
        dict(name=name, exit_code=0) for name in bundle_gate.REQUIRED_CHECKS
    ])


def bundle_workflow_block():
    value = BUNDLE_WORKFLOW.read_text(encoding='utf-8')
    marker = '      - name: Build, install and exercise the complete bundle with runtime egress blocked\n'
    assert value.count(marker) == 1
    return value, value.split(marker, 1)[1].split('\n      - name:', 1)[0]


def bundle_workflow_schedule(block):
    import re
    loop = re.search(r"foreach \(\$probe in @\(([^\n]+)\)\) \{", block)
    assert loop is not None
    probes = re.findall(r"'([^']+)'", loop.group(1))
    names = []
    for name, variable in re.findall(r"^\s+Add-BundleCheck (?:'([^']+)'|(\$probe)) \$LASTEXITCODE$", block, re.M):
        names.extend(probes if variable else [name])
    return names


def test_bundle_workflow_keeps_all_checks_before_required_verdict_and_publication():
    value, block = bundle_workflow_block()
    assert bundle_workflow_schedule(block) == list(bundle_gate.REQUIRED_CHECKS)
    assert len(bundle_gate.NATIVE_CHECKS) == 20
    assert len(bundle_gate.VALIDATION_CHECKS) == 8
    ledger = block.index('$bundleChecks = ')
    aggregate = block.index('& $python -I -B scripts/windows_bundle_acceptance.py ')
    pending = block[ledger:aggregate]
    assert 'if ($LASTEXITCODE -ne 0)' not in pending
    assert 'exit $LASTEXITCODE' not in pending
    for required in (
        'installed-epubcheck.json', 'installed-daisy-schemas.json',
        '$runInstalledTests = @', '--import-mode=importlib',
        'full-bundle-workflows.xml', 'full-bundle-workflows.json',
        'full-bundle-canonical-workflows.json', 'full-bundle-owned-lifetime.json',
        'sealed bundle unchanged after native inference and OCR workflows',
        'source-wheel-proof.json',
    ):
        assert required in pending
    verdict_tail = block[aggregate:]
    assert verdict_tail.index('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }') < verdict_tail.index(
        "Copy-Item -LiteralPath $testedManifestPath")
    assert verdict_tail.index('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }') < verdict_tail.index(
        "installed_bundle='PASS'")
    assert 'timeout-minutes: 35' in block
    assert '    timeout-minutes: 120\n' in value
    assert 'continue-on-error:' not in block


def test_independent_package_gates_require_verified_build_not_native_success():
    value, block = bundle_workflow_block()
    ready = '"verified_bundle_ready=true" | Out-File -FilePath $env:GITHUB_OUTPUT'
    assert block.count(ready) == 1
    before_ready = block.split(ready, 1)[0]
    assert before_ready.index("ci-results/full-bundle-build-verified.json") < before_ready.rindex(
        'if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }')
    assert 'module.verify(root)' in before_ready
    assert "manifest['bundled_model'] and manifest['bundled_ocr']" in before_ready
    assert "manifest['bundled_native_runtime'] and manifest['bundled_audio_runtime']" in before_ready
    assert '$manifestBefore' in before_ready
    assert '"CARR_CI_BUNDLE=$bundle"' in before_ready
    assert before_ready.index('module.verify(root)') < before_ready.index('"CARR_CI_BUNDLE=$bundle"')
    condition = ("        if: ${{ !cancelled() && "
                 "steps.complete_bundle.outputs.verified_bundle_ready == 'true' }}")
    for title, timeout in (
        ('Qualify the transferable library bundle without an audio payload', 15),
        ('Build pinned standard setup and execute actual install, repair and uninstall', 20),
    ):
        body = value.split('      - name: ' + title + '\n', 1)[1].split('\n      - name:', 1)[0]
        assert body.count(condition) == 1
        assert 'timeout-minutes: ' + str(timeout) in body
        assert 'steps.complete_bundle.conclusion' not in body
        assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in body
    assert '$bundle = $env:CARR_CI_TRANSFER_BUNDLE' in value
    assert '$bundle = $env:CARR_CI_BUNDLE' in value


def test_complete_zero_exit_inventory_allows_only_its_own_acceptance():
    result = bundle_gate.verdict(bundle_exit_report())
    assert result == dict(schema=1, status='PASS', completed=28, required=28,
                          native_required=20, validation_required=8, failed=[])
    assert 'installed_bundle' not in result


@pytest.mark.parametrize('name,code', [
    ('windows_transcription_smoke.py', 1),
    ('windows_epub_reading_smoke.py', 77),
    ('windows_desktop_integration_smoke.py', -1),
    ('epubcheck_acceptance.py', 1),
    ('daisy_schema_acceptance.py', 77),
    ('installed_pytest', 1),
    ('canonical_junit', 1),
    ('final_bundle_seal', 1),
    ('source_wheel_proof', 1),
])
def test_required_native_or_independent_failure_never_becomes_pass(name, code):
    report = bundle_exit_report()
    row = next(row for row in report['checks'] if row['name'] == name)
    row['exit_code'] = code
    result = bundle_gate.verdict(report)
    assert result['status'] == 'FAIL'
    assert result['completed'] == result['required'] == 28
    assert result['failed'] == [dict(name=name, exit_code=code)]


@pytest.mark.parametrize('mutation', [
    'missing', 'duplicate', 'reorder', 'unknown', 'boolean-exit',
    'string-exit', 'float-exit', 'large-exit', 'extra-field', 'boolean-schema',
])
def test_incomplete_or_ambiguous_bundle_exit_inventory_is_refused(mutation):
    report = bundle_exit_report()
    rows = report['checks']
    if mutation == 'missing':
        rows.pop(8)
    elif mutation == 'duplicate':
        rows[8] = dict(rows[0])
    elif mutation == 'reorder':
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == 'unknown':
        rows[0]['name'] = 'unreviewed_probe.py'
    elif mutation == 'extra-field':
        rows[0]['ignored_failure'] = True
    elif mutation == 'boolean-schema':
        report['schema'] = True
    else:
        rows[0]['exit_code'] = {
            'boolean-exit': False, 'string-exit': '0',
            'float-exit': 0.0, 'large-exit': 2 ** 80,
        }[mutation]
    with pytest.raises(ValueError):
        bundle_gate.verdict(report)


@pytest.mark.parametrize('kind', ['pass', 'failed-native', 'missing', 'duplicate-field', 'oversize'])
def test_actual_bundle_gate_cli_refuses_false_acceptance(tmp_path, kind):
    report = bundle_exit_report()
    expected = 0
    if kind == 'failed-native':
        report['checks'][8]['exit_code'] = 77
        expected = 1
    elif kind == 'missing':
        report['checks'].pop()
        expected = 1
    raw = json.dumps(report).encode('utf-8')
    if kind == 'duplicate-field':
        raw = raw.replace(b'"schema": 1', b'"schema": 1, "schema": 1', 1)
        expected = 1
    elif kind == 'oversize':
        raw = b' ' * (bundle_gate.MAX_REPORT_BYTES + 1)
        expected = 1
    selected = tmp_path / 'required-exits.json'
    selected.write_bytes(raw)
    result = subprocess.run(
        [sys.executable, '-I', '-B', str(ROOT / 'scripts/windows_bundle_acceptance.py'),
         str(selected)], capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == expected, result.stderr
    response = json.loads(result.stdout)
    assert response['status'] == ('PASS' if expected == 0 else 'FAIL')
    assert selected.read_bytes() == raw
    assert len(result.stdout) < 2048


def bundle_powershell_harness(block):
    """Use the actual ledger, loop, recording calls and final gate with controlled children."""
    import re
    import textwrap
    body = textwrap.dedent(block.split('        run: |\n', 1)[1])
    first = body.index('$bundleChecks = ')
    begin = body.index('# Retain every native failure', first)
    setup = body[first:begin]
    lines = body[begin:].splitlines()
    result = [setup]
    in_string = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith("= @'"):
            in_string = True
            continue
        if in_string:
            if stripped == "'@":
                in_string = False
            continue
        if stripped.startswith('& $python -I -B scripts/windows_bundle_acceptance.py '):
            result.extend([line, lines[index + 1]])
            break
        if stripped.startswith('foreach ($probe in @(') or stripped == '}':
            result.append(line)
        elif index + 1 < len(lines) and re.fullmatch(
                r"\s*Add-BundleCheck (?:'[^']+'|\$probe) \$LASTEXITCODE", lines[index + 1]):
            actual = lines[index + 1]
            name = re.search(r"Add-BundleCheck (.+) \$LASTEXITCODE", actual).group(1)
            indent = line[:len(line) - len(line.lstrip())]
            result.append(indent + "& $python -I -B 'controlled-child.py' " + name + " $failedName")
            result.append(actual)
    else:
        raise AssertionError('Actual required-check gate was not found.')
    program = '\n'.join(result) + '\n'
    assert bundle_workflow_schedule(block) == list(bundle_gate.REQUIRED_CHECKS)
    assert program.count('Add-BundleCheck ') == 14  # function plus twelve fixed calls and loop call
    return program


if sys.platform == 'win32':
    @pytest.mark.parametrize('failed', [
        '', 'windows_epub_reading_smoke.py', 'epubcheck_acceptance.py',
        'installed_pytest', 'source_wheel_proof',
    ], ids=['all-pass', 'native-failure', 'validator-failure', 'pytest-failure', 'wheel-failure'])
    def test_actual_powershell_keeps_independent_tail_and_refuses_false_wheel_pass(tmp_path, failed):
        _, block = bundle_workflow_block()
        script = bundle_powershell_harness(block)
        (tmp_path / 'scripts').mkdir()
        (tmp_path / 'ci-results').mkdir()
        (tmp_path / 'scripts/windows_bundle_acceptance.py').write_bytes(
            (ROOT / 'scripts/windows_bundle_acceptance.py').read_bytes())
        (tmp_path / 'controlled-child.py').write_text(
            "import json,pathlib,sys\n"
            "name=sys.argv[1]; failed=sys.argv[2] if len(sys.argv)>2 else ''\n"
            "with pathlib.Path('executed.jsonl').open('a',encoding='utf-8') as out:\n"
            " out.write(json.dumps(name)+'\\n')\n"
            "print(name,flush=True)\n"
            "raise SystemExit(77 if name==failed else 0)\n", encoding='utf-8')
        escaped_python = sys.executable.replace("'", "''")
        prefix = ("$ErrorActionPreference = 'Stop'\n"
                  "$python = '" + escaped_python + "'\n"
                  "$failedName = '" + failed + "'\n")
        target = tmp_path / 'actual-orchestration.ps1'
        target.write_text(prefix + script +
            "Set-Content 'tested-application-wheel.json' '{\"installed_bundle\":\"PASS\"}'\n",
            encoding='utf-8')
        result = subprocess.run(
            ['pwsh', '-NoLogo', '-NoProfile', '-File', str(target)],
            cwd=tmp_path, capture_output=True, timeout=60, check=False,
        )
        assert result.returncode == (1 if failed else 0), result.stdout + result.stderr
        executed = [json.loads(line) for line in
                    (tmp_path / 'executed.jsonl').read_text(encoding='utf-8').splitlines()]
        assert executed == list(bundle_gate.REQUIRED_CHECKS)
        report = json.loads((tmp_path / 'ci-results/full-bundle-required-exits.json').read_text(
            encoding='utf-8'))
        assert len(report['checks']) == 28
        response = json.loads((tmp_path / 'ci-results/full-bundle-required-verdict.json').read_text(
            encoding='utf-8'))
        assert response['status'] == ('FAIL' if failed else 'PASS')
        assert response['failed'] == ([dict(name=failed, exit_code=77)] if failed else [])
        assert (tmp_path / 'tested-application-wheel.json').exists() is (not failed)


def core_bundle_exit_report():
    return dict(schema=1, checks=[
        dict(name=name, exit_code=0) for name in bundle_gate.CORE_NATIVE_CHECKS
    ])


def core_bundle_workflow_lines(block):
    import re
    lines = block.splitlines()
    pairs = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r"\s*Add-CoreBundleCheck '([^']+)' \$LASTEXITCODE", line)
        if match:
            pairs.append((match.group(1), lines[index - 1]))
    return pairs


def test_early_native_probes_keep_their_exact_commands_and_independent_required_outcomes():
    _, block = bundle_workflow_block()
    pairs = core_bundle_workflow_lines(block)
    assert [name for name, _command in pairs] == list(bundle_gate.CORE_NATIVE_CHECKS)
    assert len(pairs) == 16
    for name, command in pairs:
        assert command.strip().startswith('& $installedPython -I -B ')
        assert name in command
        assert 'Tee-Object' in command
    first = block.index(pairs[0][1])
    last = block.index(pairs[-1][1])
    ready = block.index('"verified_bundle_ready=true"')
    assert block.index("ci-results/core-bundle-install.txt") < ready < first
    assert block.index("ci-results/core-bundle-self-test.json") < ready
    assert block.index("ci-results/full-bundle-audio-native.txt") < ready
    assert block.index("ci-results/installed-native-launcher.json") < ready
    assert block.index("ci-results/full-bundle-build-verified.json") < ready
    assert block.index("ci-results/full-bundle-verified.json") > last
    assert 'if ($LASTEXITCODE -ne 0)' not in block[first:last]
    assert 'exit $LASTEXITCODE' not in block[first:last]
    assert block.index("ci-results/full-bundle-verified.json") < block.index('$bundleChecks = ')
    assert block.index('$bundleChecks = ') < block.index(
        "& $bundledPython -I -B scripts/windows_bundle_acceptance.py --core ")
    assert block.index('$bundleCoreGateExit = $LASTEXITCODE') < block.index(
        "& $python -I -B scripts/windows_bundle_acceptance.py ")
    assert block.index('if ($bundleCoreGateExit -ne 0) { exit $bundleCoreGateExit }') < block.index(
        "Copy-Item -LiteralPath $testedManifestPath")
    assert block.index('if ($bundleCoreGateExit -ne 0) { exit $bundleCoreGateExit }') < block.index(
        "installed_bundle='PASS'")


def test_complete_core_inventory_is_separate_from_the_existing_tail_verdict():
    result = bundle_gate.core_verdict(core_bundle_exit_report())
    assert result == dict(schema=1, status='PASS', completed=16, required=16,
                          native_required=16, validation_required=0, failed=[])
    with pytest.raises(ValueError):
        bundle_gate.verdict(core_bundle_exit_report())
    with pytest.raises(ValueError):
        bundle_gate.core_verdict(bundle_exit_report())


@pytest.mark.parametrize('name', [
    'windows_v3_smoke.py', 'windows_assistant_smoke.py',
    'windows_automatic_ocr_smoke.py', 'windows_audio_metadata_smoke.py',
    'windows_workflow_templates_smoke.py', 'ocr_catalog_smoke.py',
    'windows_installer_launchers_smoke.py', 'probe_source_open_native.py',
    'windows_academic_smoke.py', 'native_academic_tools_smoke.py',
    'windows_ocr_models_smoke.py', 'probe_advanced_search_native.py',
    'windows_bibliography_interchange_smoke.py', 'windows_progress_smoke.py',
    'native_keyboard_settings_smoke.py', 'native_document_formats_smoke.py',
])
def test_each_early_native_failure_is_a_required_failure(name):
    report = core_bundle_exit_report()
    row = next(item for item in report['checks'] if item['name'] == name)
    row['exit_code'] = 77
    result = bundle_gate.core_verdict(report)
    assert result['status'] == 'FAIL'
    assert result['completed'] == result['required'] == 16
    assert result['failed'] == [dict(name=name, exit_code=77)]


@pytest.mark.parametrize('mutation', [
    'missing', 'duplicate', 'reorder', 'unknown', 'boolean-exit',
    'oversize-exit', 'extra-field',
])
def test_core_exit_report_rejects_missing_or_ambiguous_evidence(mutation):
    report = core_bundle_exit_report()
    rows = report['checks']
    if mutation == 'missing':
        rows.pop()
    elif mutation == 'duplicate':
        rows[-1] = dict(rows[0])
    elif mutation == 'reorder':
        rows[0], rows[-1] = rows[-1], rows[0]
    elif mutation == 'unknown':
        rows[0]['name'] = 'unreviewed.py'
    elif mutation == 'boolean-exit':
        rows[0]['exit_code'] = False
    elif mutation == 'oversize-exit':
        rows[0]['exit_code'] = 2 ** 80
    else:
        rows[0]['ignored_failure'] = True
    with pytest.raises(ValueError):
        bundle_gate.core_verdict(report)


@pytest.mark.parametrize('kind', ['pass', 'failed-book', 'missing', 'wrong-mode'])
def test_actual_core_gate_cli_keeps_its_failure_and_exact_mode(tmp_path, kind):
    report = core_bundle_exit_report()
    if kind == 'failed-book':
        report['checks'][0]['exit_code'] = 77
    elif kind == 'missing':
        report['checks'].pop()
    selected = tmp_path / 'core-exits.json'
    raw = json.dumps(report).encode('utf-8')
    selected.write_bytes(raw)
    mode = '--unreviewed' if kind == 'wrong-mode' else '--core'
    result = subprocess.run(
        [sys.executable, '-I', '-B', str(ROOT / 'scripts/windows_bundle_acceptance.py'),
         mode, str(selected)], capture_output=True, timeout=10, check=False,
    )
    expected = 0 if kind == 'pass' else 1
    assert result.returncode == expected, result.stderr
    response = json.loads(result.stdout)
    assert response['status'] == ('PASS' if expected == 0 else 'FAIL')
    assert selected.read_bytes() == raw
    assert len(result.stdout) < 2048


def complete_bundle_powershell_harness(block):
    """Exercise the actual two ledgers and both final gates with controlled children."""
    import re
    import textwrap
    body = textwrap.dedent(block.split('        run: |\n', 1)[1])
    setup_start = body.index('$bundleCoreChecks = ')
    setup_end = body.index('# Keep every original native command', setup_start)
    lines = body[setup_end:].splitlines()
    core = [body[setup_start:setup_end]]
    for index, line in enumerate(lines):
        if index + 1 >= len(lines):
            break
        match = re.fullmatch(r"\s*Add-CoreBundleCheck ('[^']+') \$LASTEXITCODE", lines[index + 1])
        if match:
            indent = line[:len(line) - len(line.lstrip())]
            core.append(indent + "& $python -I -B 'controlled-child.py' " +
                        match.group(1) + " $failedName")
            core.append(lines[index + 1])
    assert len([line for line in core if line.strip().startswith('Add-CoreBundleCheck ')]) == 16
    gate_start = body.index('& $bundledPython -I -B scripts/windows_bundle_acceptance.py --core ')
    gate_end = body.index('& $python -I -B scripts/windows_bundle_acceptance.py ', gate_start)
    core_gate = body[gate_start:gate_end]
    tail = bundle_powershell_harness(block)
    tail_gate = '& $python -I -B scripts/windows_bundle_acceptance.py '
    assert tail.count(tail_gate) == 1
    tail = tail.replace(tail_gate, core_gate + tail_gate, 1)
    guard = 'if ($bundleCoreGateExit -ne 0) { exit $bundleCoreGateExit }'
    assert body.count(guard) == 1
    return '\n'.join(core) + '\n' + tail + guard + '\n'


if sys.platform == 'win32':
    @pytest.mark.parametrize('failed', [
        '', 'windows_v3_smoke.py', 'native_document_formats_smoke.py',
        'windows_epub_reading_smoke.py',
    ], ids=['all-pass', 'early-book-failure', 'last-core-failure', 'tail-playback-failure'])
    def test_actual_powershell_runs_both_native_inventories_before_strict_acceptance(tmp_path, failed):
        _, block = bundle_workflow_block()
        script = complete_bundle_powershell_harness(block)
        (tmp_path / 'scripts').mkdir()
        (tmp_path / 'ci-results').mkdir()
        (tmp_path / 'scripts/windows_bundle_acceptance.py').write_bytes(
            (ROOT / 'scripts/windows_bundle_acceptance.py').read_bytes())
        (tmp_path / 'controlled-child.py').write_text(
            "import json,pathlib,sys\n"
            "name=sys.argv[1]; failed=sys.argv[2] if len(sys.argv)>2 else ''\n"
            "with pathlib.Path('executed.jsonl').open('a',encoding='utf-8') as out:\n"
            " out.write(json.dumps(name)+'\\n')\n"
            "print(name,flush=True)\n"
            "raise SystemExit(77 if name==failed else 0)\n", encoding='utf-8')
        escaped_python = sys.executable.replace("'", "''")
        prefix = ("$ErrorActionPreference = 'Stop'\n"
                  "$python = '" + escaped_python + "'\n"
                  "$bundledPython = $python\n"
                  "$failedName = '" + failed + "'\n")
        target = tmp_path / 'complete-orchestration.ps1'
        target.write_text(prefix + script +
            "Set-Content 'tested-application-wheel.json' '{\"installed_bundle\":\"PASS\"}'\n",
            encoding='utf-8')
        result = subprocess.run(
            ['pwsh', '-NoLogo', '-NoProfile', '-File', str(target)],
            cwd=tmp_path, capture_output=True, timeout=60, check=False,
        )
        assert result.returncode == (1 if failed else 0), result.stdout + result.stderr
        executed = [json.loads(line) for line in
                    (tmp_path / 'executed.jsonl').read_text(encoding='utf-8').splitlines()]
        assert executed == list(bundle_gate.CORE_NATIVE_CHECKS + bundle_gate.REQUIRED_CHECKS)
        core = json.loads((tmp_path / 'ci-results/full-bundle-core-verdict.json').read_text(
            encoding='utf-8'))
        tail = json.loads((tmp_path / 'ci-results/full-bundle-required-verdict.json').read_text(
            encoding='utf-8'))
        expected_core = 'FAIL' if failed in bundle_gate.CORE_NATIVE_CHECKS else 'PASS'
        expected_tail = 'FAIL' if failed in bundle_gate.REQUIRED_CHECKS else 'PASS'
        assert core['status'] == expected_core and core['completed'] == 16
        assert tail['status'] == expected_tail and tail['completed'] == 28
        assert core['failed'] == ([dict(name=failed, exit_code=77)] if expected_core == 'FAIL' else [])
        assert tail['failed'] == ([dict(name=failed, exit_code=77)] if expected_tail == 'FAIL' else [])
        assert (tmp_path / 'tested-application-wheel.json').exists() is (not failed)


# These controls use the actual Windows ShellLink COM interfaces and PowerShell,
# without executing a target executable or compiling/installing Inno itself.
if sys.platform == 'win32':
    SHORTCUT_FIXTURE = r"""param([string]$Folder,[string]$Targets)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
internal class FixtureShortcutObject {}

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface FixtureShortcutW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
                 int characters, IntPtr findData, uint flags);
    void GetIDList(out IntPtr list);
    void SetIDList(IntPtr list);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int characters);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int characters);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int characters);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetHotkey(out ushort hotkey);
    void SetHotkey(ushort hotkey);
    void GetShowCmd(out int command);
    void SetShowCmd(int command);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
                         int characters, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public static class FixtureShortcutWriter {
    public static void Create(string filename, string target) {
        object link = new FixtureShortcutObject();
        try {
            ((FixtureShortcutW)link).SetPath(target);
            ((IPersistFile)link).Save(filename, true);
        } finally {
            Marshal.FinalReleaseComObject(link);
        }
    }
}
'@
$targetsByName = Get-Content -LiteralPath $Targets -Raw -Encoding utf8 | ConvertFrom-Json
foreach ($entry in $targetsByName) {
    [FixtureShortcutWriter]::Create((Join-Path $Folder ($entry.name + '.lnk')), $entry.target)
}
"""

    @pytest.mark.parametrize('fault', [
        'none', 'wrong-main', 'wrong-repair', 'wrong-uninstall',
        'missing-link', 'missing-target', 'invalid-link',
    ])
    def test_actual_unicode_shelllink_inspection_keeps_exact_targets_and_refuses_wrong_ones(
            tmp_path, monkeypatch, fault):
        root = tmp_path / 'Resonance installed Ω Україна'
        appdata = tmp_path / 'isolated appdata Ω'
        folder = appdata / 'Microsoft/Windows/Start Menu/Programs/Resonance'
        folder.mkdir(parents=True)
        report = tmp_path / 'inspection'
        report.mkdir()
        monkeypatch.setenv('APPDATA', str(appdata))
        expected = {
            'Resonance': root / 'app/ResearchLibrary.exe',
            'Repair Resonance': root / 'maintenance/ResonanceSetup.exe',
            'Uninstall Resonance': root / 'unins000.exe',
        }
        for path in expected.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'Synthetic shortcut target; never executed.')
        actual = dict(expected)
        wrong_names = {
            'wrong-main': 'Resonance', 'wrong-repair': 'Repair Resonance',
            'wrong-uninstall': 'Uninstall Resonance',
        }
        if fault in wrong_names:
            name = wrong_names[fault]
            # A real adjacent ASCII-O directory must never stand in for Ω.
            wrong = tmp_path / 'Resonance installed O Україна' / expected[name].relative_to(root)
            wrong.parent.mkdir(parents=True, exist_ok=True)
            wrong.write_bytes(b'A different actual target; never executed.')
            actual[name] = wrong
        targets = tmp_path / 'shortcut-targets.json'
        targets.write_text(json.dumps([dict(name=name, target=str(target))
                                      for name, target in actual.items()]), encoding='ascii')
        fixture = tmp_path / 'create-native-shortcuts.ps1'
        fixture.write_text(SHORTCUT_FIXTURE, encoding='utf-8-sig')
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        probe.owned_process(
            [powershell, '-NoProfile', '-NonInteractive', '-File', fixture,
             '-Folder', folder, '-Targets', targets],
            cwd=tmp_path, log=tmp_path / 'create-shortcuts-process.log', timeout=60)
        selected = folder / 'Resonance.lnk'
        if fault == 'missing-link':
            selected.unlink()
        elif fault == 'missing-target':
            expected['Resonance'].unlink()
        elif fault == 'invalid-link':
            selected.write_bytes(b'This is not a native Shell link.')
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        if fault == 'none':
            assert probe.inspect_shortcuts(root, report) == folder
        else:
            message = ('shortcuts were not installed' if fault == 'missing-link'
                       else 'Native acceptance failed' if fault == 'invalid-link'
                       else 'targets another installation')
            with pytest.raises(RuntimeError, match=message):
                probe.inspect_shortcuts(root, report)
        assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
        if fault in ('missing-link', 'invalid-link'):
            assert not (report / 'shortcuts.json').exists()
            return
        observed = json.loads((report / 'shortcuts.json').read_text(encoding='utf-8-sig'))
        detail = json.loads((report / 'shortcut-inspection.json').read_text(encoding='utf-8-sig'))
        assert set(observed) == set(detail) == set(expected)
        for name, target in actual.items():
            assert Path(observed[name]).resolve() == target.resolve()
            assert detail[name]['unicode_target'] == observed[name]
            assert detail[name]['link_bytes'] == len(before[name + '.lnk'])
            assert detail[name]['link_sha256'] == checksum(before[name + '.lnk'])
            assert detail[name]['legacy_target'] is None or isinstance(detail[name]['legacy_target'], str)
            assert detail[name]['legacy_error'] is None or isinstance(detail[name]['legacy_error'], str)
        if fault == 'none':
            assert all('Ω Україна' in target for target in observed.values())
            assert all(path.is_file() for path in expected.values())
        elif fault in wrong_names:
            assert Path(observed[wrong_names[fault]]).resolve() != expected[wrong_names[fault]].resolve()
        else:
            assert fault == 'missing-target' and not expected['Resonance'].exists()


# This observer adds evidence only. The existing builder remains authoritative.
inventory_observer = load('standard_installer_inventory_observer_test',
    ROOT / 'packaging/windows_installer/bundle_difference.py')


def inventory_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def test_inventory_observation_matches_without_touching_the_installed_environment(payload):
    root, expected = payload
    synthetic_bootstrap(root)
    before = inventory_bytes(root)
    result = inventory_observer.inspect_bundle(root, expected)
    assert result['status'] == 'MATCH'
    assert result['counts'] == dict(missing=0, changed=0, undeclared=0)
    assert result['expected_files'] == result['actual_files'] == len(before) - 3
    assert result['excluded_top_directories'] == ['.venv']
    assert result['differences'] == []
    assert result['all_differences_sha256'] == checksum(b'')
    assert inventory_bytes(root) == before
    assert service.bundle(root, expected, allow_installed=True)['source_commit'] == 'a' * 40

    # The exclusion does not authorize similarly named directories elsewhere.
    nested = root / 'runtime/.venv/unreviewed.txt'
    nested.parent.mkdir()
    nested.write_bytes(b'Nested environment names are still payload inventory.')
    report = inventory_observer.inspect_bundle(root, expected)
    assert report['status'] == 'DIFFERENT'
    assert report['counts'] == dict(missing=0, changed=0, undeclared=1)
    assert report['differences'][0]['path'] == 'runtime/.venv/unreviewed.txt'
    with pytest.raises(ValueError, match='missing, changed, or undeclared'):
        service.bundle(root, expected, allow_installed=True)


@pytest.mark.parametrize('kind', ['missing', 'changed', 'undeclared'])
def test_inventory_observation_records_actual_differences_without_repair(payload, kind):
    root, expected = payload
    target = root / 'README.txt'
    old_digest = checksum(target.read_bytes())
    if kind == 'missing':
        target.unlink()
        new_digest = None
    elif kind == 'changed':
        target.write_bytes(b'Actual changed payload; the observer must retain it.')
        new_digest = checksum(target.read_bytes())
    else:
        target = root / 'runtime/Lib/__pycache__/observed.cpython-311.pyc'
        target.parent.mkdir()
        target.write_bytes(b'Non-executable fixture for an undeclared bytecode file.')
        old_digest, new_digest = None, checksum(target.read_bytes())
    before = inventory_bytes(root)
    result = inventory_observer.inspect_bundle(root, expected)
    row = dict(kind=kind, path=target.relative_to(root).as_posix(),
               expected_sha256=old_digest, actual_sha256=new_digest)
    assert result['status'] == 'DIFFERENT'
    assert result['counts'] == {name: int(name == kind)
                                for name in ('missing', 'changed', 'undeclared')}
    assert result['differences'] == [row]
    assert result['differences_truncated'] is False
    canonical = (json.dumps(row, ensure_ascii=True, sort_keys=True,
                            separators=(',', ':')) + '\n').encode('ascii')
    assert result['all_differences_sha256'] == checksum(canonical)
    assert result['expected_bundle_manifest_sha256'] == expected
    assert result['observed_bundle_manifest_sha256'] == expected
    assert inventory_bytes(root) == before
    with pytest.raises(ValueError, match='missing, changed, or undeclared'):
        service.bundle(root, expected, allow_installed=True)
    assert inventory_bytes(root) == before


def test_inventory_observation_bounds_rows_but_hashes_every_actual_difference(payload):
    root, expected = payload
    extra = root / 'extra'
    extra.mkdir()
    rows = []
    for number in range(131):
        path = extra / (f'{number:03d}-' + 'Ω' * 40 + '.txt')
        raw = f'Actual additional file {number}'.encode('ascii')
        path.write_bytes(raw)
        rows.append(dict(kind='undeclared', path=path.relative_to(root).as_posix(),
                         expected_sha256=None, actual_sha256=checksum(raw)))
    before = inventory_bytes(root)
    result = inventory_observer.inspect_bundle(root, expected)
    complete = b''.join((json.dumps(row, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')) + '\n').encode('ascii') for row in rows)
    assert result['status'] == 'DIFFERENT'
    assert result['counts'] == dict(missing=0, changed=0, undeclared=131)
    assert result['differences'] == rows[:128]
    assert result['differences_truncated'] is True
    assert result['all_differences_sha256'] == checksum(complete)
    assert len(json.dumps(result, ensure_ascii=True).encode('ascii')) < 1024 * 1024
    assert inventory_bytes(root) == before

    # A change beyond the emitted sample must still alter the complete receipt.
    final = root / rows[-1]['path']
    final.write_bytes(b'Changed only the unprinted final difference.')
    changed = inventory_observer.inspect_bundle(root, expected)
    assert changed['differences'] == result['differences']
    assert changed['counts'] == result['counts']
    assert changed['all_differences_sha256'] != result['all_differences_sha256']
    assert final.read_bytes() == b'Changed only the unprinted final difference.'


@pytest.mark.parametrize('fault', [
    'malformed', 'duplicate', 'unsafe-path', 'invalid-hash', 'wrong-identity',
])
def test_inventory_observation_unavailable_for_invalid_manifest(payload, fault):
    root, expected = payload
    manifest = root / 'bundle-manifest.json'
    original = manifest.read_bytes()
    document = json.loads(original)
    if fault == 'malformed':
        raw = b'{"files":'
    elif fault == 'duplicate':
        raw = b'{"files":{},"files":{}}'
    elif fault == 'unsafe-path':
        document['files']['../outside.txt'] = 'b' * 64
        raw = json.dumps(document).encode('utf-8')
    elif fault == 'invalid-hash':
        document['files']['README.txt'] = 'b' * 63
        raw = json.dumps(document).encode('utf-8')
    else:
        raw = original
    manifest.write_bytes(raw)
    expected = 'b' * 64 if fault == 'wrong-identity' else checksum(raw)
    before = inventory_bytes(root)
    result = inventory_observer.inspect_bundle(root, expected)
    assert result['status'] == 'UNAVAILABLE'
    assert result['counts'] is None and result['actual_files'] is None
    assert result['differences'] == [] and result['all_differences_sha256'] is None
    assert result['error_type'] and 0 < len(result['error']) <= 512
    assert inventory_bytes(root) == before
    with pytest.raises((ValueError, KeyError)):
        service.bundle(root, expected, allow_installed=True)


@pytest.mark.parametrize('name', ['.venv', 'runtime/linked'])
def test_inventory_observation_refuses_actual_linked_tree_before_exclusion(payload, tmp_path, name):
    root, expected = payload
    outside = tmp_path / 'outside preserved Ω'
    outside.mkdir()
    sentinel = outside / 'private.txt'
    sentinel.write_bytes(b'Unrelated bytes must remain unchanged.')
    link = root / name
    if sys.platform == 'win32':
        command = Path(os.environ['SystemRoot']) / 'System32/cmd.exe'
        result = subprocess.run([str(command), '/d', '/c', 'mklink', '/J',
            str(link), str(outside)], capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    else:
        link.symlink_to(outside, target_is_directory=True)
        assert link.is_symlink()
    result = inventory_observer.inspect_bundle(root, expected)
    assert result['status'] == 'UNAVAILABLE'
    assert result['counts'] is None and result['differences'] == []
    with pytest.raises(ValueError):
        service.bundle(root, expected, allow_installed=True)
    assert sentinel.read_bytes() == b'Unrelated bytes must remain unchanged.'
    assert (root / 'bundle-manifest.json').read_bytes()
    # Remove only this fixture's link; never traverse or delete its target.
    if sys.platform == 'win32':
        link.rmdir()
    else:
        link.unlink()
    assert sentinel.is_file()


@pytest.mark.parametrize('when', ['during-read', 'after-inventory'])
def test_inventory_observation_refuses_a_manifest_changed_during_observation(payload, monkeypatch, when):
    root, expected = payload
    manifest = root / 'bundle-manifest.json'
    original = manifest.read_bytes()
    assert original.count(b'a' * 40) == 1
    changed = original.replace(b'a' * 40, b'b' * 40, 1)
    before = manifest.stat()
    changes = []
    monkeypatch.setattr(inventory_observer, '_maintenance', lambda: service)

    def mutate():
        assert not changes
        manifest.write_bytes(changed)
        os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
        changes.append(True)

    if when == 'during-read':
        actual_fdopen = os.fdopen
        identity = (before.st_dev, before.st_ino)

        class ChangedAfterRead:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def fileno(self):
                return self.stream.fileno()
            def read(self, *args):
                raw = self.stream.read(*args)
                if not changes:
                    mutate()
                return raw

        def opened(descriptor, *args, **kwargs):
            stream = actual_fdopen(descriptor, *args, **kwargs)
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) == identity and not changes:
                return ChangedAfterRead(stream)
            return stream
        monkeypatch.setattr(os, 'fdopen', opened)
    else:
        actual_inventory = service.inventory
        def scan(*args, **kwargs):
            result = actual_inventory(*args, **kwargs)
            mutate()
            return result
        monkeypatch.setattr(service, 'inventory', scan)
    result = inventory_observer.inspect_bundle(root, expected)
    assert changes == [True]
    assert result['status'] == 'UNAVAILABLE'
    assert result['counts'] is None and result['differences'] == []
    assert result['all_differences_sha256'] is None
    assert 'changed' in result['error'].lower()
    assert manifest.read_bytes() == changed
    with pytest.raises(ValueError, match='installer identity'):
        service.bundle(root, expected, allow_installed=True)


@pytest.mark.parametrize('state', ['match', 'different', 'unavailable'])
def test_actual_inventory_cli_emits_only_bounded_observation_and_never_repairs(payload, state):
    root, expected = payload
    if state == 'different':
        (root / 'extra Ω.txt').write_bytes(b'Keep the observed extra file.')
    if state == 'unavailable':
        expected = 'b' * 64
    before = inventory_bytes(root)
    result = subprocess.run([sys.executable, '-I', '-B',
        str(ROOT / 'packaging/windows_installer/bundle_difference.py'),
        '--bundle', str(root), '--bundle-manifest-sha256', expected],
        capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout) < 1024 * 1024
    assert result.stdout.decode('ascii').count('\n') == 1
    report = json.loads(result.stdout)
    assert report['status'] == state.upper()
    assert report['scope'] == 'standard-installer-file-inventory'
    assert 'installed_bundle' not in report and report['status'] != 'PASS'
    assert inventory_bytes(root) == before


if sys.platform == 'win32':
    @pytest.mark.parametrize('fault,build_exit', [
        ('none', 0), ('observer-exit', 41), ('observer-launch', 41),
        ('observer-output', 41), ('none', 41),
    ], ids=['success', 'observer-exit', 'observer-launch', 'observer-output', 'builder-refusal'])
    def test_actual_inventory_powershell_always_reaches_original_builder(tmp_path, fault, build_exit):
        import textwrap

        workflow = (ROOT.parent / '.github/workflows/carr-windows.yml').read_text(encoding='utf-8')
        block = workflow.split(
            '      - name: Build pinned standard setup and execute actual install, repair and uninstall\n',
            1)[1].split('\n      - name:', 1)[0]
        body = textwrap.dedent(block.split('        run: |\n', 1)[1])
        start = body.index('$inventoryObservation = try {')
        command = '& $python -I -B -X utf8 packaging/windows_installer/build_setup.py '
        builder = body.index(command, start)
        finish = body.index('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }', builder)
        finish += len('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }')
        actual = body[start:finish]
        # Only the two external child programs are replaced. The real observer
        # exception/output handling and the original authoritative exit guard run.
        assert actual.count('packaging/windows_installer/bundle_difference.py') == 1
        assert actual.count('packaging/windows_installer/build_setup.py') == 1
        actual = actual.replace('packaging/windows_installer/bundle_difference.py', 'observer-child.py')
        actual = actual.replace('packaging/windows_installer/build_setup.py', 'builder-child.py')
        if fault == 'observer-launch':
            actual = actual.replace('& $python -I -B -X utf8 observer-child.py',
                                    "& './missing-observer.exe' -I -B -X utf8 observer-child.py")
        (tmp_path / 'ci-results').mkdir()
        receipt = tmp_path / 'ci-results/standard-installer-inventory.json'
        if fault == 'observer-output':
            receipt.mkdir()
        (tmp_path / 'observer-child.py').write_text(
            "import json,pathlib,sys\n"
            "with pathlib.Path('invoked.jsonl').open('a',encoding='utf-8') as out:\n"
            " out.write(json.dumps('observer')+'\\n')\n"
            "print(json.dumps(dict(schema=1,status='MATCH')),flush=True)\n"
            f"raise SystemExit({17 if fault == 'observer-exit' else 0})\n", encoding='utf-8')
        (tmp_path / 'builder-child.py').write_text(
            "import json,pathlib,sys\n"
            "with pathlib.Path('invoked.jsonl').open('a',encoding='utf-8') as out:\n"
            " out.write(json.dumps('builder')+'\\n')\n"
            "pathlib.Path('builder-arguments.json').write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\n"
            "print('AUTHORITATIVE BUILDER EXECUTED',flush=True)\n"
            f"raise SystemExit({build_exit})\n", encoding='utf-8')
        quoted = sys.executable.replace("'", "''")
        prefix = ("$ErrorActionPreference = 'Stop'\n"
                  "$python = '" + quoted + "'\n"
                  "$bundle = 'fixed-bundle'\n$manifest = '" + 'a' * 64 + "'\n"
                  "$buildRoot = 'fixed-build'\n$compilerRoot = 'fixed-compiler'\n")
        target = tmp_path / 'actual-inventory-orchestration.ps1'
        target.write_text(prefix + actual + "\nexit 0\n", encoding='utf-8')
        result = subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-File', str(target)],
            cwd=tmp_path, capture_output=True, timeout=30, check=False)
        assert result.returncode == build_exit, result.stdout + result.stderr
        invoked = [json.loads(line) for line in
            (tmp_path / 'invoked.jsonl').read_text(encoding='utf-8').splitlines()]
        assert invoked == (['builder'] if fault == 'observer-launch' else ['observer', 'builder'])
        arguments = json.loads((tmp_path / 'builder-arguments.json').read_text(encoding='utf-8'))
        assert arguments[:5] == ['--bundle', 'fixed-bundle', '--bundle-manifest-sha256',
                                 'a' * 64, '--installed-bundle']
        assert b'AUTHORITATIVE BUILDER EXECUTED' in result.stdout
        if fault == 'observer-output':
            assert receipt.is_dir()
            assert b'"status":"UNAVAILABLE"' in result.stdout
        else:
            report = json.loads(receipt.read_text(encoding='utf-8-sig'))
            assert report['status'] == ('UNAVAILABLE' if fault.startswith('observer-') else 'MATCH')


@pytest.mark.parametrize('log_kind', ['utf8', 'utf16', 'bounded', 'unreadable'])
def test_shortcut_child_failure_keeps_cause_and_bounded_actual_log(tmp_path, monkeypatch, log_kind):
    appdata = tmp_path / 'appdata'
    folder = appdata / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    folder.mkdir(parents=True)
    for name in ('Resonance', 'Repair Resonance', 'Uninstall Resonance'):
        (folder / (name + '.lnk')).write_bytes(b'Owned diagnostic fixture; never followed.')
    monkeypatch.setenv('APPDATA', str(appdata))
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'windows'))
    report = tmp_path / 'inspection'
    report.mkdir()
    marker = 'Native acceptance failed at shortcuts-process.log: exit 1'
    original = RuntimeError(marker)
    text = 'Actual child diagnostic: \u03a9 \u0423\u043a\u0440\u0430\u0457\u043d\u0430\n'
    raw = (text.encode('utf-16') if log_kind == 'utf16'
           else text.encode('utf-8'))
    if log_kind == 'bounded':
        raw = b'bounded-head\n' + b'x' * (256 * 1024) + b'UNREAD-TAIL'
    observed = []

    def failed_child(command, *, cwd, log, timeout):
        observed.append((cwd, log, timeout))
        if log_kind == 'unreadable':
            log.mkdir()
        else:
            log.write_bytes(raw)
        raise original

    monkeypatch.setattr(probe, 'owned_process', failed_child)
    with pytest.raises(RuntimeError, match=marker) as failure:
        probe.inspect_shortcuts(tmp_path / 'installation', report)
    assert failure.value.__cause__ is original
    assert observed == [(report, report / 'shortcuts-process.log', 30)]
    message = str(failure.value)
    assert message.isascii()
    detail = json.loads(message.split('; child log: ', 1)[1])
    assert detail['log'] == 'shortcuts-process.log'
    if log_kind == 'unreadable':
        assert detail['status'] == 'UNAVAILABLE'
        assert detail['read_error'] in ('IsADirectoryError', 'PermissionError')
        assert 'text' not in detail and 'captured_sha256' not in detail
        assert (report / 'shortcuts-process.log').is_dir()
    else:
        captured = raw[:16384]
        assert detail['status'] == 'READ'
        assert detail['captured_bytes'] == len(captured)
        assert detail['captured_sha256'] == checksum(captured)
        assert detail['truncated'] is (len(raw) > 16384)
        assert (report / 'shortcuts-process.log').read_bytes() == raw
        if log_kind == 'bounded':
            assert detail['text'].startswith('bounded-head\n')
            assert 'UNREAD-TAIL' not in message
            assert len(detail['text']) == 16384
            assert len(message) < 17000
        else:
            assert detail['text'] == text
    assert not (report / 'shortcuts.json').exists()
    assert not (report / 'shortcut-inspection.json').exists()
