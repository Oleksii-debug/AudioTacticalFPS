"""Real TUF, files, backup and owned child proofs; native setup remains separate.

The setup image below is explicitly inert JSON decoded by a test adapter.
No synthetic image is passed to CreateProcess as an installer or called an EXE.
"""
from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
from types import SimpleNamespace
import zipfile

import pytest
import carr_library

from carr_library.application import LibraryApp
from carr_library.installation_lease import application_lease, installation_lease
from carr_library.security._files import TrustError, atomic_write, json_bytes, json_value, locked, read_bytes
from carr_library.security.anchors import Anchor
from carr_library.security.standard_installation import (
    RecoveryReviewRequired, StandardUpdateTransaction, reserve_generation,
)
from carr_library.security.standard_state import (
    PENDING, PRIOR, PROTOCOL, REJECTED, check_generation_start, read_pending,
)
from carr_library.security.standard_windows import (
    WindowsStandardHost, _binary, _file_digest, run_owned,
)
from carr_library.security.updates import LocalRepositoryClient
from .conftest import MemoryAnchor, installer_verify

A, B, C = 'a' * 40, 'b' * 40, 'c' * 40
PACKAGING = Path(__file__).resolve().parents[2] / 'packaging'


def delivery():
    spec = importlib.util.spec_from_file_location('_carr_standard_update_delivery',
                                                 PACKAGING / 'create_delivery.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(source, label):
    files = {
        'install.lock': b'# synthetic file inventory only\n',
        'install_windows.py': (PACKAGING / 'install_windows.py').read_bytes(),
        'Install.cmd': b'@exit /b 0\n', 'ResearchLibrary.cmd': b'@exit /b 0\n',
        'CheckInstallation.cmd': b'@exit /b 0\n', 'README.txt': b'Synthetic source-bound update fixture.\n',
        'wheelhouse/synthetic.whl': b'INERT FIXTURE, NOT AN APPLICATION WHEEL',
        'runtime/python.exe': b'INERT FIXTURE, NEVER EXECUTE',
        'ResearchLibrary.exe': b'INERT FIXTURE, NEVER EXECUTE',
        'native-launcher-build.json': b'{}',
        'ocr/ocr-manifest.json': b'{}', 'ocr/tesseract.exe': b'INERT OCR',
        'ocr/tessdata/eng.traineddata': b'INERT MODEL',
        'ocr-corresponding-source.tar': b'INERT TEST SOURCE INVENTORY',
        'program.py': ('print(' + repr(label) + ')\n').encode(),
    }
    manifest = {'schema': 1, 'source_commit': source, 'bundled_python': True,
        'bundled_ocr': True, 'bundled_native_launcher': True,
        'files': {name: sha256(data).hexdigest() for name, data in files.items()}}
    return files, json_bytes(manifest)


def setup_image(source=B, label='B'):
    import base64
    files, manifest = payload(source, label)
    image = json_bytes({'schema': 'inert-test-setup/1', 'source': source,
        'files': {name: base64.b64encode(value).decode('ascii') for name, value in files.items()},
        'manifest': base64.b64encode(manifest).decode('ascii')})
    return image, manifest


def package_bytes(source=B, label='B', mutation=None):
    image, manifest = setup_image(source, label)
    name = 'dist/Resonance-Setup-' + source[:12] + '.exe'
    report = {'schema': 1, 'kind': 'standard-windows-installer', 'source_commit': source,
        'manifest_sha256': sha256(manifest).hexdigest(), 'payload_files': len(json.loads(manifest)['files']),
        'status': 'compiled', 'recipe_sha256': 'd' * 64, 'compiler_sha256': 'e' * 64,
        'native_install_repair_uninstall': 'NOT_RUN', 'authenticode_verified': False,
        'nvda_verified': False, 'automatic_network': False, 'update_or_entitlement_authority': False,
        'managed_update_protocol': PROTOCOL,
        'installer': {'path': name, 'sha256': sha256(image).hexdigest(), 'bytes': len(image)}}
    members = {name: image, 'bundle-manifest.json': manifest}
    if mutation is not None:
        mutation(report, members)
    members['installer-build.json'] = json_bytes(report)
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, raw in members.items():
            archive.writestr(path, raw)
    return result.getvalue()


class SyntheticStandardHost(WindowsStandardHost):
    """Only native registry/shortcut/installer actions are synthetic.

    The inherited snapshot validation and A/B-only restore guards, actual file
    restore, existing verifier, TUF client, backup and child ownership are real.
    """
    def __init__(self, root):
        self.root = root
        self.guarded = False
        self.launches = self.health_calls = 0
        self.fail_health = False
        self.on_health = None
        self.native = delivery()
        self.verify_bundle = installer_verify()
        self.timeout = 30
        def validate(raw, source):
            manifest = self.native._manifest(raw, source)
            self.native._require_transferable_audio_inventory(manifest)
            return manifest
        self.validate_manifest = validate

    @contextmanager
    def setup_guard(self):
        assert not self.guarded
        with locked(self.root, 'synthetic-setup.lock'):
            self.guarded = True
            try:
                yield
            finally:
                self.guarded = False

    def _registration(self):
        return json_value(read_bytes(self.root / 'synthetic-registration.json', 65536))

    def _write_registration(self, previous, observed, current):
        assert self.guarded
        assert self._registration() == current
        atomic_write(self.root / 'synthetic-registration.json', json_bytes(previous))

    def shortcut(self):
        return self.root / 'synthetic-start-menu.lnk'

    def _shortcut_target(self, shortcut, root):
        return Path(read_bytes(shortcut, 4096).decode('utf-8'))

    def install(self, image, *, generation=None):
        import base64
        value = json_value(image, maximum=1024 * 1024)
        assert value['schema'] == 'inert-test-setup/1'
        source = value['source']
        generation = generation or self.root / 'versions' / (source[:12] + '-1')
        generation.mkdir()
        for name, encoded in value['files'].items():
            path = generation.joinpath(*name.split('/'))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded))
        (generation / 'bundle-manifest.json').write_bytes(base64.b64decode(value['manifest']))
        (generation / 'maintenance-setup.exe').write_bytes(image)
        python = generation / '.venv' / 'Scripts' / 'python.exe'
        python.parent.mkdir(parents=True)
        python.write_bytes(b'INERT INTERPRETER; fixture uses the actual test Python for health')
        (generation / '.venv' / '.installation-complete').write_bytes(b'fixture complete\n')
        self.verify_bundle(generation)
        registration = {
            'InstallLocation': {'type': 1, 'value': str(self.root)},
            'UninstallString': {'type': 1, 'value': '"' + str(self.root / 'unins000.exe') + '"'},
            'ModifyPath': {'type': 1, 'value': '"' + str(generation / 'maintenance-setup.exe') + '"'},
            'DisplayIcon': {'type': 1, 'value': str(generation / 'ResearchLibrary.exe')},
            'DisplayVersion': {'type': 1, 'value': source[:12]},
        }
        atomic_write(self.root / 'synthetic-registration.json', json_bytes(registration))
        atomic_write(self.shortcut(), str(generation / 'ResearchLibrary.exe').encode())
        atomic_write(self.root / 'unins000.exe', ('inert uninstaller ' + source).encode())
        atomic_write(self.root / 'unins000.dat', ('owned generations including ' + source).encode())
        return generation

    def launch(self, root, staged, installer, transaction, *, cancelled):
        assert not self.guarded
        self.launches += 1
        with self.setup_guard():
            pending = read_pending(root)
            for index in range(1, 100000):
                generation = root / 'versions' / (pending['candidate_source'][:12] + '-' + str(index))
                if not os.path.lexists(generation):
                    break
            else:
                raise TrustError('Synthetic generation budget exhausted.')
            reserve_generation(root, transaction, generation, pending['candidate_source'],
                               pending['candidate_manifest'], pending['candidate_setup'])
            self.install(read_bytes(staged / installer, 1024 * 1024), generation=generation)
        return 0

    def health(self, root, snapshot, release, *, cancelled=lambda: False):
        assert self.guarded
        self.health_calls += 1
        generation = root.joinpath(*snapshot['generation'].split('/'))
        raw = read_bytes(generation / 'bundle-manifest.json', 1024 * 1024)
        assert sha256(raw).hexdigest() == snapshot['manifest_sha256']
        manifest = self.validate_manifest(raw, release.source_commit)
        self.verify_bundle(generation, manifest_bytes=raw)
        assert _file_digest(generation / 'program.py') == manifest['files']['program.py']
        code, output = run_owned([sys.executable, '-I', '-B', generation / 'program.py'],
            cwd=generation, environment=self.environment(), timeout=30, cancelled=cancelled)
        if self.on_health is not None:
            self.on_health()
        return not self.fail_health and code == 0 and output.strip() == b'B'


@pytest.fixture
def setup(repository, tmp_path, monkeypatch):
    root = tmp_path / 'standard-program'
    (root / 'versions').mkdir(parents=True)
    (root / 'install-layout-v1.txt').write_bytes(b'Resonance.StandardInstall/1\r\n')
    (root / 'installation.lease').write_bytes(b'')
    host = SyntheticStandardHost(root)
    generation = host.install(setup_image(A, 'A')[0])
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('User-owned library')
    app.add_text(workspace, 'Original', 'Preserve this exact research source.')
    original = (root / 'user-owned-file.txt')
    original.write_bytes(b'Unknown user file retained')
    policy, anchor = repository.policy(), MemoryAnchor()
    transaction = StandardUpdateTransaction(app, generation, tmp_path / 'update-work', policy, anchor, host)
    monkeypatch.setattr('carr_library.security.standard_installation._trusted_previous_host',
                        lambda selected_root, pending, previous: host)
    return SimpleNamespace(root=root, generation=generation, app=app, workspace=workspace,
                           host=host, policy=policy, anchor=anchor, transaction=transaction,
                           repository=repository, original=original)


def verified(value, *, source=B, label='B', mutation=None):
    raw = package_bytes(source, label, mutation)
    value.repository.publish(raw, custom={'source_commit': source, 'version': '0.2.0'})
    client = LocalRepositoryClient(value.transaction.work / 'tuf-cache', value.policy)
    return client.download(value.repository.root, value.repository.target_path,
                           reviewed_sha256=sha256(raw).hexdigest())


def reverified(value, original):
    client = LocalRepositoryClient(value.transaction.work / 'fresh-recovery-tuf', value.policy)
    result = client.download(value.repository.root, value.repository.target_path,
        reviewed_sha256=original.release.sha256)
    assert result is not original and result.release == original.release
    return result


def test_actual_tuf_and_backup_activate_distinct_b_without_moving_or_deleting_a(setup):
    prior = setup.host.snapshot(setup.root)
    package = verified(setup)
    result = setup.transaction.apply(package)
    assert result['status'] == 'complete' and result['source_commit'] == B
    assert setup.host.launches == 1 and setup.host.health_calls == 1
    assert setup.anchor.load().sequence == 2 and setup.anchor.load().digest == package.release.sha256
    assert setup.generation.is_dir() and Path(result['generation']).is_dir()
    assert prior['generation'] != setup.host.snapshot(setup.root)['generation']
    assert setup.original.read_bytes() == b'Unknown user file retained'
    assert setup.app.search(setup.workspace, 'exact research source')
    from carr_library.library_backup import restore_backup
    restored = setup.transaction.work / 'verified-backup-restore'
    restore_backup(Path(result['backup']), restored, expected_manifest_sha256=result['backup_sha256'])
    assert LibraryApp(restored).search(setup.workspace, 'exact research source')
    assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()


def test_failed_b_health_restores_exact_registry_shortcut_uninstaller_and_keeps_b_diagnostics(setup):
    prior = setup.host.snapshot(setup.root)
    setup.host.fail_health = True
    with pytest.raises(TrustError, match='health'):
        setup.transaction.apply(verified(setup))
    assert setup.host.snapshot(setup.root) == prior and setup.anchor.load() == Anchor()
    rejected = setup.root / 'versions' / (B[:12] + '-1')
    assert (rejected / REJECTED).is_file() and setup.generation.is_dir()
    assert setup.transaction.recover() == 'rolled-back'
    assert setup.original.read_bytes() == b'Unknown user file retained'
    assert not (setup.root / PENDING).exists()


@pytest.mark.parametrize('component', ['registry', 'shortcut', 'uninstaller'])
def test_concurrent_foreign_activation_component_is_never_overwritten(setup, component):
    def change():
        if component == 'registry':
            data = setup.host._registration()
            data['DisplayVersion'] = {'type': 1, 'value': 'NEWER-INSTALLATION'}
            atomic_write(setup.root / 'synthetic-registration.json', json_bytes(data))
        elif component == 'shortcut':
            atomic_write(setup.host.shortcut(), b'Foreign shortcut bytes')
        else:
            atomic_write(setup.root / 'unins000.dat', b'Newer uninstaller ownership')
    setup.host.on_health = change
    setup.host.fail_health = True
    with pytest.raises(TrustError):
        setup.transaction.apply(verified(setup))
    if component == 'registry':
        assert setup.host._registration()['DisplayVersion']['value'] == 'NEWER-INSTALLATION'
    elif component == 'shortcut':
        assert setup.host.shortcut().read_bytes() == b'Foreign shortcut bytes'
    else:
        assert (setup.root / 'unins000.dat').read_bytes() == b'Newer uninstaller ownership'
    assert (setup.root / PENDING).is_file()
    assert setup.anchor.load() == Anchor() and setup.generation.is_dir()


@pytest.mark.parametrize('phase', ['activated', 'health-checked'])
def test_interrupted_observed_uncommitted_b_recovers_exact_a(setup, monkeypatch, phase):
    class ProcessEnded(BaseException):
        pass
    write = setup.transaction._write
    def crash(record, state):
        write(record, state)
        if state == phase:
            raise ProcessEnded
    prior = setup.host.snapshot(setup.root)
    monkeypatch.setattr(setup.transaction, '_write', crash)
    package = verified(setup)
    with pytest.raises(ProcessEnded):
        setup.transaction.apply(package)
    assert (setup.root / PENDING).is_file()
    monkeypatch.setattr(setup.transaction, '_write', write)
    assert setup.transaction.recover(reverified(setup, package)) == 'rolled-back'
    assert setup.host.snapshot(setup.root) == prior and setup.anchor.load() == Anchor()


def test_committed_anchor_survives_final_journal_failure_and_recovery_finishes_b(setup, monkeypatch):
    write = setup.transaction._write
    def fail(record, phase):
        if phase == 'committed':
            raise OSError('Synthetic final journal failure')
        return write(record, phase)
    monkeypatch.setattr(setup.transaction, '_write', fail)
    package = verified(setup)
    with pytest.raises(OSError, match='journal'):
        setup.transaction.apply(package)
    assert setup.anchor.load().digest == package.release.sha256
    assert setup.host.snapshot(setup.root)['source_commit'] == B
    monkeypatch.setattr(setup.transaction, '_write', write)
    assert setup.transaction.recover() == 'complete'
    assert not (setup.root / PENDING).exists() and setup.generation.is_dir()


def test_newer_protected_state_refuses_recovery_without_clobbering_metadata(setup, monkeypatch):
    class ProcessEnded(BaseException):
        pass
    write = setup.transaction._write
    def crash(record, phase):
        write(record, phase)
        if phase == 'activated':
            raise ProcessEnded
    monkeypatch.setattr(setup.transaction, '_write', crash)
    with pytest.raises(ProcessEnded):
        setup.transaction.apply(verified(setup))
    before = setup.host.snapshot(setup.root)
    setup.anchor.value = Anchor(3, 'f' * 64, 1)
    monkeypatch.setattr(setup.transaction, '_write', write)
    with pytest.raises(TrustError, match='Protected'):
        setup.transaction.recover()
    assert setup.host.snapshot(setup.root) == before and (setup.root / PENDING).exists()


def test_same_source_repair_is_refused_before_stage_backup_or_launch(setup):
    with pytest.raises(TrustError, match='same-source'):
        setup.transaction.apply(verified(setup, source=A, label='A'))
    assert setup.host.launches == 0
    assert not tuple(setup.transaction.work.glob('standard-stage-*'))
    assert not tuple(setup.transaction.work.glob('standard-backup-*'))


@pytest.mark.parametrize('mutation', ['protocol', 'source', 'manifest', 'setup-hash', 'extra-file',
    'audio-claim', 'audio-payload'], ids=['unsupported-handoff', 'different-source', 'manifest-digest',
    'setup-digest', 'extra-package-file', 'unreviewed-audio-flag', 'unreviewed-audio-files'])
def test_bad_or_unreviewed_tuf_package_cannot_reach_backup_or_installer(setup, monkeypatch, mutation):
    def mutate(report, members):
        if mutation == 'protocol':
            report['managed_update_protocol'] = 'unsupported'
        elif mutation == 'source':
            report['source_commit'] = C
        elif mutation == 'manifest':
            report['manifest_sha256'] = 'f' * 64
        elif mutation == 'setup-hash':
            report['installer']['sha256'] = 'f' * 64
        elif mutation == 'extra-file':
            members['unrelated.txt'] = b'No authority'
        else:
            manifest = json.loads(members['bundle-manifest.json'])
            if mutation == 'audio-claim':
                manifest['bundled_audio_runtime'] = True
            else:
                manifest['files']['audio-runtime/ffmpeg.exe'] = 'a' * 64
            members['bundle-manifest.json'] = json_bytes(manifest)
            report['manifest_sha256'] = sha256(members['bundle-manifest.json']).hexdigest()
            report['payload_files'] = len(manifest['files'])
    prior = setup.host.snapshot(setup.root)
    monkeypatch.setattr('carr_library.library_backup.create_backup',
                        lambda *a, **k: pytest.fail('Rejected package started a research backup.'))
    with pytest.raises((TrustError, ValueError)):
        setup.transaction.apply(verified(setup, mutation=mutate))
    assert setup.host.launches == 0 and setup.host.snapshot(setup.root) == prior
    assert not (setup.root / PENDING).exists()


def test_pending_b_launch_is_blocked_while_exact_bootstrap_and_existing_a_work_remain_available(setup):
    def inspect():
        current = setup.root / 'versions' / (B[:12] + '-1') / '.venv'
        with pytest.raises(TrustError, match='pending'):
            with application_lease([], prefix=current):
                pytest.fail('Uncommitted B launched a normal application.')
        with application_lease(['--self-test'], prefix=current, allow_bootstrap_self_test=True):
            pass
        with application_lease([], prefix=setup.generation / '.venv'):
            pass
    setup.host.on_health = inspect
    assert setup.transaction.apply(verified(setup))['status'] == 'complete'


def test_already_running_generation_does_not_poll_or_parse_pending_state_again(setup):
    environment = setup.generation / '.venv'
    with application_lease([], prefix=environment):
        (setup.root / PENDING).write_bytes(b'Interrupted or malformed coordination')
        with application_lease([], prefix=environment):
            pass
    (setup.root / PENDING).unlink()


def test_rejected_generation_stays_unlaunchable_after_a_was_restored(setup):
    setup.host.fail_health = True
    with pytest.raises(TrustError):
        setup.transaction.apply(verified(setup))
    current = setup.root / 'versions' / (B[:12] + '-1') / '.venv'
    with pytest.raises(TrustError, match='did not pass'):
        with application_lease([], prefix=current):
            pytest.fail('A rejected retained generation launched.')
    with application_lease([], prefix=setup.generation / '.venv'):
        pass


def test_active_previous_process_survives_a_to_b_and_still_blocks_destructive_retirement(setup, tmp_path):
    stop = tmp_path / 'stop-previous'
    code = """import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from carr_library.installation_lease import application_lease
with application_lease([], prefix=pathlib.Path(sys.argv[2])):
    sys.stdout.buffer.write(b'READY\\n'); sys.stdout.buffer.flush()
    deadline = time.monotonic() + 90
    while not pathlib.Path(sys.argv[3]).exists() and time.monotonic() < deadline:
        time.sleep(.02)
"""
    from carr_library.audio.engines import _start_process, _stop
    process = _start_process([sys.executable, '-I', '-B', '-c', code,
        str(Path(carr_library.__file__).resolve().parent.parent), str(setup.generation / '.venv'), str(stop)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    reader = None
    try:
        ready, lines = threading.Event(), []
        def read_ready():
            try:
                lines.append(process.stdout.readline())
            finally:
                ready.set()
        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        assert ready.wait(20), 'The owned previous process did not become ready within its deadline.'
        assert lines == [b'READY\n']
        result = setup.transaction.apply(verified(setup))
        assert result['status'] == 'complete' and process.poll() is None
        with pytest.raises(RuntimeError, match='Close'):
            with installation_lease(setup.root, exclusive=True):
                pytest.fail('The active old process lost its destructive-retirement guard.')
        stop.write_bytes(b'stop')
        assert process.wait(timeout=20) == 0
        with installation_lease(setup.root, exclusive=True):
            pass
    finally:
        stop.write_bytes(b'stop')
        _stop(process)
        if reader is not None:
            reader.join(timeout=5)
            assert not reader.is_alive()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_foreign_tuf_root_with_same_product_cannot_enter_standard_transaction(setup, tmp_path, monkeypatch):
    from .conftest import Repository
    other = Repository(tmp_path / 'different-publisher')
    raw = package_bytes()
    other.publish(raw, custom={'source_commit': B, 'version': '0.2.0'})
    client = LocalRepositoryClient(tmp_path / 'foreign-cache', other.policy())
    package = client.download(other.root, other.target_path, reviewed_sha256=sha256(raw).hexdigest())
    monkeypatch.setattr('carr_library.library_backup.create_backup',
                        lambda *a, **k: pytest.fail('Foreign publisher reached research backup.'))
    with pytest.raises(TrustError, match='different trusted publisher'):
        setup.transaction.apply(package)
    assert setup.host.launches == 0
    assert not tuple(setup.transaction.work.glob('standard-stage-*'))
    assert not (setup.root / PENDING).exists()


@pytest.mark.parametrize('file', ['prior', 'pending', 'journal'])
def test_changed_recovery_receipt_fails_closed_without_activation_write(setup, monkeypatch, file):
    class ProcessEnded(BaseException):
        pass
    write = setup.transaction._write
    def crash(record, phase):
        write(record, phase)
        if phase == 'activated':
            raise ProcessEnded
    monkeypatch.setattr(setup.transaction, '_write', crash)
    with pytest.raises(ProcessEnded):
        setup.transaction.apply(verified(setup))
    monkeypatch.setattr(setup.transaction, '_write', write)
    prior = setup.host.snapshot(setup.root)
    target = {'prior': setup.root / PRIOR, 'pending': setup.root / PENDING,
              'journal': setup.transaction.journal}[file]
    target.write_bytes(b'changed recovery evidence')
    with pytest.raises((TrustError, ValueError)):
        setup.transaction.recover()
    assert setup.host.snapshot(setup.root) == prior
    assert setup.anchor.load() == Anchor() and setup.generation.is_dir()


def test_changed_previous_activation_is_refused_by_actual_installer_handoff(setup, monkeypatch):
    original = setup.host.launch
    def changed(root, staged, installer, transaction, *, cancelled):
        record = setup.host._registration()
        record['DisplayVersion'] = {'type': 1, 'value': 'NEWER-ACTIVATION'}
        atomic_write(root / 'synthetic-registration.json', json_bytes(record))
        return original(root, staged, installer, transaction, cancelled=cancelled)
    monkeypatch.setattr(setup.host, 'launch', changed)
    with pytest.raises(TrustError):
        setup.transaction.apply(verified(setup))
    assert setup.host._registration()['DisplayVersion']['value'] == 'NEWER-ACTIVATION'
    assert len(tuple((setup.root / 'versions').iterdir())) == 1
    assert setup.anchor.load() == Anchor() and (setup.root / PENDING).is_file()


def test_stop_before_native_launch_restores_prior_without_claiming_a_new_generation(setup, monkeypatch):
    from carr_library.security.updates import UpdateCancelled
    monkeypatch.setattr(setup.host, 'launch',
        lambda *a, **k: (_ for _ in ()).throw(UpdateCancelled('Stopped before native child.')))
    prior = setup.host.snapshot(setup.root)
    with pytest.raises(UpdateCancelled):
        setup.transaction.apply(verified(setup))
    assert setup.host.snapshot(setup.root) == prior
    assert setup.transaction.recover() == 'rolled-back'
    assert not (setup.root / PENDING).exists()


def test_pending_child_cleanup_retains_actual_lease_and_work_lock_until_quiescent(setup, monkeypatch):
    from carr_library.audio.cleanup import AudioCleanupPending
    class PendingOwner:
        def __init__(self):
            self.callbacks = []
        def add_quiescent_callback(self, callback):
            self.callbacks.append(callback)
        def finish(self):
            for callback in self.callbacks:
                callback()
    owner = PendingOwner()
    error = AudioCleanupPending('Synthetic pending owned child', cleanup_owner=owner)
    def pending(*args, **kwargs):
        raise error
    monkeypatch.setattr(setup.host, 'launch', pending)
    prior = setup.host.snapshot(setup.root)
    package = verified(setup)
    with pytest.raises(AudioCleanupPending) as caught:
        setup.transaction.apply(package)
    assert caught.value is error and len(owner.callbacks) == 1
    assert setup.host.snapshot(setup.root) == prior and (setup.root / PENDING).exists()
    with pytest.raises(RuntimeError, match='Close'):
        with installation_lease(setup.root, exclusive=True):
            pytest.fail('Pending child released the live installation lease.')
    with pytest.raises(TrustError):
        with locked(setup.transaction.work, '.standard-update.lock'):
            pytest.fail('Pending child released the actual recovery lock.')
    owner.finish()
    with installation_lease(setup.root, exclusive=True):
        pass
    assert setup.transaction.recover(reverified(setup, package)) == 'rolled-back'


def configured_controller(value, **options):
    from carr_library.delivery_contract import IntegrationContext
    from carr_library.security.controller import TrustController
    client = LocalRepositoryClient(value.transaction.work / 'controller-cache', value.policy)
    return TrustController(updates=client, target=value.repository.target_path,
        context=IntegrationContext('carr-test', 'build-A', 'stable'),
        standard_update=value.transaction, **options)


def test_one_controller_requires_review_and_same_policy_then_commits_actual_transaction(setup):
    controller = configured_controller(setup)
    package = verified(setup)
    with pytest.raises(TrustError, match='explicit activation review'):
        controller.install_review(package)
    assert setup.host.launches == 0
    assert controller.install_review(package, accepted=True)['status'] == 'complete'
    assert controller.recover_update() == 'complete'
    assert 'active work' in controller.status_text()


def test_controller_rejects_different_policy_even_for_same_product(setup):
    from carr_library.security.controller import TrustController
    from carr_library.delivery_contract import IntegrationContext
    client = LocalRepositoryClient(setup.transaction.work / 'other-controller', setup.repository.policy())
    with pytest.raises(TrustError, match='exact configured update policy'):
        TrustController(updates=client, target=setup.repository.target_path,
            context=IntegrationContext('carr-test', 'build-A', 'stable'),
            standard_update=setup.transaction)


def test_default_cli_activation_is_waiting_input_and_does_not_read_repository(tmp_path, capsys):
    from carr_library.security.__main__ import main
    absent = tmp_path / 'not-a-repository'
    assert main(['install-standard-update', '--repository', str(absent),
                 '--reviewed-sha256', 'b' * 64, '--accept-reviewed']) == 2
    assert 'WAITING_INPUT' in capsys.readouterr().err
    assert not absent.exists()


def test_explicit_cli_uses_same_controller_and_real_tuf_transaction(setup, capsys):
    from carr_library.security.__main__ import main
    package = verified(setup)
    controller = configured_controller(setup)
    assert main(['install-standard-update', '--repository', str(setup.repository.root),
                 '--reviewed-sha256', package.release.sha256, '--accept-reviewed'],
                controller=controller) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['status'] == 'complete' and value['source_commit'] == B
    assert setup.host.launches == 1 and setup.original.read_bytes() == b'Unknown user file retained'


def review_surface(choice):
    """Extract the real review callback; widgets only supply user choices."""
    import ast
    source = Path(importlib.util.find_spec('carr_library.ui.trust_status').origin).read_text(encoding='utf-8')
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('show_install_review', '_same_update_library')]
    assert len(functions) == 2
    names = []
    class Widget:
        def __init__(self, *args, **kwargs):
            pass
        def SetName(self, value):
            names.append(value)
        def SetLabel(self, value):
            pass
        def Add(self, *args, **kwargs):
            pass
        def SetSizer(self, value):
            pass
        def SetFocus(self):
            pass
        def CreateStdDialogButtonSizer(self, flags):
            return Widget()
        def FindWindow(self, identity):
            return Widget()
        def ShowModal(self):
            return choice
        def Destroy(self):
            pass
    wx = SimpleNamespace(Dialog=Widget, TextCtrl=Widget, BoxSizer=Widget,
        DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, TE_MULTILINE=4, TE_READONLY=8,
        VERTICAL=16, EXPAND=32, ALL=64, OK=128, CANCEL=256, ALIGN_RIGHT=512,
        ID_OK=1, ID_CANCEL=2)
    from carr_library.security.controller import release_text
    namespace = {'__package__': 'carr_library.ui', 'wx': wx, 'release_text': release_text}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'real_standard_update_review', 'exec'), namespace)
    return namespace['show_install_review'], names


@pytest.mark.parametrize('accept', [False, True])
def test_native_review_callback_requires_separate_activation_choice(setup, accept):
    controller, package = configured_controller(setup), verified(setup)
    show, names = review_surface(1 if accept else 2)
    calls, reports = [], []
    def run_job(label, operation, *, done, failed):
        calls.append(label)
        done(operation())
    frame = SimpleNamespace(library=setup.app, report=reports.append, run_job=run_job,
                            cancel_event=threading.Event())
    show(frame, controller, package)
    assert 'Reviewed standard activation details' in names
    assert 'Back up and activate this verified update' in names
    assert len(calls) == (1 if accept else 0)
    assert setup.host.launches == (1 if accept else 0)
    assert setup.anchor.load().sequence == (2 if accept else 0)
    if accept:
        assert any('Verified standard update activated' in item for item in reports)
    else:
        assert not tuple(setup.transaction.work.glob('standard-backup-*'))


def test_activation_review_refuses_a_different_current_library_before_prompt_or_job(setup, tmp_path):
    show, names = review_surface(1)
    frame = SimpleNamespace(library=LibraryApp(tmp_path / 'other-library'),
        run_job=lambda *a, **k: pytest.fail('A stale library review started activation.'))
    with pytest.raises(TrustError, match='configured library'):
        show(frame, configured_controller(setup), verified(setup))
    assert names == [] and setup.host.launches == 0


def test_download_only_controller_does_not_imply_standard_activation(setup):
    from carr_library.security.controller import TrustController
    from carr_library.delivery_contract import IntegrationContext
    client = LocalRepositoryClient(setup.transaction.work / 'download-only', setup.policy)
    controller = TrustController(updates=client, target=setup.repository.target_path,
        context=IntegrationContext('carr-test', 'build-A', 'stable'))
    show, names = review_surface(1)
    reports = []
    frame = SimpleNamespace(report=reports.append,
        run_job=lambda *a, **k: pytest.fail('No standard transaction was configured.'))
    show(frame, controller, verified(setup))
    assert len(reports) == 1 and 'WAITING_INPUT' in reports[0]
    assert names == [] and setup.host.launches == 0


@pytest.mark.parametrize('failure', ['deadline', 'diagnostic-budget'])
def test_actual_owned_helper_stops_bounded_failed_child(tmp_path, monkeypatch, failure):
    import carr_library.security.standard_windows as module
    start, children = module._start_process, []
    def observed(*args, **kwargs):
        child = start(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module, '_start_process', observed)
    code = ("import time; time.sleep(30)" if failure == 'deadline'
            else "import os; os.write(1,b'x'*(3*1024*1024))")
    before = time.monotonic()
    with pytest.raises(TrustError, match='stopped|budget'):
        run_owned([sys.executable, '-I', '-B', '-c', code], cwd=tmp_path,
                  environment=WindowsStandardHost.environment(), timeout=1)
    assert time.monotonic() - before < 15
    assert len(children) == 1 and children[0].poll() is not None


def test_backup_changed_during_native_health_cannot_commit_protected_activation(setup):
    def alter():
        backups = tuple(setup.transaction.work.glob('standard-backup-*'))
        assert len(backups) == 1
        (backups[0] / 'manifest.json').write_bytes(b'changed backup manifest')
    setup.host.on_health = alter
    prior = setup.host.snapshot(setup.root)
    with pytest.raises((TrustError, ValueError)):
        setup.transaction.apply(verified(setup))
    assert setup.anchor.load() == Anchor()
    assert setup.host.snapshot(setup.root) == prior
    assert not (setup.root / PENDING).exists()


def test_native_descendant_drain_keeps_diagnostic_budget_after_leader_exit(tmp_path, monkeypatch):
    import carr_library.security.standard_windows as module
    diagnostic = tmp_path / 'diagnostic'
    diagnostic.write_bytes(b'x' * (2 * 1024**2 + 1))
    def query(*args):
        pytest.fail('An over-budget descendant must not be waited indefinitely.')
    job = SimpleNamespace(handle=1, api=SimpleNamespace(
        kernel=SimpleNamespace(QueryInformationJobObject=query)))
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt', fstat=os.fstat))
    with diagnostic.open('rb') as stream:
        with pytest.raises(TrustError, match='diagnostic budget'):
            module._drain(SimpleNamespace(_carr_job=job), time.monotonic() + 30,
                          lambda: False, stream)


def test_native_activation_snapshot_stops_at_aggregate_component_budget(setup, monkeypatch):
    import carr_library.security.standard_windows as module
    for number in (101, 102, 103):
        (setup.root / ('unins' + str(number) + '.dat')).write_bytes(b'x' * 100000)
    read, seen = module.read_bytes, []
    def tracked(path, maximum):
        if Path(path).name in {'unins101.dat', 'unins102.dat', 'unins103.dat'}:
            seen.append(Path(path).name)
        return read(path, maximum)
    monkeypatch.setattr(module, 'MAX_STATE', 256 * 1024)
    monkeypatch.setattr(module, 'read_bytes', tracked)
    with pytest.raises(TrustError, match='aggregate byte budget'):
        setup.host.snapshot(setup.root)
    assert 1 <= len(seen) <= 2


def test_retry_after_verified_rollback_retains_failed_b_and_uses_a_fresh_generation(setup):
    package = verified(setup)
    setup.host.fail_health = True
    with pytest.raises(TrustError, match='health'):
        setup.transaction.apply(package)
    failed = setup.root / 'versions' / (B[:12] + '-1')
    before = (failed / REJECTED).read_bytes()
    setup.host.fail_health = False
    result = setup.transaction.apply(package)
    assert result['status'] == 'complete'
    assert Path(result['generation']).name == B[:12] + '-2'
    assert (failed / REJECTED).read_bytes() == before
    assert setup.generation.is_dir() and setup.host.launches == 2
    assert setup.anchor.load().sequence == 2


def interrupted_update(value, monkeypatch, phase='activated'):
    class ProcessEnded(BaseException):
        pass
    original = value.transaction._write
    def crash(record, current):
        original(record, current)
        if current == phase:
            raise ProcessEnded
    package = verified(value)
    with monkeypatch.context() as patch:
        patch.setattr(value.transaction, '_write', crash)
        with pytest.raises(ProcessEnded):
            value.transaction.apply(package)
    assert value.transaction.journal.is_file()
    return package


def recovery_state(value):
    paths = (value.transaction.journal, value.root / PENDING, value.root / PRIOR)
    return (value.host.snapshot(value.root), value.anchor.load(),
        tuple(path.read_bytes() if path.exists() else None for path in paths),
        tuple(sorted(path.relative_to(value.root).as_posix()
                     for path in (value.root / 'versions').rglob(REJECTED))),
        value.original.read_bytes(), value.host.health_calls)


@pytest.mark.parametrize('phase', ['installing', 'activated', 'health-checked'])
def test_precommit_crash_without_fresh_proof_keeps_every_recovery_component(setup, monkeypatch, phase):
    interrupted_update(setup, monkeypatch, phase)
    before = recovery_state(setup)
    with pytest.raises(RecoveryReviewRequired, match='Reverify'):
        setup.transaction.recover()
    assert recovery_state(setup) == before
    assert setup.app.search(setup.workspace, 'exact research source')


@pytest.mark.parametrize('authority', ['other-root', 'other-release'])
def test_precommit_recovery_refuses_fresh_but_different_authority_or_release(setup, monkeypatch, tmp_path, authority):
    from .conftest import Repository
    original = interrupted_update(setup, monkeypatch)
    if authority == 'other-root':
        other = Repository(tmp_path / 'foreign-recovery-root')
        raw = original.path.read_bytes()
        other.publish(raw, custom={'source_commit': B, 'version': original.release.version})
        client = LocalRepositoryClient(tmp_path / 'foreign-recovery-cache', other.policy())
        package = client.download(other.root, other.target_path, reviewed_sha256=original.release.sha256)
    else:
        setup.repository.advance()
        package = verified(setup, source=C, label='C')
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='different trusted publisher|differs from the pending recovery'):
        setup.transaction.recover(package)
    assert recovery_state(setup) == before


@pytest.mark.parametrize('mode', ['no-repository', 'explicit', 'configured', 'cancelled'])
def test_controller_recovery_reverifies_only_explicit_or_configured_repository(setup, monkeypatch, mode):
    from carr_library.security.updates import UpdateCancelled
    prior = setup.host.snapshot(setup.root)
    interrupted_update(setup, monkeypatch)
    options = {'update_repository': setup.repository.root} if mode == 'configured' else {}
    controller = configured_controller(setup, **options)
    download, calls = controller.updates.download, []
    def observed(repository, target, **kwargs):
        calls.append((repository, target))
        return download(repository, target, **kwargs)
    monkeypatch.setattr(controller.updates, 'download', observed)
    before = recovery_state(setup)
    if mode == 'no-repository':
        with pytest.raises(RecoveryReviewRequired):
            controller.recover_update()
        assert not calls and recovery_state(setup) == before
    elif mode == 'cancelled':
        with pytest.raises(UpdateCancelled):
            controller.recover_update(setup.repository.root, cancelled=lambda: True)
        assert not calls and recovery_state(setup) == before
    else:
        selected = setup.repository.root if mode == 'explicit' else None
        assert controller.recover_update(selected) == 'rolled-back'
        assert len(calls) == 1 and calls[0] == (setup.repository.root, setup.repository.target_path)
        assert setup.host.snapshot(setup.root) == prior
        assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()


def test_cli_recovery_requires_repository_reverification_before_rollback(setup, monkeypatch, capsys):
    from carr_library.security.__main__ import main
    prior = setup.host.snapshot(setup.root)
    interrupted_update(setup, monkeypatch)
    controller = configured_controller(setup)
    before = recovery_state(setup)
    assert main(['recover-standard-update'], controller=controller) == 2
    assert recovery_state(setup) == before
    capsys.readouterr()
    assert main(['recover-standard-update', '--repository', str(setup.repository.root)],
                controller=controller) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'rolled-back'}
    assert setup.host.snapshot(setup.root) == prior


@pytest.mark.parametrize('damage', ['missing', 'modified'])
def test_committed_recovery_requires_retained_target_bytes_before_health_or_clear(setup, monkeypatch, damage):
    interrupted_update(setup, monkeypatch, 'committed')
    record = json.loads(setup.transaction.journal.read_bytes())
    archive = setup.transaction.work / record['archive']
    if damage == 'missing':
        archive.unlink()
    else:
        archive.write_bytes(b'Untrusted replacement archive')
    before = recovery_state(setup)
    with pytest.raises((TrustError, OSError)):
        setup.transaction.recover()
    assert recovery_state(setup) == before


def relabel_journal_as_c(value, *, replace_archive=False):
    """Keep protected build/digest but coherently relabel every mutable field."""
    from carr_library.security.standard_installation import review_installer
    record = json.loads(value.transaction.journal.read_bytes())
    raw_c = package_bytes(C, 'B')
    stage = value.transaction.work / 'synthetic-untrusted-c'
    stage.mkdir()
    with zipfile.ZipFile(io.BytesIO(raw_c)) as archive:
        archive.extractall(stage)
    from dataclasses import replace
    release = replace(value.transaction.pending_release(), source_commit=C)
    candidate = review_installer(stage, release, value.host)
    value.host.install(setup_image(C, 'B')[0])
    observed = value.host.snapshot(value.root)
    record['release']['source_commit'] = C
    record['candidate'] = candidate
    record['observed'] = observed
    record['pending'].update(candidate_source=C,
        candidate_manifest=candidate['manifest_sha256'], candidate_setup=candidate['setup_sha256'],
        candidate_generation=observed['generation'])
    atomic_write(value.transaction.journal, json_bytes(record))
    from carr_library.security.standard_state import pending_bytes
    atomic_write(value.root / PENDING, pending_bytes(record['pending'], value.root))
    if replace_archive:
        (value.transaction.work / record['archive']).write_bytes(raw_c)
    # This is deliberately a valid old parser input, not a syntax-only refusal.
    checked, _, _ = value.transaction._read()
    assert checked['observed'] == observed and checked['candidate']['source_commit'] == C


@pytest.mark.parametrize('replace_archive', [False, True], ids=['rewritten-journal', 'rewritten-journal-and-archive'])
def test_committed_recovery_cannot_promote_coherent_c_metadata_under_b_anchor(setup, monkeypatch, replace_archive):
    package = interrupted_update(setup, monkeypatch, 'committed')
    relabel_journal_as_c(setup, replace_archive=replace_archive)
    assert setup.anchor.load().digest == package.release.sha256
    before = recovery_state(setup)
    with pytest.raises(TrustError):
        setup.transaction.recover()
    assert recovery_state(setup) == before
    assert setup.host.snapshot(setup.root)['source_commit'] == C


def test_precommit_recovery_does_not_relabel_newer_c_using_reverified_b(setup, monkeypatch):
    package = interrupted_update(setup, monkeypatch)
    relabel_journal_as_c(setup, replace_archive=True)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='differs from the pending recovery'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before


def test_streamed_setup_digest_observes_cancellation_after_first_chunk(tmp_path, monkeypatch):
    import carr_library.security.standard_windows as module
    from carr_library.security.updates import UpdateCancelled
    path = tmp_path / 'large-setup.exe'
    path.write_bytes(b'x' * (3 * 1024**2))
    original, reads = module.verified_reader, []
    @contextmanager
    def tracked(selected):
        with original(selected) as source:
            class Reader:
                def read(self, count):
                    raw = source.read(count)
                    reads.append(len(raw))
                    return raw
            yield Reader()
    monkeypatch.setattr(module, 'verified_reader', tracked)
    with pytest.raises(UpdateCancelled):
        module._file_digest(path, cancelled=lambda: bool(reads))
    assert reads == [1024**2]


@pytest.mark.parametrize('accept', [False, True])
def test_recovery_ui_reverifies_chosen_repository_without_restarting_install(setup, monkeypatch, accept):
    import ast
    interrupted_update(setup, monkeypatch)
    before = recovery_state(setup)
    source = Path(importlib.util.find_spec('carr_library.ui.trust_status').origin).read_text(encoding='utf-8')
    functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                 and node.name in ('recover_standard_update', '_same_update_library')]
    assert len(functions) == 2
    prompts, reports, calls = [], [], []
    class Chooser:
        def __init__(self, parent, title):
            prompts.append(title)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def ShowModal(self):
            return 1 if accept else 2
        def GetPath(self):
            return str(setup.repository.root)
    namespace = {'__package__': 'carr_library.ui', 'wx': SimpleNamespace(DirDialog=Chooser, ID_OK=1)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'real_standard_recovery_ui', 'exec'), namespace)
    def run_job(label, operation, *, done, failed):
        calls.append(label)
        try:
            result = operation()
        except Exception as error:
            failed(error)
        else:
            done(result)
    frame = SimpleNamespace(library=setup.app, report=reports.append, run_job=run_job,
                            cancel_event=threading.Event())
    namespace['recover_standard_update'](frame, configured_controller(setup))
    assert len(prompts) == 1 and len(calls) == (2 if accept else 1)
    assert setup.host.launches == 1
    if accept:
        assert setup.host.snapshot(setup.root)['source_commit'] == A
        assert any('rolled-back' in report for report in reports)
    else:
        assert recovery_state(setup) == before
        assert any('not started' in report for report in reports)


def test_recovery_refuses_coherently_rewritten_prior_uninstaller_even_with_fresh_signed_b(setup, monkeypatch):
    from carr_library.security.standard_state import pending_bytes
    package = interrupted_update(setup, monkeypatch)
    record = json.loads(setup.transaction.journal.read_bytes())
    foreign = b'UNAPPROVED UNINSTALLER BYTES FROM A MUTABLE JOURNAL'
    record['prior']['uninstallers']['unins000.exe'] = _binary(foreign)
    raw_prior = json_bytes(record['prior'])
    record['pending']['prior_sha256'] = sha256(raw_prior).hexdigest()
    atomic_write(setup.transaction.journal, json_bytes(record))
    atomic_write(setup.root / PRIOR, raw_prior)
    atomic_write(setup.root / PENDING, pending_bytes(record['pending'], setup.root))
    parsed, _, _ = setup.transaction._read()
    assert parsed['prior']['uninstallers']['unins000.exe'] == _binary(foreign)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='prior native activation'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before
    assert (setup.root / 'unins000.exe').read_bytes() != foreign


def test_new_process_cannot_authorize_prior_native_bytes_from_its_journal(setup, monkeypatch):
    package = interrupted_update(setup, monkeypatch)
    restarted = StandardUpdateTransaction(setup.app, setup.generation,
        setup.transaction.work, setup.policy, setup.anchor, setup.host)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='no retained live authority'):
        restarted.recover(reverified(setup, package))
    assert recovery_state(setup) == before
    assert setup.app.search(setup.workspace, 'exact research source')


def test_retained_target_failure_never_unlinks_foreign_stage_replacement(setup, monkeypatch):
    import carr_library.audio.jobs as jobs
    package = verified(setup)
    retained = []
    def replace_then_fail(source, destination, **options):
        owned = source.with_name(source.name + '-moved-owned')
        source.rename(owned)
        source.write_bytes(b'Foreign competing bytes must remain')
        retained.append(source)
        raise OSError('Synthetic publication interruption')
    monkeypatch.setattr(jobs, 'publish_noreplace', replace_then_fail)
    with pytest.raises(OSError, match='publication interruption'):
        setup.transaction._retain_target(package, setup.transaction.work / 'never-published.zip', lambda: False)
    assert len(retained) == 1 and retained[0].read_bytes() == b'Foreign competing bytes must remain'
    assert not (setup.transaction.work / 'never-published.zip').exists()


# The vault protocol is the existing provider test double, with an exact
# optional-read seam. This is not a plaintext production anchor or fallback.
def protected_setup(value):
    from carr_library.security.anchors import WindowsVaultAnchor
    path = Path(__file__).resolve().parents[1] / 'e2e' / 'test_provider_gateway.py'
    specification = importlib.util.spec_from_file_location('_existing_provider_vault_fixture', path)
    fixture = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixture)
    class RecoveryVault(fixture.MemoryVault):
        def get_optional(self, reference, binding):
            return self.values.get((reference, binding))
    vault = RecoveryVault()
    anchor = WindowsVaultAnchor('url-api-' + '1' * 32, '2' * 64,
        product=value.policy.product, profile_id='standard-recovery-test', vault=vault)
    vault.put(anchor.reference, anchor.vault_binding, anchor.encode(value.anchor.load()))
    # A separate provider credential at the same opaque reference must survive.
    vault.put(anchor.reference, 'f' * 64, 'unrelated-explicit-test-credential')
    value.anchor = anchor
    value.transaction = StandardUpdateTransaction(value.app, value.generation,
        value.transaction.work, value.policy, anchor, value.host)
    return vault


def restart_protected(value):
    from carr_library.security.anchors import WindowsVaultAnchor
    previous = value.anchor
    # New anchor/transaction/controller objects; no retained live snapshot.
    value.anchor = WindowsVaultAnchor(previous.reference, previous.binding,
        product=previous.product, profile_id=previous.profile_id, vault=previous.vault)
    value.transaction = StandardUpdateTransaction(value.app, value.generation,
        value.transaction.work, value.policy, value.anchor, value.host)
    assert value.transaction._live_prior is None
    return configured_controller(value)


def recovery_credential(value):
    record = json.loads(value.transaction.journal.read_bytes())
    authority = value.transaction._recovery_authority
    return record, (value.anchor.reference, authority.binding(record))


def test_current_native_vault_optional_read_distinguishes_only_actual_not_found(monkeypatch):
    import carr_library.providers as providers
    vault = providers.WindowsCredentialVault.__new__(providers.WindowsCredentialVault)
    vault.Credential = providers.ctypes.c_void_p
    vault.api = SimpleNamespace(CredReadW=lambda *args: False)
    monkeypatch.setattr(providers.ctypes, 'get_last_error', lambda: 1168, raising=False)
    assert vault.get_optional('url-api-' + '1' * 32, '2' * 64) is None
    with pytest.raises(providers.ProviderError, match='unavailable'):
        vault.get('url-api-' + '1' * 32, '2' * 64)


@pytest.mark.parametrize('native_error', [0, 5, 87, 1312])
def test_current_native_vault_optional_read_does_not_turn_native_errors_into_absence(monkeypatch, native_error):
    import carr_library.providers as providers
    vault = providers.WindowsCredentialVault.__new__(providers.WindowsCredentialVault)
    vault.Credential = providers.ctypes.c_void_p
    vault.api = SimpleNamespace(CredReadW=lambda *args: False)
    monkeypatch.setattr(providers.ctypes, 'get_last_error', lambda: native_error, raising=False)
    for read in (vault.get, vault.get_optional):
        with pytest.raises(providers.ProviderError, match='unavailable'):
            read('url-api-' + '1' * 32, '2' * 64)


@pytest.mark.parametrize('phase', ['installing', 'activated', 'health-checked'])
def test_protected_prior_survives_real_controller_restart_and_fresh_tuf_recovery(setup, monkeypatch, phase):
    vault = protected_setup(setup)
    # Native prior bytes exceed the credential limit and stay in bounded PRIOR.
    (setup.root / 'unins000.exe').write_bytes(b'Inert prior uninstaller bytes. ' * 256)
    prior = setup.host.snapshot(setup.root)
    interrupted_update(setup, monkeypatch, phase)
    record, key = recovery_credential(setup)
    assert len(json_bytes(record['prior'])) > 2400
    assert len(vault.values[key].encode('ascii')) <= 2400
    controller = restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(RecoveryReviewRequired):
        controller.recover_update()
    assert recovery_state(setup) == before
    assert controller.recover_update(setup.repository.root) == 'rolled-back'
    assert setup.host.snapshot(setup.root) == prior and setup.anchor.load() == Anchor()
    assert key not in vault.values
    assert vault.get(setup.anchor.reference, 'f' * 64) == 'unrelated-explicit-test-credential'
    assert setup.app.search(setup.workspace, 'exact research source')
    assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()


def test_protected_readback_precedes_any_native_pending_state_and_crash_is_recoverable(setup, monkeypatch):
    import carr_library.security.standard_installation as module
    vault = protected_setup(setup)
    class ProcessEnded(BaseException):
        pass
    def stop_before_pending(root, pending, prior):
        record, key = recovery_credential(setup)
        assert vault.values[key] == setup.transaction._recovery_authority.value(record)
        assert not (root / PENDING).exists() and not (root / PRIOR).exists()
        raise ProcessEnded
    with monkeypatch.context() as patch:
        patch.setattr(module, 'begin_pending', stop_before_pending)
        with pytest.raises(ProcessEnded):
            setup.transaction.apply(verified(setup))
    assert setup.host.launches == 0
    assert restart_protected(setup).recover_update(setup.repository.root) == 'rolled-back'
    assert setup.host.snapshot(setup.root)['source_commit'] == A


@pytest.mark.parametrize('phase', ['committed', 'complete'])
def test_protected_restart_finishes_committed_b_and_only_then_invalidates_its_authority(setup, monkeypatch, phase):
    vault = protected_setup(setup)
    package = interrupted_update(setup, monkeypatch, phase)
    record, key = recovery_credential(setup)
    assert key in vault.values and setup.anchor.load().digest == package.release.sha256
    assert restart_protected(setup).recover_update() == 'complete'
    assert setup.host.snapshot(setup.root)['source_commit'] == B
    assert key not in vault.values
    assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()


@pytest.mark.parametrize('failure', ['read', 'write', 'readback'])
def test_protected_credential_failure_never_starts_native_setup_or_loses_research(setup, monkeypatch, failure):
    vault = protected_setup(setup)
    prior, original_read, original_put = setup.host.snapshot(setup.root), vault.get_optional, vault.put
    written = []
    def read(reference, binding):
        if binding != setup.anchor.vault_binding:
            if failure == 'read':
                raise OSError('Synthetic native credential failure')
            if failure == 'readback' and written:
                return 'corrupt-protected-readback'
        return original_read(reference, binding)
    def put(reference, binding, value):
        if binding != setup.anchor.vault_binding:
            if failure == 'write':
                raise OSError('Synthetic native credential failure')
            written.append(binding)
        return original_put(reference, binding, value)
    monkeypatch.setattr(vault, 'get_optional', read)
    monkeypatch.setattr(vault, 'put', put)
    with pytest.raises(TrustError):
        setup.transaction.apply(verified(setup))
    assert setup.host.launches == 0 and setup.host.snapshot(setup.root) == prior
    assert setup.app.search(setup.workspace, 'exact research source')
    assert setup.anchor.load() == Anchor()
    assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()


@pytest.mark.parametrize('damage', ['missing', 'changed', 'foreign-transaction'])
def test_protected_restart_rejects_missing_corrupt_or_mismatched_credential_without_native_writes(setup, monkeypatch, damage):
    vault = protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    record, key = recovery_credential(setup)
    if damage == 'missing':
        del vault.values[key]
    elif damage == 'changed':
        vault.values[key] = 'corrupted'
    else:
        parsed = json.loads(vault.values[key])
        parsed['transaction'] = '0' * 32
        vault.values[key] = json_bytes(parsed).decode('ascii')
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='protected restart authority'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before


def test_protected_restart_rejects_coherently_rewritten_prior_even_after_fresh_same_tuf(setup, monkeypatch):
    from carr_library.security.standard_state import pending_bytes
    protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    record = json.loads(setup.transaction.journal.read_bytes())
    foreign = b'UNAPPROVED mutable native uninstaller'
    record['prior']['uninstallers']['unins000.exe'] = _binary(foreign)
    raw = json_bytes(record['prior'])
    record['pending']['prior_sha256'] = sha256(raw).hexdigest()
    atomic_write(setup.transaction.journal, json_bytes(record))
    atomic_write(setup.root / PRIOR, raw)
    atomic_write(setup.root / PENDING, pending_bytes(record['pending'], setup.root))
    restart_protected(setup)
    assert setup.transaction._read()[0]['prior'] == record['prior']
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='prior native activation'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before
    assert (setup.root / 'unins000.exe').read_bytes() != foreign


def test_protected_prior_credential_cannot_be_replayed_for_a_new_transaction(setup, monkeypatch):
    vault = protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    record, key = recovery_credential(setup)
    old = vault.values[key]
    assert restart_protected(setup).recover_update(setup.repository.root) == 'rolled-back'
    assert key not in vault.values
    # Same signed B and unchanged A do not authorize replaying the old UUID.
    setup.repository.advance()
    package = interrupted_update(setup, monkeypatch)
    newer, new_key = recovery_credential(setup)
    assert new_key != key and newer['transaction'] != record['transaction']
    vault.values[new_key] = old
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='protected restart authority'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before


def test_protected_restart_revalidates_the_exact_research_backup_before_rollback(setup, monkeypatch):
    protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    record, _ = recovery_credential(setup)
    backup = setup.transaction.work / record['backup']
    # The normal verified manifest is real; mutate an actual listed payload.
    files = [path for path in backup.rglob('*') if path.is_file() and path.name != 'manifest.json']
    assert files
    files[0].write_bytes(b'Corrupted retained research backup')
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises((TrustError, ValueError, OSError)):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before
    assert setup.app.search(setup.workspace, 'exact research source')


def test_protected_restart_never_replaces_an_unrelated_newer_c_activation(setup, monkeypatch):
    protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    setup.host.install(setup_image(C, 'C')[0])
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='newer native activation'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before
    assert setup.host.snapshot(setup.root)['source_commit'] == C


def test_protected_recovery_never_overwrites_a_conflicting_credential(setup, monkeypatch):
    vault = protected_setup(setup)
    original = setup.transaction._write
    conflicting = []
    def introduce_conflict(record, phase):
        original(record, phase)
        if phase == 'prepared':
            key = (setup.anchor.reference, setup.transaction._recovery_authority.binding(record))
            vault.values[key] = 'retain-this-unrelated-existing-value'
            conflicting.append(key)
    monkeypatch.setattr(setup.transaction, '_write', introduce_conflict)
    with pytest.raises(TrustError):
        setup.transaction.apply(verified(setup))
    assert len(conflicting) == 1 and vault.values[conflicting[0]] == 'retain-this-unrelated-existing-value'
    assert setup.host.launches == 0 and setup.host.snapshot(setup.root)['source_commit'] == A


def test_protected_terminal_cleanup_failure_retains_exact_credential_and_retries_without_reinstall(setup, monkeypatch):
    vault = protected_setup(setup)
    original = vault.delete
    def failed(reference, binding):
        assert json.loads(setup.transaction.journal.read_bytes())['phase'] == 'complete'
        assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()
        raise OSError('Synthetic secure deletion failure')
    with monkeypatch.context() as patch:
        patch.setattr(vault, 'delete', failed)
        with pytest.raises(TrustError, match='clear its own protected'):
            setup.transaction.apply(verified(setup))
    record, key = recovery_credential(setup)
    assert key in vault.values and setup.host.launches == 1
    assert restart_protected(setup).recover_update() == 'complete'
    assert key not in vault.values and setup.host.launches == 1
    assert vault.get(setup.anchor.reference, 'f' * 64) == 'unrelated-explicit-test-credential'


def test_settled_journal_replay_without_protected_authority_cannot_restore_native_bytes(setup, monkeypatch):
    vault = protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    record, key = recovery_credential(setup)
    retained = {path: path.read_bytes() for path in
                (setup.transaction.journal, setup.root / PENDING, setup.root / PRIOR)}
    assert restart_protected(setup).recover_update(setup.repository.root) == 'rolled-back'
    assert key not in vault.values
    for path, raw in retained.items():
        atomic_write(path, raw)
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(TrustError, match='protected restart authority'):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before


def test_review_protected_restart_refuses_relabelled_newer_same_release_generation(setup, monkeypatch):
    from carr_library.security.standard_state import pending_bytes
    protected_setup(setup)
    package = interrupted_update(setup, monkeypatch)
    record = json.loads(setup.transaction.journal.read_bytes())
    first_generation = record['observed']['generation']
    later = setup.root / 'versions' / (B[:12] + '-900')
    setup.host.install(setup_image(B, 'B')[0], generation=later)
    observed_later = setup.host.snapshot(setup.root)
    assert observed_later['generation'] != first_generation
    # Ordinary journal + pending can both change; the protected credential is untouched.
    record['observed'] = observed_later
    record['pending']['candidate_generation'] = observed_later['generation']
    atomic_write(setup.transaction.journal, json_bytes(record))
    atomic_write(setup.root / PENDING, pending_bytes(record['pending'], setup.root))
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(TrustError):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before


@pytest.mark.parametrize('boundary', ['before-protected-observation', 'after-protected-observation'])
def test_uncheckpointed_native_activation_refuses_restart_reclassification_and_preserves_data(setup, monkeypatch, boundary):
    protected_setup(setup)
    observe = setup.transaction._recovery_authority.observe
    class ProcessEnded(BaseException):
        pass
    def crash(record, before, **options):
        assert setup.host.snapshot(setup.root) == record['observed']
        assert record['observed']['source_commit'] == B
        if boundary == 'after-protected-observation':
            observe(record, before, **options)
        raise ProcessEnded
    with monkeypatch.context() as patch:
        patch.setattr(setup.transaction._recovery_authority, 'observe', crash)
        package = verified(setup)
        with pytest.raises(ProcessEnded):
            setup.transaction.apply(package)
    record = json.loads(setup.transaction.journal.read_bytes())
    assert record['phase'] == 'installing' and record['observed'] is None
    assert setup.host.snapshot(setup.root)['source_commit'] == B
    restart_protected(setup)
    before = recovery_state(setup)
    with pytest.raises(TrustError):
        setup.transaction.recover(reverified(setup, package))
    assert recovery_state(setup) == before
    assert setup.generation.is_dir() and setup.app.search(setup.workspace, 'exact research source')
    current = setup.root.joinpath(*setup.host.snapshot(setup.root)['generation'].split('/'))
    with pytest.raises(TrustError, match='pending update'):
        check_generation_start(setup.root, current / '.venv', [])


def test_review_reserved_b_failure_before_activation_clears_only_its_protected_authority(setup, monkeypatch):
    vault = protected_setup(setup)
    prior = setup.host.snapshot(setup.root)
    def fail_after_reserve(root, staged, installer, transaction, *, cancelled):
        with setup.host.setup_guard():
            pending = read_pending(root)
            generation = root / 'versions' / (B[:12] + '-700')
            reserve_generation(root, transaction, generation, pending['candidate_source'],
                               pending['candidate_manifest'], pending['candidate_setup'])
            generation.mkdir()
        raise TrustError('synthetic early native setup failure')
    monkeypatch.setattr(setup.host, 'launch', fail_after_reserve)
    with pytest.raises(TrustError, match='synthetic early native setup failure'):
        setup.transaction.apply(verified(setup))
    assert setup.host.snapshot(setup.root) == prior
    assert setup.anchor.load() == Anchor()
    record, key = recovery_credential(setup)
    assert record['phase'] == 'rolled-back'
    assert key not in vault.values
    assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()


@pytest.mark.parametrize('failure', ['write-failure', 'cancel-before-write', 'cancel-after-write'])
def test_observation_failure_clears_only_owned_initial_or_observed_authority_and_allows_fresh_retry(setup, monkeypatch, failure):
    from carr_library.security.updates import UpdateCancelled
    vault = protected_setup(setup)
    prior = setup.host.snapshot(setup.root)
    observe = setup.transaction._recovery_authority.observe
    put = vault.put
    entered, stopped = [], threading.Event()
    def observed(record, before, **options):
        entered.append(record['pending']['candidate_generation'])
        if failure == 'cancel-before-write':
            stopped.set()
        return observe(record, before, **options)
    def writing(reference, binding, value):
        if entered and binding != setup.anchor.vault_binding:
            if failure == 'write-failure':
                raise OSError('Synthetic observed-credential write failure')
            put(reference, binding, value)
            if failure == 'cancel-after-write':
                stopped.set()
            return
        return put(reference, binding, value)
    with monkeypatch.context() as patch:
        patch.setattr(setup.transaction._recovery_authority, 'observe', observed)
        patch.setattr(vault, 'put', writing)
        error = UpdateCancelled if failure.startswith('cancel-') else TrustError
        with pytest.raises(error):
            setup.transaction.apply(verified(setup), cancelled=stopped.is_set)
    assert len(entered) == 1
    assert setup.host.snapshot(setup.root) == prior and setup.anchor.load() == Anchor()
    record, key = recovery_credential(setup)
    assert record['phase'] == 'rolled-back' and key not in vault.values
    assert not (setup.root / PENDING).exists() and not (setup.root / PRIOR).exists()
    assert vault.get(setup.anchor.reference, 'f' * 64) == 'unrelated-explicit-test-credential'
    assert setup.app.search(setup.workspace, 'exact research source')
    stopped.clear()
    controller = restart_protected(setup)
    assert controller.recover_update() == 'rolled-back'
    setup.repository.advance()
    assert controller.install_review(verified(setup), accepted=True)['status'] == 'complete'
    assert setup.host.launches == 2 and setup.anchor.load().sequence == 2
