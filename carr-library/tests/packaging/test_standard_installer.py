"""Real filesystem/lease guards; bootstrap and PE bytes are explicit fixtures.

No test here claims native Inno execution. The separate Windows acceptance
script compiles and installs the existing actual verified offline bundle.
"""
import base64
import csv
import errno
import io
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
from types import FunctionType, SimpleNamespace
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


def _git(repo, *arguments):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME='Synthetic Installer Test',
        GIT_AUTHOR_EMAIL='installer@example.invalid',
        GIT_COMMITTER_NAME='Synthetic Installer Test',
        GIT_COMMITTER_EMAIL='installer@example.invalid')
    return subprocess.check_output(['git', '-C', str(repo), *arguments],
        env=environment).decode().strip()


def _wheel(path, files):
    record = 'universal_research_library-3.0.0.dist-info/RECORD'
    files = {name: raw for name, raw in files.items() if name != record}
    rows = [[name, 'sha256=' + base64.urlsafe_b64encode(
        hashlib.sha256(raw).digest()).decode().rstrip('='), str(len(raw))]
        for name, raw in files.items()]
    rows.append([record, '', ''])
    output = io.StringIO(newline='')
    csv.writer(output, lineterminator='\n').writerows(rows)
    with ZipFile(path, 'w') as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
        archive.writestr(record, output.getvalue().encode())


@pytest.fixture(scope='module')
def committed_application(tmp_path_factory):
    """A real small Git/wheel boundary; no synthetic PE is executed."""
    repo = tmp_path_factory.mktemp('installer-committed-source')
    project = repo / 'carr-library'
    package = project / 'src/carr_library'
    package.mkdir(parents=True)
    (project / 'pyproject.toml').write_text(
        '[project]\nname = "universal-research-library"\nversion = "3.0.0"\n'
        'license-files = ["LICENSE"]\n'
        '[project.scripts]\nresearch-library = "carr_library.__main__:main"\n'
        '[tool.setuptools]\npackages = ["carr_library", "carr_library.audio"]\n'
        'package-dir = {"" = "src"}\n'
        '[tool.setuptools.package-data]\n"*" = ["*.sql", "*.md"]\n',
        encoding='utf-8')
    (project / 'LICENSE').write_bytes(b'Synthetic installer source fixture license.\n')
    (package / '__init__.py').write_bytes(b'"""Synthetic source-bound installer fixture."""\n')
    for name in ('installation_lease.py', 'managed_installation.py', '__main__.py'):
        (package / name).write_bytes((ROOT / 'src/carr_library' / name).read_bytes())
    (package / 'audio').mkdir()
    (package / 'audio/__init__.py').write_bytes(b'"""Synthetic package recipe; exact owners below."""\n')
    for name in service.PROCESS_HELPERS:
        (package / 'audio' / name).write_bytes((ROOT / 'src/carr_library/audio' / name).read_bytes())
    (package / 'schema.sql').write_bytes(b'CREATE TABLE installer_fixture (id INTEGER);\n')
    (package / 'README.md').write_bytes(b'Purpose-written source-bound installer fixture.\n')
    _git(repo, 'init', '-q')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'Exact synthetic installer source')
    commit = _git(repo, 'rev-parse', 'HEAD')
    files = {'carr_library/' + path.relative_to(package).as_posix(): path.read_bytes()
             for path in package.rglob('*') if path.is_file()}
    prefix = 'universal_research_library-3.0.0.dist-info/'
    files.update({
        prefix + 'METADATA': b'Metadata-Version: 2.4\nName: universal-research-library\nVersion: 3.0.0\n',
        prefix + 'WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        prefix + 'entry_points.txt': b'[console_scripts]\nresearch-library = carr_library.__main__:main\n',
        prefix + 'licenses/LICENSE': (project / 'LICENSE').read_bytes(),
    })
    return repo, commit, files


def reseal(root, *, source_commit=None):
    manifest = root / 'bundle-manifest.json'
    if source_commit is None:
        source_commit = json.loads(manifest.read_bytes())['source_commit']
    files = {p.relative_to(root).as_posix(): checksum(p.read_bytes())
             for p in root.rglob('*') if p.is_file() and p.name != 'bundle-manifest.json'}
    raw = (json.dumps(dict(schema=1, target='Windows x64 / CPython 3.11',
        bundled_python=True, bundled_model=False, bundled_ocr=False, bundled_audio=False,
        source_commit=source_commit, files=files), sort_keys=True) + '\n').encode()
    manifest.write_bytes(raw)
    return checksum(raw)


@pytest.fixture
def payload(tmp_path, monkeypatch, committed_application):
    root = tmp_path / 'synthetic sealed bundle Ω'
    root.mkdir()
    for name in ('install.lock', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd',
                 'README.txt', 'runtime/python.exe', 'runtime/Lib/os.py',
                 'ResearchLibrary.exe'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('SYNTHETIC, NOT EXECUTABLE: ' + name).encode())
    (root / 'install_windows.py').write_bytes((ROOT / 'packaging/install_windows.py').read_bytes())
    wheels = root / 'wheelhouse'
    wheels.mkdir()
    repo, commit, files = committed_application
    monkeypatch.setattr(build, 'SOURCE_REPOSITORY', repo)
    _wheel(wheels / 'universal_research_library-3.0.0-py3-none-any.whl', files)
    return root, reseal(root, source_commit=commit)

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
    (root / 'app/foreign notes.txt').write_text('user notes Ω', encoding='utf-8')
    external = tmp_path / 'outside-user-library'
    external.mkdir()
    (external / 'content.db').write_bytes(b'private-data sentinel')
    outside_hash = service.sha(external / 'content.db')
    result = service.install(payload, root, expected, setup, runner=synthetic_bootstrap)
    recovery = root.parent / result['retained_previous']
    assert result['operation'] == 'repair'
    assert (recovery / 'app/README.txt').read_bytes().startswith(b'changed by the user')
    assert (recovery / 'app/foreign notes.txt').read_text(encoding='utf-8') == 'user notes Ω'
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
    (external / 'book.txt').write_text('original Unicode prose Ω\f[pause 2]\r\n', encoding='utf-8')
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
        with pytest.raises(RuntimeError, match='Close Resonance'):
            with application_lease(['--self-test'], prefix=root / 'app/.venv'):
                pytest.fail('A secondary entrypoint bypassed the incomplete installation.')
        for arguments in ([], ['--data-dir', 'foreign'], ['--self-test', '--data-dir', 'foreign']):
            with pytest.raises(RuntimeError, match='Close Resonance'):
                with application_lease(arguments, prefix=root / 'app/.venv'):
                    pytest.fail('unready installed entrypoint admitted user-data access')
    for arguments in ([], ['--self-test'], ['--data-dir', 'foreign']):
        with pytest.raises(RuntimeError, match='incomplete'):
            with application_lease(arguments, prefix=root / 'app/.venv'):
                pytest.fail('Releasing maintenance did not make an incomplete environment ready.')


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
    assert 'AppPublisher=Sceneria Innovative Technologies' in script.splitlines()
    assert 'CloseApplications=no' in script
    assert 'Repair Resonance' in script and 'ModifyPath' in script
    assert '[UninstallDelete]' not in script and 'filesandordirs' not in script
    assert '[Dirs]\nName: "{app}\\maintenance"; Flags: uninsalwaysuninstall\n' in script
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
    for name in ('process.py', 'windows_process.py', 'models.py', 'cleanup.py'):
        assert service.HELPERS[name] == ROOT / 'src/carr_library/audio' / name


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
    upgrade, ordinary = prepare.split("if not RunHelper('preflight', False) then begin", 1)
    assert upgrade.index("RunHelper('upgrade-check', True)") < upgrade.index("RunHelper('upgrade', True)")
    assert upgrade.index("RunHelper('upgrade', True)") < upgrade.index('Prepared := True;')
    assert ordinary.index("RunHelper('install', True)") < ordinary.index('Prepared := True;')
    assert upgrade.count('Prepared := True;') == ordinary.count('Prepared := True;') == 1
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
        def wait(self, *, timeout):
            events.append(('wait', timeout))
            if outcome == 'timeout':
                raise subprocess.TimeoutExpired('synthetic bootstrap', timeout)
            return 9 if outcome == 'nonzero' else 0

    @contextmanager
    def start(command, **kwargs):
        command_seen.update(command=command, **kwargs)
        assert kwargs['stdout'] is kwargs['stderr']
        assert kwargs['stdout'].writable() and kwargs['stdout'].readable()
        assert stat.S_ISREG(os.fstat(kwargs['stdout'].fileno()).st_mode)
        try:
            yield Child()
        finally:
            events.append('confirm whole owned job')

    environment = {'PYTHONPATH': 'untrusted', 'PIP_INDEX_URL': 'https://invalid.test',
                   'SystemRoot': 'fixed platform directory'}
    from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
    api = SimpleNamespace(owned_process=start, AudioCleanupPending=AudioCleanupPending,
                          pending_owners=_pending_owners)
    monkeypatch.setattr(service, 'os', SimpleNamespace(name='nt', environ=environment))
    monkeypatch.setattr(service, '_process_api', lambda: api)
    program = tmp_path / 'program'
    if outcome == 'success':
        service._bootstrap(program)
    else:
        expected_error = subprocess.TimeoutExpired if outcome == 'timeout' else subprocess.CalledProcessError
        with pytest.raises(expected_error):
            service._bootstrap(program)
    assert events == [('wait', 1800), 'confirm whole owned job']
    assert command_seen['command'] == [str(program / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
                                      str(program / 'install_windows.py')]
    assert command_seen['cwd'] == program
    assert command_seen['stdin'] == subprocess.DEVNULL
    assert command_seen['stdout'].closed
    assert command_seen['env']['PIP_NO_INDEX'] == '1'
    assert 'PYTHONPATH' not in command_seen['env'] and 'PIP_INDEX_URL' not in command_seen['env']

def test_captured_native_helpers_load_as_one_private_package_without_installed_app(tmp_path, monkeypatch):
    helpers = tmp_path / 'standalone helpers'
    helpers.mkdir()
    for name in service.PROCESS_HELPERS:
        (helpers / name).write_bytes(service.HELPERS[name].read_bytes())
    monkeypatch.setattr(service, 'process_source', helpers / 'process.py')
    api = service._process_api()
    prefix = api.owned_process.__module__.rsplit('.', 1)[0]
    assert prefix.startswith('_resonance_installer_process_')
    assert api.AudioCleanupPending.__module__ == prefix + '.cleanup'
    assert api.pending_cleanup.__module__ == prefix + '.cleanup'
    assert api.pending_owners.__module__ == prefix + '.cleanup'
    assert service._process_api().owned_process is api.owned_process
    for name in service.PROCESS_HELPERS:
        module = importlib.import_module(prefix + '.' + name[:-3])
        assert Path(module.__file__) == helpers / name
        assert (helpers / name).read_bytes() == service.HELPERS[name].read_bytes()
    assert api.AudioCleanupPending is sys.modules[prefix + '.cleanup'].AudioCleanupPending

@pytest.mark.parametrize('phase', ['constructor', 'wait'])
def test_native_cleanup_keeps_repair_tree_and_exclusive_lease_until_confirmed(installed, monkeypatch, phase):
    from carr_library.audio.cleanup import CleanupOwner, AudioCleanupPending, _pending_owners
    root, payload, expected, setup = installed
    original = service.inventory(root / 'app')
    blocked, release = threading.Event(), threading.Event()
    calls, streams = [], []

    def native_close():
        calls.append('cleanup')
        blocked.set()
        return release.is_set()

    owner = CleanupOwner(native_close)

    class Child:
        def wait(self, *, timeout):
            raise subprocess.TimeoutExpired('synthetic bootstrap', timeout)

    @contextmanager
    def start(command, **kwargs):
        streams.append(kwargs['stdout'])
        assert kwargs['stdout'] is kwargs['stderr']
        if phase == 'constructor':
            blocked.set()
            raise AudioCleanupPending('Synthetic startup cleanup pending.', cleanup_owner=owner)
        try:
            yield Child()
        finally:
            if not native_close():
                raise AudioCleanupPending('Synthetic exit cleanup pending.', cleanup_owner=owner)

    api = SimpleNamespace(owned_process=start, pending_owners=_pending_owners,
                          AudioCleanupPending=AudioCleanupPending)
    monkeypatch.setattr(service, '_process_api', lambda: api)
    # Override only this function's platform view, preserving the real
    # filesystem/lease module for the concurrent ownership assertions.
    runner = FunctionType(service._bootstrap.__code__,
        dict(service._bootstrap.__globals__,
             os=SimpleNamespace(name='nt', environ=os.environ)),
        service._bootstrap.__name__)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.install, payload, root, expected, setup, runner=runner)
        try:
            assert blocked.wait(5), 'The synthetic native child must reach pending cleanup.'
            assert not future.done()
            assert all(not stream.closed for stream in streams)
            # The newly published program must remain at its exact path while
            # the native bootstrap owns it; no incomplete/rollback rename yet.
            assert (root / 'app/runtime/python.exe').exists()
            assert not list(root.parent.glob(root.name + '-incomplete-*'))
            recovery = list(root.parent.glob(root.name + '-recovery-*'))
            assert len(recovery) == 1
            assert service.inventory(recovery[0] / 'app') == original
            with pytest.raises(RuntimeError, match='Close Resonance'):
                with installation_lease(root, exclusive=True):
                    pytest.fail('Maintenance ownership was released before native shutdown.')
        finally:
            release.set()
        error = AudioCleanupPending if phase == 'constructor' else subprocess.TimeoutExpired
        with pytest.raises(error):
            future.result(5)
    assert owner.quiescent_event.is_set()
    assert streams and all(stream.closed for stream in streams)
    assert service.inventory(root / 'app') == original
    with installation_lease(root, exclusive=True):
        pass

def test_interrupt_during_native_cleanup_is_delivered_only_after_quiescence(monkeypatch):
    class Event:
        ready = False
        def is_set(self):
            return self.ready
        def wait(self, seconds):
            pass

    class Owner:
        def __init__(self):
            self.quiescent_event = Event()
            self.attempts = 0
        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise KeyboardInterrupt()
            self.quiescent_event.ready = True

    owner = Owner()
    with pytest.raises(KeyboardInterrupt):
        service._wait_for_cleanup(owner)
    assert owner.quiescent_event.is_set()
    assert owner.attempts == 2


probe = load('standard_installer_native_probe_test', ROOT / 'scripts/windows_standard_installer_smoke.py')


@pytest.fixture
def shortcut_targets(tmp_path):
    root = tmp_path / 'Managed Resonance Україна Ω'
    values = {}
    for name, relative in {'Resonance': 'app/ResearchLibrary.exe',
                           'Repair Resonance': 'maintenance/ResonanceSetup.exe',
                           'Uninstall Resonance': 'unins000.exe'}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic target identity, never executed')
        values[name] = str(path)
    return root, values


def test_shortcut_inspection_retains_exact_unicode_and_real_target_identity(shortcut_targets):
    root, values = shortcut_targets
    probe.validate_shortcut_targets(root, values)
    assert 'Україна Ω' in values['Resonance']


@pytest.mark.parametrize('name', ['Resonance', 'Repair Resonance', 'Uninstall Resonance'],
                         ids=['application', 'repair', 'uninstall'])
def test_shortcut_inspection_refuses_an_existing_foreign_target(shortcut_targets, tmp_path, name):
    root, values = shortcut_targets
    other = tmp_path / 'Another installation' / Path(values[name]).name
    other.parent.mkdir()
    other.write_bytes(Path(values[name]).read_bytes())
    values[name] = str(other)
    with pytest.raises(RuntimeError, match='targets another installation'):
        probe.validate_shortcut_targets(root, values)


@pytest.mark.parametrize('kind', ['lossy-ansi', 'missing', 'directory', 'relative'])
def test_shortcut_inspection_does_not_repair_or_guess_a_target(shortcut_targets, kind):
    root, values = shortcut_targets
    if kind == 'lossy-ansi':
        values['Resonance'] = values['Resonance'].replace('Україна Ω', '??????? O')
    elif kind == 'missing':
        Path(values['Resonance']).unlink()
    elif kind == 'directory':
        values['Resonance'] = str(root / 'app')
    else:
        values['Resonance'] = 'app/ResearchLibrary.exe'
    with pytest.raises(RuntimeError, match='targets another installation'):
        probe.validate_shortcut_targets(root, values)


@pytest.mark.parametrize('kind', ['list', 'missing-key', 'extra-key', 'empty', 'null', 'nul', 'long'])
def test_shortcut_inspection_requires_the_complete_typed_inventory(shortcut_targets, kind):
    root, values = shortcut_targets
    if kind == 'list':
        values = list(values.values())
    elif kind == 'missing-key':
        del values['Repair Resonance']
    elif kind == 'extra-key':
        values['Other program'] = values['Resonance']
    else:
        values['Resonance'] = {'empty': '', 'null': None, 'nul': '\x00', 'long': 'x' * 32768}[kind]
    with pytest.raises(RuntimeError, match='inventory|invalid target'):
        probe.validate_shortcut_targets(root, values)


def test_native_shortcut_probe_keeps_unicode_report_from_owned_inspection(
        shortcut_targets, tmp_path, monkeypatch):
    root, values = shortcut_targets
    appdata = tmp_path / 'Roaming Україна Ω'
    folder = appdata / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    folder.mkdir(parents=True)
    for name in values:
        (folder / (name + '.lnk')).write_bytes(b'Synthetic link placeholder; COM is not run here')
    monkeypatch.setenv('APPDATA', str(appdata))
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    report = tmp_path / 'report'
    report.mkdir()
    invocations = []

    def inspected(command, *, cwd, log, timeout):
        invocations.append(command)
        assert cwd == report and timeout == 30
        assert Path(command[command.index('-Folder') + 1]) == folder
        output = Path(command[command.index('-Output') + 1])
        output.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8-sig')
        log.write_text('Synthetic process boundary, no native acceptance claim')

    monkeypatch.setattr(probe, 'owned_process', inspected)
    assert probe.inspect_shortcuts(root, report) == folder
    assert len(invocations) == 1
    assert json.loads((report / 'shortcuts.json').read_text(encoding='utf-8-sig')) == values
    assert Path(values['Resonance']).read_bytes() == b'Synthetic target identity, never executed'


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

    @contextmanager
    def owned(*args, **kwargs):
        try:
            yield child
        finally:
            events.append('close whole owned job')

    monkeypatch.setattr(probe, 'native_owned_process', owned)
    monkeypatch.setattr(probe, 'active_job_processes', lambda value: next(counts))
    monkeypatch.setattr(probe.time, 'sleep', lambda seconds: events.append('allow second-phase cleanup'))
    probe.owned_process([tmp_path / 'synthetic-not-executed.exe'], cwd=tmp_path,
                        log=tmp_path / 'process.log', timeout=5)
    assert events == ['parent exited', 'allow second-phase cleanup', 'allow second-phase cleanup',
                      'close whole owned job']

def test_native_probe_stops_owned_tree_after_descendant_timeout(tmp_path, monkeypatch):
    events = []
    clock = iter([10, 15])
    child = SimpleNamespace(wait=lambda **_: 0)

    @contextmanager
    def owned(*args, **kwargs):
        try:
            yield child
        finally:
            events.append('close whole owned job')

    monkeypatch.setattr(probe, 'native_owned_process', owned)
    monkeypatch.setattr(probe, 'active_job_processes', lambda value: 1)
    monkeypatch.setattr(probe.time, 'monotonic', lambda: next(clock))
    with pytest.raises(TimeoutError, match='owned native installation tree'):
        probe.owned_process([tmp_path / 'synthetic-not-executed.exe'], cwd=tmp_path,
                            log=tmp_path / 'process.log', timeout=2)
    assert events == ['close whole owned job']

def test_native_probe_never_queries_an_ambient_or_missing_job():
    with pytest.raises(RuntimeError, match='no live owned job'):
        probe.active_job_processes(SimpleNamespace(_carr_job=None))


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
    ('carr_library.backup_incremental_cli', 'main'),
    ('carr_library.bibliography_cli', 'main'),
    ('carr_library.delivery_cli', 'main'),
    ('carr_library.extract.ocr_model_catalog', 'main'),
    ('carr_library.native_checks', 'main'),
    ('carr_library.operations_cli', 'main'),
    ('carr_library.streamed_import_cli', 'main'),
    ('carr_library.studio.book_cli', 'main'),
    ('carr_library.studio.cli', 'main'),
    ('carr_library.studio.cli', 'assistant_main'),
    ('carr_library.sync.cli', 'main'),
    ('carr_library.transcription_cli', 'main'),
    ('carr_library.translation_cli', 'main'),
])
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




# The bundle's actual complete source verifier remains authoritative. These
# small fixtures use a real Git commit and a complete RECORD, never a mocked
# "trusted" wheel or a second source inventory.
WHEEL_HELPERS = {
    'installation_lease.py': 'carr_library/installation_lease.py',
    'managed_installation.py': 'carr_library/managed_installation.py',
    **{name: 'carr_library/audio/' + name for name in service.PROCESS_HELPERS},
}


def test_prepared_helpers_match_exact_verified_wheel_members(tmp_path, payload, committed_application):
    bundle, expected = payload
    _, commit, members = committed_application
    output = tmp_path / 'verified exact helper preparation'
    record = build.prepare(bundle, output, manifest_sha256=expected)
    assert record['source_commit'] == commit
    for name, member in WHEEL_HELPERS.items():
        assert (output / 'helpers' / name).read_bytes() == members[member]
        assert record['maintenance_sha256'][name] == checksum(members[member])
    assert record['windows_installation_verified'] is False
    assert json.loads((output / 'setup-build.json').read_bytes()) == record


@pytest.mark.parametrize('name', tuple(WHEEL_HELPERS))
def test_stale_live_helper_is_refused_before_setup_output(tmp_path, payload, monkeypatch, name):
    bundle, expected = payload
    stale = tmp_path / ('stale-' + name)
    original = service.HELPERS[name].read_bytes()
    stale.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    monkeypatch.setitem(service.HELPERS, name, stale)
    output = tmp_path / 'must not prepare stale helper'
    with pytest.raises(ValueError, match='helper differs from the source-verified'):
        build.prepare(bundle, output, manifest_sha256=expected)
    assert not output.exists()
    assert stale.read_bytes() != original


@pytest.mark.parametrize('name', tuple(WHEEL_HELPERS))
@pytest.mark.parametrize('moment', ['own-copy', 'last-copy'])
def test_changed_helper_copy_cannot_receive_source_commit_receipt(
    tmp_path, payload, monkeypatch, name, moment
):
    bundle, expected = payload
    output = tmp_path / 'changed captured helper'
    target = output / 'helpers' / name
    original_copy = build.shutil.copyfile
    last_name = tuple(service.HELPERS)[-1]
    changed = []

    def copy(source, destination, *args, **kwargs):
        result = original_copy(source, destination, *args, **kwargs)
        trigger = target if moment == 'own-copy' else output / 'helpers' / last_name
        if Path(destination) == trigger:
            raw = target.read_bytes()
            target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            changed.append(target)
        return result

    monkeypatch.setattr(build.shutil, 'copyfile', copy)
    with pytest.raises(ValueError, match='Copied installer helper differs'):
        build.prepare(bundle, output, manifest_sha256=expected)
    assert changed == [target]
    assert not (output / 'setup-build.json').exists()
    assert not (output / 'ResonanceSetup.iss').exists()


def test_verified_wheel_digest_survives_the_helper_snapshot_reread(
    tmp_path, payload, monkeypatch, committed_application
):
    bundle, expected = payload
    _, _, members = committed_application
    wheel = build._application_wheel(bundle)
    original_verify = build.source_verifier.verify_application_wheel
    verified = []

    def verify(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        verified.append(result['wheel_sha256'])
        changed = dict(members)
        changed['carr_library/audio/process.py'] += b'\n# changed after real whole-source verification\n'
        _wheel(wheel, changed)
        return result

    monkeypatch.setattr(build.source_verifier, 'verify_application_wheel', verify)
    output = tmp_path / 'must not accept a different reread'
    with pytest.raises(ValueError, match='wheel changed before helper capture'):
        build.prepare(bundle, output, manifest_sha256=expected)
    assert verified and checksum(wheel.read_bytes()) != verified[0]
    assert not output.exists()


def test_resealed_but_uncommitted_owner_bytes_fail_actual_source_verification(
    tmp_path, payload, committed_application
):
    bundle, _ = payload
    _, _, members = committed_application
    changed = dict(members)
    changed['carr_library/audio/process.py'] += b'\n# not part of the captured Git commit\n'
    _wheel(build._application_wheel(bundle), changed)
    expected = reseal(bundle)
    output = tmp_path / 'must not accept uncommitted helper bytes'
    with pytest.raises(ValueError):
        build.prepare(bundle, output, manifest_sha256=expected)
    assert not output.exists()


@pytest.mark.parametrize('target', ['maintenance', 'probe'])
@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('phase', ['announcement', 'initial-status', 'close'])
def test_all_causal_cleanup_owners_settle_before_delivering_interruption(
    target, signal_type, phase, monkeypatch
):
    from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner, _pending_owners
    events = []
    signal = signal_type('Purpose-written cleanup interruption')
    raised = [False]

    def finish_first():
        events.append('first-close')
        if phase == 'close' and not raised[0]:
            raised[0] = True
            raise signal
        return True

    def finish_second():
        events.append('second-close')
        return True

    first, second = CleanupOwner(finish_first), CleanupOwner(finish_second)
    if phase == 'announcement':
        def announce(*args, **kwargs):
            if not raised[0]:
                raised[0] = True
                events.append('announcement-interruption')
                raise signal
        monkeypatch.setattr(service if target == 'maintenance' else probe,
                            'print', announce, raising=False)
    if phase == 'initial-status':
        actual = first.quiescent_event

        class StatusSignal:
            def is_set(self):
                if not raised[0]:
                    raised[0] = True
                    events.append('initial-status-interruption')
                    raise signal
                return actual.is_set()

            def set(self):
                return actual.set()

            def wait(self, timeout=None):
                return actual.wait(timeout)

        first.quiescent_event = StatusSignal()
    outer = AudioCleanupPending('First causal cleanup', cleanup_owner=first)
    outer.__cause__ = AudioCleanupPending('Second causal cleanup', cleanup_owner=second)
    assert _pending_owners(outer) == (first, second)
    with pytest.raises(signal_type) as caught:
        if target == 'maintenance':
            service._wait_for_pending(outer, SimpleNamespace(pending_owners=_pending_owners))
        else:
            probe.wait_pending_cleanup(outer)
    assert caught.value is signal and caught.value.__cause__ is outer
    assert raised[0] and first.quiescent and second.quiescent
    assert events[-1] == 'second-close'
    assert events.count('first-close') == (2 if phase == 'close' else 1)


@pytest.mark.parametrize('masked', [False, True])
def test_completed_bootstrap_retains_stream_then_preserves_success_after_pending_cleanup(
    tmp_path, monkeypatch, masked
):
    from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner, _pending_owners
    released = threading.Event()
    pending = threading.Event()
    streams = []
    owner = CleanupOwner(released.is_set)

    class Child:
        def wait(self, *, timeout):
            assert timeout == 1800
            return 0

    @contextmanager
    def start(command, **kwargs):
        streams.append(kwargs['stdout'])
        try:
            yield Child()
        finally:
            pending.set()
            error = AudioCleanupPending('The completed bootstrap still owns native cleanup.',
                                        cleanup_owner=owner)
            if masked:
                raise OSError('Cleanup context wraps the same pending owner.') from error
            raise error

    api = SimpleNamespace(owned_process=start, pending_owners=_pending_owners,
                          AudioCleanupPending=AudioCleanupPending)
    monkeypatch.setattr(service, '_process_api', lambda: api)
    runner = FunctionType(service._bootstrap.__code__,
        dict(service._bootstrap.__globals__, os=SimpleNamespace(name='nt', environ=os.environ)),
        service._bootstrap.__name__)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner, tmp_path / 'completed bootstrap')
        try:
            assert pending.wait(5)
            assert not future.done() and streams and not streams[0].closed
        finally:
            released.set()
        if masked:
            # A different work/finalization error is still authoritative; only
            # the positively completed direct pending result can be reconciled.
            with pytest.raises(OSError, match='Cleanup context wraps'):
                future.result(5)
        else:
            assert future.result(5) is None
    assert owner.quiescent and all(stream.closed for stream in streams)


def _replace_bundle_manifest(root, document):
    raw = (json.dumps(document, sort_keys=True) + '\n').encode('utf-8')
    (root / 'bundle-manifest.json').write_bytes(raw)
    return checksum(raw)


def test_current_launcher_inventory_without_donor_flag_is_prepared(tmp_path, payload):
    root, expected = payload
    before = (root / 'bundle-manifest.json').read_bytes()
    manifest = json.loads(before)
    assert manifest['target'] == 'Windows x64 / CPython 3.11'
    assert set(manifest) == {'schema', 'target', 'bundled_python', 'bundled_model',
                             'bundled_ocr', 'bundled_audio', 'source_commit', 'files'}
    assert 'bundled_launcher' not in manifest
    assert 'native-launcher-build.json' not in manifest['files']
    assert manifest['files']['ResearchLibrary.exe'] == checksum(
        (root / 'ResearchLibrary.exe').read_bytes())
    assert service.bundle(root, expected) == manifest

    output = tmp_path / 'current-schema-setup'
    result = build.prepare(root, output, manifest_sha256=expected)
    assert result['bundle_manifest_sha256'] == expected
    assert result['source_commit'] == manifest['source_commit']
    assert result['status'] == 'SOURCE_PREPARED'
    assert result['windows_installation_verified'] is False
    assert (output / 'payload/ResearchLibrary.exe').read_bytes() == (
        root / 'ResearchLibrary.exe').read_bytes()
    assert (output / 'payload/bundle-manifest.json').read_bytes() == before
    assert (root / 'bundle-manifest.json').read_bytes() == before


@pytest.mark.parametrize('change', [
    'missing', 'missing-resealed', 'undeclared', 'tampered',
    'wrong-digest', 'directory', 'hardlink',
])
def test_current_launcher_inventory_refuses_missing_or_changed_payload_before_output(
    tmp_path, payload, change
):
    root, expected = payload
    launcher = root / 'ResearchLibrary.exe'
    original = launcher.read_bytes()
    foreign = tmp_path / 'foreign-launcher.exe'
    if change in {'missing', 'missing-resealed', 'directory'}:
        launcher.unlink()
        if change == 'directory':
            launcher.mkdir()
        if change == 'missing-resealed':
            expected = reseal(root)
    elif change == 'undeclared':
        manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
        del manifest['files']['ResearchLibrary.exe']
        expected = _replace_bundle_manifest(root, manifest)
    elif change == 'tampered':
        launcher.write_bytes(original + b'\nchanged after bundle sealing\n')
    elif change == 'wrong-digest':
        manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
        manifest['files']['ResearchLibrary.exe'] = '0' * 64
        expected = _replace_bundle_manifest(root, manifest)
    else:
        foreign.write_bytes(original)
        launcher.unlink()
        os.link(foreign, launcher)
    before = {p.relative_to(root).as_posix(): p.read_bytes()
              for p in root.rglob('*') if p.is_file()}
    directories = sorted(p.relative_to(root).as_posix()
                         for p in root.rglob('*') if p.is_dir())
    output = tmp_path / 'must-not-prepare-changed-launcher'
    with pytest.raises(ValueError, match='launcher bundle|missing, changed, or undeclared|unlinked'):
        build.prepare(root, output, manifest_sha256=expected)
    assert not output.exists()
    assert {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()} == before
    assert sorted(p.relative_to(root).as_posix()
                  for p in root.rglob('*') if p.is_dir()) == directories
    if change == 'hardlink':
        assert foreign.read_bytes() == original
        assert os.path.samefile(launcher, foreign)
        assert launcher.stat().st_nlink == 2


@pytest.mark.parametrize(('field', 'value'), [
    pytest.param('target', 'win_amd64', id='donor-target'),
    pytest.param('target', 'Windows ARM64 / CPython 3.11', id='wrong-architecture'),
    pytest.param('bundled_python', False, id='no-bundled-python'),
    pytest.param('bundled_python', 1, id='nonboolean-python'),
    pytest.param('files', [], id='untyped-file-inventory'),
])
def test_standard_setup_requires_current_native_bundle_contract_before_output(
    tmp_path, payload, field, value
):
    root, _ = payload
    manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
    manifest[field] = value
    expected = _replace_bundle_manifest(root, manifest)
    before = (root / 'bundle-manifest.json').read_bytes()
    output = tmp_path / 'must-not-prepare-incompatible-bundle'
    with pytest.raises(ValueError, match='complete Python/native launcher bundle'):
        build.prepare(root, output, manifest_sha256=expected)
    assert not output.exists()
    assert (root / 'bundle-manifest.json').read_bytes() == before


def test_donor_launcher_flag_cannot_replace_the_actual_owned_launcher(tmp_path, payload):
    root, _ = payload
    (root / 'ResearchLibrary.exe').unlink()
    reseal(root)
    manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
    manifest['bundled_launcher'] = True
    expected = _replace_bundle_manifest(root, manifest)
    output = tmp_path / 'must-not-trust-declared-launcher'
    with pytest.raises(ValueError, match='complete Python/native launcher bundle'):
        build.prepare(root, output, manifest_sha256=expected)
    assert not output.exists()
    assert not (root / 'ResearchLibrary.exe').exists()


@pytest.fixture
def uninstall_poststate_fixture(tmp_path):
    root = tmp_path / 'synthetic installed directory'
    report = tmp_path / 'native receipts'
    root.mkdir()
    report.mkdir()
    identity = dict(source_commit='a' * 40, setup_sha256='b' * 64,
                    bundle_manifest_sha256='c' * 64)
    return root, report, identity


@pytest.mark.parametrize('kind', ['absent', 'directory', 'file'])
def test_native_uninstall_poststate_records_only_fixed_metadata(
    uninstall_poststate_fixture, monkeypatch, kind
):
    root, report, identity = uninstall_poststate_fixture
    (root / 'app').mkdir()
    private = root / 'app/private-user-sentinel.txt'
    private.write_bytes(b'do not inspect or alter this content')
    maintenance = root / 'maintenance'
    if kind == 'directory':
        maintenance.mkdir()
    elif kind == 'file':
        maintenance.write_bytes(b'foreign replacement must remain untouched')
    original_lstat, observed, registry_calls = Path.lstat, [], []

    def fixed_lstat(path):
        assert path in (root / 'app', maintenance)
        observed.append(path.name)
        return original_lstat(path)

    def registration():
        registry_calls.append(True)
        return {'InstallLocation': 'PRIVATE VALUE MUST NOT ENTER THE REPORT'}

    def forbidden(*args, **kwargs):
        pytest.fail('Post-uninstall diagnostics read content or traversed directories.')

    with monkeypatch.context() as guard:
        guard.setattr(probe, 'registry', registration)
        guard.setattr(Path, 'lstat', fixed_lstat)
        for method in ('read_bytes', 'read_text', 'iterdir', 'rglob', 'glob'):
            guard.setattr(Path, method, forbidden)
        result = probe.record_uninstall_state(root, report, identity)

    raw = (report / 'uninstall-poststate.json').read_bytes()
    assert json.loads(raw) == result
    assert len(raw) < 2048 and b'PRIVATE VALUE' not in raw
    assert result['observation'] == 'after_owned_uninstall'
    assert all(result[key] == value for key, value in identity.items())
    assert result['registration'] == {'present': True}
    assert result['paths']['app'] == dict(present=True, directory=True,
        regular_file=False, symbolic_link=False, reparse_point=False)
    expected = {'present': False} if kind == 'absent' else dict(
        present=True, directory=kind == 'directory', regular_file=kind == 'file',
        symbolic_link=False, reparse_point=False)
    assert result['paths']['maintenance'] == expected
    assert observed == ['app', 'maintenance'] and registry_calls == [True]
    assert private.read_bytes() == b'do not inspect or alter this content'
    if kind == 'file':
        assert maintenance.read_bytes() == b'foreign replacement must remain untouched'


def test_native_uninstall_poststate_keeps_incomplete_registry_and_stat_errors_distinct(
    uninstall_poststate_fixture, monkeypatch
):
    root, report, identity = uninstall_poststate_fixture
    original_lstat = Path.lstat

    def registration():
        raise FileNotFoundError(errno.ENOENT, 'Private required registry value is missing')

    def denied(path):
        if path == root / 'maintenance':
            raise PermissionError(errno.EACCES, 'Private path must not enter the report')
        return original_lstat(path)

    monkeypatch.setattr(probe, 'registry', registration)
    monkeypatch.setattr(Path, 'lstat', denied)
    result = probe.record_uninstall_state(root, report, identity)
    assert result['registration'] == dict(present=None, error=dict(
        type='FileNotFoundError', errno=errno.ENOENT, winerror=None))
    assert result['paths']['app'] == {'present': False}
    assert result['paths']['maintenance'] == dict(present=None, error=dict(
        type='PermissionError', errno=errno.EACCES, winerror=None))
    raw = (report / 'uninstall-poststate.json').read_bytes()
    assert json.loads(raw) == result and b'Private' not in raw
    # An observation error is not converted into an absent installation.
    with pytest.raises(FileNotFoundError, match='required registry value'):
        probe.registry()


def test_native_uninstall_poststate_does_not_replace_an_existing_receipt(
    uninstall_poststate_fixture, monkeypatch
):
    root, report, identity = uninstall_poststate_fixture
    receipt = report / 'uninstall-poststate.json'
    receipt.write_bytes(b'keep the previous receipt exactly')
    monkeypatch.setattr(probe, 'registry', lambda: None)
    with pytest.raises(FileExistsError):
        probe.record_uninstall_state(root, report, identity)
    assert receipt.read_bytes() == b'keep the previous receipt exactly'


def cache_runtime_supplement(bundle, tmp_path, monkeypatch):
    """Use the real native verifier with explicit synthetic PE/file pins."""
    from carr_library import native_runtime as native

    runtime = tmp_path / 'synthetic native cache supplement'
    binary = bytearray(134)
    binary[:2] = b'MZ'
    binary[60:64] = (128).to_bytes(4, 'little')
    binary[128:] = b'PE\0\0\x64\x86'
    pins = {name: (len(binary), checksum(binary)) for name in native.DLLS}
    monkeypatch.setattr(native, 'DLLS', pins)
    for name in native.FILES:
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binary if name.startswith('bin/') else b'Synthetic cache-copy fixture\n')
    source = {'selected_original_files': {
        'Library/bin/' + name: {'destination': 'bin/' + name, 'size': size, 'sha256': sha}
        for name, (size, sha) in pins.items()}}
    lock = runtime / 'provenance/source-lock.json'
    lock.write_text(json.dumps(source), encoding='utf-8')
    monkeypatch.setattr(native, 'SOURCE_LOCK_SHA256', checksum(lock.read_bytes()))
    (runtime / native.MANIFEST).write_text(json.dumps({
        'schema': 1, 'target': 'Windows x64', 'use_scope': 'private-local-installation',
        'source_lock_sha256': native.SOURCE_LOCK_SHA256,
        'files': {name: checksum((runtime / name).read_bytes()) for name in native.FILES},
    }), encoding='utf-8')
    host_pins = {}
    for name in native.HOST_DLLS:
        path = bundle / 'runtime' / name
        path.write_bytes(('Synthetic host cache-copy fixture ' + name).encode('ascii'))
        host_pins[name] = checksum(path.read_bytes())
    monkeypatch.setattr(native, 'HOST_DLLS', host_pins)
    wheel, = (bundle / 'wheelhouse').glob('universal_research_library-*.whl')
    # Keep this native-supplement fixture's RECORD coherent; the setup case
    # below retains the separate exact committed-source wheel fixture.
    with ZipFile(wheel) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    for name in ('native_runtime.py', 'native_checks.py'):
        files['carr_library/' + name] = (ROOT / 'src/carr_library' / name).read_bytes()
    _wheel(wheel, files)
    native.verify_native_runtime(runtime)
    return runtime


def import_real_timestamp_cache(root):
    # Protect the executing test host's stdlib. Enable writes only while the
    # independent child imports the no-import fixture module in the copied Lib.
    code = """
import importlib, json, pathlib, sys
library = pathlib.Path(sys.argv[1]) / 'runtime' / 'Lib'
sys.path.insert(0, str(library))
sys.dont_write_bytecode = False
try:
    module = importlib.import_module('carr_payload_cache_probe')
    assert module.VALUE == 'sealed cache'
    assert pathlib.Path(module.__file__).parent == library
    enabled = not sys.dont_write_bytecode
finally:
    sys.dont_write_bytecode = True
print(json.dumps({'value': module.VALUE, 'bytecode_enabled_for_fixture': enabled}))
"""
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(root)],
                            check=True, capture_output=True, text=True, timeout=20)
    assert json.loads(result.stdout) == {
        'value': 'sealed cache', 'bytecode_enabled_for_fixture': True}


@pytest.mark.parametrize('copy_stage', ['native-supplement', 'standard-payload'])
def test_payload_copy_preserves_real_timestamp_pyc_and_still_rejects_rewrite(
        tmp_path, payload, monkeypatch, copy_stage):
    import py_compile

    root, _ = payload
    source = root / 'runtime/Lib/carr_payload_cache_probe.py'
    source.write_bytes(b"VALUE = 'sealed cache'\n")
    # The fixed old time makes copyfile's metadata loss observable even when
    # compilation, staging and import all happen in the same wall-clock second.
    os.utime(source, (946684800, 946684800))
    cache = Path(py_compile.compile(str(source), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))
    original_cache = cache.read_bytes()
    assert int.from_bytes(original_cache[4:8], 'little') == 0
    assert int.from_bytes(original_cache[8:12], 'little') == 946684800
    original_source_time = source.stat().st_mtime_ns
    if copy_stage == 'native-supplement':
        runtime = cache_runtime_supplement(root, tmp_path, monkeypatch)
    expected = reseal(root)
    original_manifest = (root / 'bundle-manifest.json').read_bytes()
    if copy_stage == 'native-supplement':
        monkeypatch.setattr(sys, 'path', list(sys.path))
        supplement = load('runtime_cache_supplement_test', ROOT / 'packaging/add_native_runtime.py')
        copied = tmp_path / 'complete cache bundle'
        supplement.attach_runtime(root, runtime, copied)
    else:
        prepared = tmp_path / 'prepared cache setup'
        build.prepare(root, prepared, manifest_sha256=expected)
        copied = prepared / 'payload'
    copied_source = copied / source.relative_to(root)
    copied_cache = copied / cache.relative_to(root)
    assert copied_cache.read_bytes() == original_cache
    service.verifier.verify(copied)
    import_real_timestamp_cache(copied)
    assert copied_cache.read_bytes() == original_cache
    assert copied_source.stat().st_mtime_ns == original_source_time
    service.verifier.verify(copied)
    assert cache.read_bytes() == original_cache
    assert source.stat().st_mtime_ns == original_source_time
    assert (root / 'bundle-manifest.json').read_bytes() == original_manifest
    service.verifier.verify(root)

    # A real stale cache is still rewritten by Python and rejected by the same
    # unchanged sealed-file verifier; cache files were not ignored or resealed.
    os.utime(copied_source, (946684860, 946684860))
    import_real_timestamp_cache(copied)
    assert copied_cache.read_bytes() != original_cache
    with pytest.raises(ValueError, match='bundle checksum failed: runtime/Lib/__pycache__/'):
        service.verifier.verify(copied)
