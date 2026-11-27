"""Synthetic signed targets and real managed filesystem/lease recovery.

The setup executable, registry and bootstrap are explicit public fixtures.
These tests do not claim native Inno execution, signing authority or NVDA.
"""
import base64
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.installation_lease import application_lease, installation_lease
from carr_library.library_backup import restore_backup
from carr_library.managed_installation import decode_proof, encode_proof, PROOF_FILE, PROOF_SCHEMA
from carr_library.security._files import TrustError, atomic_write, json_bytes
from carr_library.security.anchors import Anchor
from carr_library.security.managed_installation import (
    ManagedUpdateTransaction, WindowsManagedSetupAdapter, setup_receipt, UNINSTALL_FILES)
from carr_library.security.updates import LocalRepositoryClient
from carr_library.security.windows_registration import LINKS, validate_snapshot

from .conftest import MemoryAnchor, idle


ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load('managed_upgrade_test_builder', ROOT / 'packaging/windows_installer/build_setup.py')
maintenance = builder.maintenance
target_builder = _load('managed_upgrade_target_builder', ROOT / 'packaging/windows_installer/create_update_target.py')


def _payload(root, version, commit):
    root.mkdir()
    for name in ('install.lock', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd',
                 'README.txt', 'runtime/python.exe', 'runtime/Lib/os.py',
                 'ResearchLibrary.exe', 'native-launcher-build.json'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('SYNTHETIC NONEXECUTABLE ' + version + ': ' + name).encode())
    (root / 'version.txt').write_text(version)
    (root / 'install_windows.py').write_bytes((ROOT / 'packaging/install_windows.py').read_bytes())
    wheels = root / 'wheelhouse'
    wheels.mkdir()
    with ZipFile(wheels / ('universal_research_library-' + version + '-py3-none-any.whl'), 'w') as archive:
        archive.writestr('universal_research_library-' + version + '.dist-info/METADATA',
            'Metadata-Version: 2.3\nName: universal-research-library\nVersion: ' + version + '\n')
        for name in ('installation_lease.py', '__main__.py'):
            archive.writestr('carr_library/' + name, (ROOT / 'src/carr_library' / name).read_bytes())
    files = {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
             for path in root.rglob('*') if path.is_file()}
    raw = json_bytes(dict(schema=1, target='win_amd64', bundled_python=True,
                         bundled_launcher=True, source_commit=commit, files=files))
    (root / 'bundle-manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _bootstrap(program):
    scripts = program / '.venv/Scripts'
    scripts.mkdir(parents=True)
    (scripts / 'python.exe').write_bytes(b'SYNTHETIC interpreter; never executed')
    (program / '.venv/pyvenv.cfg').write_bytes(b'SYNTHETIC environment')
    (program / '.venv/.installation-complete').write_bytes(b'')


class FixtureRegistration:
    """Actual bounded files stand in for registry/shortcuts, not a production fallback."""
    def __init__(self, root, directory):
        self.root, self.directory = root, directory
        directory.mkdir()
        self.path = directory / 'registration.json'
        self.menu_root = directory / 'menu'
        self.menu_root.mkdir()
        self.write('3.1.0')

    def write(self, version):
        values = {
            'InstallLocation': {'type': 1, 'value': str(self.root)},
            'DisplayName': {'type': 1, 'value': 'Resonance'},
            'DisplayVersion': {'type': 1, 'value': version},
            'ModifyPath': {'type': 1, 'value': '"' + str(self.root / 'maintenance/ResonanceSetup.exe') + '"'},
            'UninstallString': {'type': 1, 'value': '"' + str(self.root / 'unins000.exe') + '"'},
            'UnrelatedUserNote': {'type': 1, 'value': 'Retain this exact metadata'},
        }
        atomic_write(self.path, json_bytes(values))
        for name in LINKS:
            atomic_write(self.menu_root / name, ('Synthetic shortcut ' + version + ': ' + name).encode())

    def snapshot(self, root, *, allow_partial=False):
        assert root == self.root
        if not allow_partial:
            assert self.path.is_file() and self.menu_root.is_dir()
        value = dict(schema=1, root=str(root), menu_root=str(self.menu_root),
            registry_present=self.path.is_file(),
            values=json.loads(self.path.read_bytes()) if self.path.is_file() else {},
            shortcuts={name: (base64.b64encode((self.menu_root / name).read_bytes()).decode('ascii')
                             if (self.menu_root / name).is_file() else None) for name in LINKS})
        return validate_snapshot(value, root)

    def health(self, root, release):
        value = self.snapshot(root)
        return value['values']['DisplayVersion']['value'] == release.version

    def restore(self, root, value):
        validate_snapshot(value, root)
        assert value['menu_root'] == str(self.menu_root)
        atomic_write(self.path, json_bytes(value['values']))
        self.menu_root.mkdir(exist_ok=True)
        for name, content in value['shortcuts'].items():
            atomic_write(self.menu_root / name, base64.b64decode(content, validate=True))


class FixtureSetupAdapter(WindowsManagedSetupAdapter):
    def __init__(self, root, registration, payload):
        super().__init__(maintenance, registration=registration)
        self.root, self.payload = root, payload
        self.failure = None
        self.started = 0
        self.bundle.health = self._fixture_health

    def _fixture_health(self, program, release):
        maintenance.verifier.verify(program)
        assert json.loads((program / 'bundle-manifest.json').read_bytes())['source_commit'] == release.source_commit
        assert (program / 'version.txt').read_text() == release.version
        return self.failure != 'health'

    def activate(self, root, stage, release, proof_file, proof_sha256, owner, cancelled):
        receipt = setup_receipt(stage, release)
        owner.check(root, True)
        assert not cancelled()
        self.started += 1
        # A separately opened app lease cannot start during the entire setup.
        with pytest.raises(RuntimeError):
            with installation_lease(root):
                pytest.fail('A running application entered an exclusive update.')

        def runner(program):
            if self.failure == 'bootstrap':
                (program / 'retained-incomplete.txt').write_text('Retain failed B bytes')
                raise RuntimeError('synthetic bootstrap failure')
            _bootstrap(program)

        if self.failure == 'after-old-move':
            proof = decode_proof(Path(proof_file).read_bytes(), proof_sha256)
            previous = Path(proof_file).parent
            maintenance.set_marker(root, 'updating', receipt['bundle_manifest_sha256'], receipt['setup_sha256'])
            (root / 'app').rename(previous / 'app')
            raise RuntimeError('synthetic loss immediately after retaining old app')

        maintenance.upgrade(self.payload, root, receipt['bundle_manifest_sha256'], receipt['setup_sha256'],
            proof_file=proof_file, proof_sha256=proof_sha256, owner=owner,
            setup_source=stage / 'ResonanceSetup.exe', runner=runner)
        for name in UNINSTALL_FILES:
            (root / name).write_bytes(('B wrapper is deliberately longer than the A bytes: ' + name).encode())
        self.registration.write(release.version)
        if self.failure == 'missing-wrapper':
            self.registration.path.unlink()
            for path in self.registration.menu_root.iterdir():
                path.unlink()
            self.registration.menu_root.rmdir()
            raise RuntimeError('synthetic interruption while the Inno wrapper is absent')
        if self.failure == 'wrapper':
            raise RuntimeError('synthetic interruption after wrapper changes')


@pytest.fixture
def managed(tmp_path, repository, clock):
    payload_a, payload_b = tmp_path / 'payload-A', tmp_path / 'payload-B'
    hash_a = _payload(payload_a, '3.1.0', '1' * 40)
    hash_b = _payload(payload_b, '3.2.0', '2' * 40)
    setup_a = tmp_path / 'setup-A.exe'
    setup_a.write_bytes(b'SYNTHETIC setup A; never execute')
    root = tmp_path / 'Installed Resonance Ω'
    maintenance.install(payload_a, root, hash_a, maintenance.sha(setup_a),
                        runner=_bootstrap, setup_source=setup_a)
    for name in UNINSTALL_FILES:
        (root / name).write_bytes(('A:' + name).encode())
    registration = FixtureRegistration(root, tmp_path / 'synthetic-registration')
    before_wrapper = registration.snapshot(root)
    before_uninstall = {name: (root / name).read_bytes() for name in UNINSTALL_FILES}
    old_owner, old_marker = (root / maintenance.OWNER).read_bytes(), (root / maintenance.MARKER).read_bytes()
    old_files = maintenance.inventory(root / 'app')
    prepared = tmp_path / 'prepared-B'
    receipt = builder.prepare(payload_b, prepared, manifest_sha256=hash_b)
    setup_b = b'SYNTHETIC setup B; selected through actual TUF, never executed'
    receipt.update(status='BUILT_NOT_INSTALLED', setup_sha256=sha256(setup_b).hexdigest(),
                   setup_bytes=len(setup_b), toolchain={'synthetic_fixture': True})
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('ResonanceSetup.exe', setup_b)
        archive.writestr('setup-build.json', json_bytes(receipt))
    repository.publish(buffer.getvalue(), custom={'version': '3.2.0'})
    policy = repository.policy()
    client = LocalRepositoryClient(tmp_path / 'update-cache', policy)
    review = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Preserve original research')
    book = app.add_text(workspace, 'Original source', 'Exact Ukrainian text. Незмінне джерело.')
    anchor = MemoryAnchor(Anchor(1, 'a' * 64, clock.now))
    adapter = FixtureSetupAdapter(root, registration, prepared / 'payload')
    tx = ManagedUpdateTransaction(app, root, tmp_path / 'update-work', policy,
                                  anchor, adapter, idle_lease=idle, clock=clock)
    return SimpleNamespace(root=root, app=app, workspace=workspace, book=book, tx=tx, adapter=adapter,
        package=package, anchor=anchor, registration=registration, hash_a=hash_a, hash_b=hash_b,
        before_wrapper=before_wrapper, before_uninstall=before_uninstall, old_owner=old_owner,
        old_marker=old_marker, old_files=old_files, prepared=prepared, receipt=receipt)


def _assert_original(data):
    assert maintenance.inventory(data.root / 'app') == data.old_files
    assert (data.root / maintenance.OWNER).read_bytes() == data.old_owner
    assert (data.root / maintenance.MARKER).read_bytes() == data.old_marker
    assert data.registration.snapshot(data.root) == data.before_wrapper
    assert {name: (data.root / name).read_bytes() for name in UNINSTALL_FILES} == data.before_uninstall
    assert data.anchor.load().sequence == 1
    assert data.app.search(data.workspace, 'Ukrainian')[0].quote == 'Ukrainian'


def test_signed_managed_a_to_b_preserves_full_wrapper_and_usable_repair_uninstall(managed, tmp_path):
    d = managed
    result = d.tx.apply(d.package)
    assert result['status'] == 'complete' and result['layout'] == 'standard-setup'
    assert d.anchor.load().sequence == 2 and maintenance.marker(d.root)['state'] == 'complete'
    assert (d.root / 'app/version.txt').read_text() == '3.2.0'
    previous = Path(result['previous'])
    assert maintenance.inventory(previous / 'app') == d.old_files
    assert (previous / maintenance.OWNER).read_bytes() == d.old_owner
    assert (previous / maintenance.MARKER).read_bytes() == d.old_marker
    assert {name: (previous / 'wrapper' / name).read_bytes() for name in UNINSTALL_FILES} == d.before_uninstall
    restored = LibraryApp(restore_backup(Path(result['backup']), tmp_path / 'restored', result['backup_sha256']))
    assert restored.search(d.workspace, 'Ukrainian')[0].quote == 'Ukrainian'
    assert d.app.search(d.workspace, 'Ukrainian')[0].quote == 'Ukrainian'
    marker = maintenance.marker(d.root)
    maintenance.check(d.root, marker['bundle_sha256'], marker['setup_sha256'])
    repaired = maintenance.install(d.prepared / 'payload', d.root, marker['bundle_sha256'],
                                   marker['setup_sha256'], runner=_bootstrap)
    assert repaired['operation'] == 'repair'
    maintenance.uninstall(d.root, marker['bundle_sha256'], marker['setup_sha256'])
    assert not (d.root / 'app').exists()
    assert d.app.search(d.workspace, 'Ukrainian')[0].quote == 'Ukrainian'


@pytest.mark.parametrize('failure', ['bootstrap', 'wrapper', 'missing-wrapper', 'health', 'anchor'])
def test_failed_managed_activation_restores_all_of_a_without_retargeting_data(managed, failure):
    d = managed
    if failure == 'anchor':
        d.anchor.fail = True
    else:
        d.adapter.failure = failure
    with pytest.raises((TrustError, RuntimeError)):
        d.tx.apply(d.package)
    _assert_original(d)
    record = json.loads(d.tx.journal.read_bytes())
    assert record['phase'] == 'rolled-back'
    _, previous, failed = d.tx._paths(record)
    assert (previous / PROOF_FILE).is_file()
    assert (failed / 'app').is_dir()
    assert (failed / 'maintenance/ResonanceSetup.exe').is_file()
    assert d.tx.recover() == 'rolled-back'


def test_running_application_prevents_managed_update_before_any_app_changes(managed):
    d = managed
    with installation_lease(d.root):
        with pytest.raises(RuntimeError, match='Close Resonance'):
            d.tx.apply(d.package)
    _assert_original(d)
    assert d.adapter.started == 0 and not d.tx.journal.exists()


def test_changed_program_or_uninstall_snapshot_is_not_silently_overwritten(managed):
    d = managed
    foreign = d.root / 'app/retained-notes.txt'
    foreign.write_bytes(b'Unowned user note')
    with pytest.raises(TrustError, match='Repair'):
        d.tx.apply(d.package)
    assert foreign.read_bytes() == b'Unowned user note'
    assert d.adapter.started == 0 and d.anchor.load().sequence == 1


def test_recovery_reopens_missing_app_gap_and_restores_original_wrapper(managed, monkeypatch):
    d = managed
    d.adapter.failure = 'after-old-move'
    rollback = d.tx._rollback
    monkeypatch.setattr(d.tx, '_rollback', lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic stopped recovery')))
    with pytest.raises(RuntimeError, match='stopped recovery'):
        d.tx.apply(d.package)
    assert not (d.root / 'app').exists()
    assert maintenance.marker(d.root)['state'] == 'updating'
    monkeypatch.setattr(d.tx, '_rollback', rollback)
    reopened = ManagedUpdateTransaction(d.app, d.root, d.tx.work, d.tx.policy, d.anchor, d.adapter,
                                        idle_lease=idle, clock=d.tx.clock)
    assert reopened.recover() == 'rolled-back'
    _assert_original(d)


@pytest.mark.parametrize('boundary', ['finish-marker', 'final-journal'])
def test_committed_anchor_never_rolls_back_after_final_marker_or_journal_failure(managed, monkeypatch, boundary):
    d = managed
    if boundary == 'finish-marker':
        original = maintenance.finish_upgrade
        def failed(*args, **kwargs):
            raise OSError('synthetic final marker failure')
        monkeypatch.setattr(maintenance, 'finish_upgrade', failed)
    else:
        original = d.tx._write
        def failed(record, phase):
            if phase == 'complete':
                raise OSError('synthetic final journal failure')
            return original(record, phase)
        monkeypatch.setattr(d.tx, '_write', failed)
    with pytest.raises(OSError, match='synthetic final'):
        d.tx.apply(d.package)
    assert d.anchor.load().sequence == 2 and (d.root / 'app/version.txt').read_text() == '3.2.0'
    if boundary == 'finish-marker':
        assert maintenance.marker(d.root)['state'] == 'updating'
        with pytest.raises(RuntimeError, match='pending maintenance'):
            with application_lease([], prefix=d.root / 'app/.venv'):
                pytest.fail('Pending B must not become an ordinary application launch.')
        monkeypatch.setattr(maintenance, 'finish_upgrade', original)
    else:
        monkeypatch.setattr(d.tx, '_write', original)
    assert d.tx.recover() == 'complete'
    assert maintenance.marker(d.root)['state'] == 'complete'
    assert d.anchor.load().sequence == 2


def test_same_process_lease_borrow_requires_live_pid_thread_root_and_exclusivity(managed, monkeypatch):
    d = managed
    service = maintenance.lease_module
    invented = service._HeldLease(d.root, True)
    invented.active = True
    with pytest.raises(ValueError, match='no longer held'):
        with service.installation_lease(d.root, exclusive=True, owner=invented):
            pytest.fail('A constructed token is not an actually held native lease.')
    with service.installation_lease(d.root, exclusive=True) as held:
        with service.installation_lease(d.root, exclusive=True, owner=held):
            pass
        with monkeypatch.context() as patch:
            original_pid = os.getpid()
            patch.setattr(service.os, 'getpid', lambda: original_pid + 1)
            with pytest.raises(ValueError, match='no longer held'):
                with service.installation_lease(d.root, exclusive=True, owner=held):
                    pass
    with pytest.raises(ValueError, match='no longer held'):
        with service.installation_lease(d.root, exclusive=True, owner=held):
            pass
    with service.installation_lease(d.root) as shared:
        with pytest.raises(ValueError, match='no longer held'):
            with service.installation_lease(d.root, exclusive=True, owner=shared):
                pass


def test_modified_authenticated_setup_is_refused_before_standard_installer(managed):
    d = managed
    d.package.path.write_bytes(b'A pathname is not TUF verification')
    with pytest.raises(TrustError, match='length changed'):
        d.tx.apply(d.package)
    _assert_original(d)
    assert d.adapter.started == 0


def test_shared_application_handle_cannot_be_relabelled_as_exclusive_maintenance(managed):
    service = maintenance.lease_module
    with service.installation_lease(managed.root) as shared:
        handle = shared.native_handle if os.name == 'nt' else 1
        with pytest.raises(ValueError, match='not exclusive|only valid on Windows'):
            with service.installation_lease(managed.root, exclusive=True,
                                           owner=(os.getpid(), handle)):
                pytest.fail('A shared app handle authorized a native setup mutation.')


def test_standard_update_accepts_exact_legacy_seven_helper_inventory(managed):
    d = managed
    path = d.root / 'maintenance/managed_installation.py'
    path.unlink()
    record = json.loads((d.root / maintenance.OWNER).read_bytes())
    del record['maintenance']['managed_installation.py']
    raw = json_bytes(record)
    (d.root / maintenance.OWNER).write_bytes(raw)
    old = maintenance.marker(d.root)
    maintenance.set_marker(d.root, 'complete', old['bundle_sha256'], old['setup_sha256'], sha256(raw).hexdigest())
    assert set(maintenance.owned(d.root, old['bundle_sha256'], old['setup_sha256'])['maintenance']) == set(maintenance.LEGACY_MAINTENANCE)
    result = d.tx.apply(d.package)
    assert result['status'] == 'complete'
    assert (Path(result['previous']) / 'maintenance/managed_installation.py').exists() is False
    assert (d.root / 'maintenance/managed_installation.py').is_file()


@pytest.mark.parametrize('change', [
    lambda p: p.update(root='relative/path'),
    lambda p: p.update(transaction='0' * 31),
    lambda p: p['new'].update(version='not an installer version'),
    lambda p: p['new'].update(source_commit='g' * 40),
    lambda p: p['old'].update(owner_sha256='A' * 64),
    lambda p: p.update(extra=True),
])
def test_upgrade_proof_rejects_retargeted_or_ambiguous_fields(managed, change):
    d = managed
    proof = dict(schema=PROOF_SCHEMA, transaction='0' * 32, root=str(d.root),
        old=dict(bundle_sha256=d.hash_a, setup_sha256='a' * 64, owner_sha256='b' * 64),
        new=dict(bundle_sha256=d.hash_b, setup_sha256='c' * 64, source_commit='2' * 40, version='3.2.0'))
    change(proof)
    with pytest.raises(ValueError):
        encode_proof(proof)


def _compiled_fixture(data):
    compiled = data.prepared / 'compiled'
    compiled.mkdir()
    with ZipFile(data.package.path) as archive:
        (compiled / 'ResonanceSetup.exe').write_bytes(archive.read('ResonanceSetup.exe'))
        (data.prepared / 'setup-build.json').write_bytes(archive.read('setup-build.json'))
    return data.prepared


def test_core_update_target_contains_exact_compiled_bytes_without_publisher_claim(managed, tmp_path, repository):
    prepared = _compiled_fixture(managed)
    output = tmp_path / 'unsigned target'
    report = target_builder.create_update_target(prepared, '2' * 40, output)
    raw = (output / report['target']).read_bytes()
    assert (len(raw), sha256(raw).hexdigest()) == (report['bytes'], report['sha256'])
    assert report['publisher_authority'] == 'NOT_PROVISIONED'
    assert report['native_update'] == 'NOT_RUN' and report['audio_transfer_payload'] is False
    with ZipFile(output / report['target']) as archive:
        assert set(archive.namelist()) == {'ResonanceSetup.exe', 'setup-build.json'}
        assert archive.read('ResonanceSetup.exe') == (prepared / 'compiled/ResonanceSetup.exe').read_bytes()
        assert archive.read('setup-build.json') == (prepared / 'setup-build.json').read_bytes()
    repository.advance()
    repository.publish(raw, custom={'version': '3.2.0'})
    client = LocalRepositoryClient(tmp_path / 'produced-target-client', repository.policy())
    selected = client.check(repository.root, repository.target_path)
    authenticated = client.download(repository.root, repository.target_path, reviewed_sha256=selected.sha256)
    assert managed.tx.apply(authenticated)['status'] == 'complete'


@pytest.mark.parametrize('mode', ['flag', 'audio', 'audio-runtime', 'uppercase-namespace'])
def test_update_target_reuses_audio_transfer_refusal_even_for_opaque_setup(managed, tmp_path, mode):
    prepared = _compiled_fixture(managed)
    manifest = json.loads((prepared / 'payload/bundle-manifest.json').read_bytes())
    if mode == 'flag':
        manifest['bundled_audio'] = True
    else:
        manifest['bundled_audio'] = False
        folder = 'Audio-Runtime' if mode == 'uppercase-namespace' else mode
        path = folder + '/synthetic-runtime.exe'
        (prepared / 'payload' / folder).mkdir()
        (prepared / 'payload' / path).write_bytes(b'SYNTHETIC unexecuted forbidden namespace')
        manifest['files'][path] = sha256((prepared / 'payload' / path).read_bytes()).hexdigest()
    raw = json_bytes(manifest)
    (prepared / 'payload/bundle-manifest.json').write_bytes(raw)
    receipt = json.loads((prepared / 'setup-build.json').read_bytes())
    receipt['bundle_manifest_sha256'] = sha256(raw).hexdigest()
    (prepared / 'setup-build.json').write_bytes(json_bytes(receipt))
    output = tmp_path / 'must not publish'
    with pytest.raises(ValueError, match='distribution clearance'):
        target_builder.create_update_target(prepared, '2' * 40, output)
    assert not output.exists()


@pytest.mark.parametrize('path', ['compiled/ResonanceSetup.exe', 'ResonanceSetup.iss',
                                  'helpers/maintenance.py', 'payload/README.txt'])
def test_update_target_refuses_changed_compiler_inputs_or_result(managed, tmp_path, path):
    prepared = _compiled_fixture(managed)
    with (prepared / path).open('ab') as stream:
        stream.write(b'changed after capture')
    output = tmp_path / 'must not publish'
    with pytest.raises(ValueError):
        target_builder.create_update_target(prepared, '2' * 40, output)
    assert not output.exists()


@pytest.mark.parametrize('boundary', ['cancel', 'mutate-target', 'destination-race'])
def test_update_target_last_publication_boundary_preserves_other_bytes(managed, tmp_path, monkeypatch, boundary):
    prepared = _compiled_fixture(managed)
    output = tmp_path / 'unsigned target'
    stopped = False
    checked = target_builder._checked_source
    count = 0

    def after_check(*args):
        nonlocal count, stopped
        result = checked(*args)
        count += 1
        if count == 2:
            if boundary == 'cancel':
                stopped = True
            elif boundary == 'mutate-target':
                stage, = tmp_path.glob('.carr-managed-target-*')
                (stage / target_builder.TARGET).write_bytes(b'changed target staging')
            else:
                output.mkdir()
                (output / 'preserve.txt').write_bytes(b'concurrent destination owner')
        return result

    monkeypatch.setattr(target_builder, '_checked_source', after_check)
    with pytest.raises((InterruptedError, ValueError, FileExistsError)):
        target_builder.create_update_target(prepared, '2' * 40, output, cancelled=lambda: stopped)
    if boundary == 'destination-race':
        assert (output / 'preserve.txt').read_bytes() == b'concurrent destination owner'
        assert {p.name for p in output.iterdir()} == {'preserve.txt'}
    else:
        assert not output.exists()
    assert not list(tmp_path.glob('.carr-managed-target-*'))
