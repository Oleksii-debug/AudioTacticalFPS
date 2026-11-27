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

    class Child:
        def __init__(self):
            self.calls = 0
            self._carr_job = SimpleNamespace(close=lambda: events.append('close owned job'))

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
    assert events == [('wait', 1800), 'close owned job', ('wait', 5)]
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
