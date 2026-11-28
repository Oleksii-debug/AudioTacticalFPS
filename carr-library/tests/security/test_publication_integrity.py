"""Actual signed-package and protected-publication boundary regressions."""
from hashlib import sha256
import io
import json
import threading
from types import SimpleNamespace
import zipfile

import pytest

from carr_library.security import installation, refresh
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.anchors import Anchor, WindowsVaultAnchor
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import RefreshWorker
from carr_library.security.updates import LocalRepositoryClient, Release
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, bundle, envelope, grant, installer_verify
from .test_canonical_boundary import CanonicalVault


def downloaded(repository, tmp_path):
    repository.publish(bundle('B'))
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    review = client.check(repository.root, repository.target_path)
    return client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)


def test_in_place_rewrite_after_snapshot_cannot_replace_extracted_bytes(repository, tmp_path, monkeypatch):
    package = downloaded(repository, tmp_path)
    original_zip = zipfile.ZipFile
    replacement = bundle('C')
    rewritten = []

    def rewrite_before_extraction(stream, *args, **kwargs):
        package.path.write_bytes(replacement)
        rewritten.append(True)
        return original_zip(stream, *args, **kwargs)

    monkeypatch.setattr(installation.zipfile, 'ZipFile', rewrite_before_extraction)
    stage = installation.unpack(package, tmp_path / 'stage', repository.policy())
    assert rewritten == [True]
    assert package.path.read_bytes() == replacement
    assert (stage / 'version.txt').read_bytes() == b'B'


def test_consuming_a_verified_package_still_applies_current_package_budget(repository, tmp_path):
    package = downloaded(repository, tmp_path)
    original = package.path.read_bytes()
    with pytest.raises(TrustError, match='download budget'):
        installation.unpack(package, tmp_path / 'stage',
                            repository.policy(max_package_bytes=len(original) - 1))
    assert not (tmp_path / 'stage').exists()
    assert package.path.read_bytes() == original


def release_fixture(tmp_path, *, receipt=False):
    root = tmp_path / 'program'
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        archive.extractall(root)
    manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
    release = Release('carr-test', 'stable', '0.2.0', 2, '2' * 40, 4,
                      'release.zip', 1, 'a' * 64, '')
    manifest['source_commit'] = release.source_commit
    if receipt:
        wheel = 'wheelhouse/universal_research_library-0.2.0-py3-none-any.whl'
        (root / 'wheelhouse/synthetic.whl').rename(root / wheel)
        digest = manifest['files'].pop('wheelhouse/synthetic.whl')
        manifest['files'][wheel] = digest
        proof = {'source_commit': release.source_commit, 'source_tree': '3' * 40,
                 'project_name': 'universal-research-library', 'project_version': release.version,
                 'wheel_sha256': digest, 'payload_files': 1, 'required_runtime_files': 1}
        save_proof(root, manifest, proof)
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    return root, release, manifest


def save_proof(root, manifest, proof):
    raw = json_bytes(proof)
    (root / 'application-source.json').write_bytes(raw)
    manifest['files']['application-source.json'] = sha256(raw).hexdigest()


@pytest.mark.parametrize('receipt', [False, True])
def test_real_bundle_verifier_and_signed_release_share_exact_source_identity(tmp_path, receipt):
    root, release, _ = release_fixture(tmp_path, receipt=receipt)
    installation.WindowsBundleAdapter(installer_verify())._verify_release(root, release)


@pytest.mark.parametrize(('field', 'value'), [
    ('source_commit', '9' * 40), ('project_version', '0.1.0'),
    ('project_name', 'other-product'), ('wheel_sha256', 'f' * 64),
])
def test_inventoried_source_receipt_cannot_disagree_with_signed_release(tmp_path, field, value):
    root, release, manifest = release_fixture(tmp_path, receipt=True)
    proof = json.loads((root / 'application-source.json').read_bytes())
    proof[field] = value
    save_proof(root, manifest, proof)
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    # Inventory itself is internally consistent: the cross-binding must reject.
    installer_verify()(root)
    with pytest.raises(TrustError, match='source receipt differs'):
        installation.WindowsBundleAdapter(installer_verify())._verify_release(root, release)


def test_health_refuses_another_source_commit_before_starting_installed_python(tmp_path, monkeypatch):
    root, release, manifest = release_fixture(tmp_path)
    manifest['source_commit'] = '9' * 40
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    python = root / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'Synthetic sentinel: never executed.')
    calls = []
    monkeypatch.setattr(installation.WindowsBundleAdapter, '_run_owned', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(TrustError, match='source commit differs'):
        installation.WindowsBundleAdapter(installer_verify()).health(root, release)
    assert calls == []


def test_source_receipt_cannot_be_added_outside_verified_inventory(tmp_path):
    root, release, manifest = release_fixture(tmp_path, receipt=True)
    del manifest['files']['application-source.json']
    (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    with pytest.raises(TrustError, match='outside the verified inventory'):
        installation.WindowsBundleAdapter(installer_verify())._verify_release(root, release)


@pytest.mark.parametrize('stop', ['close', 'deadline'])
def test_stop_during_final_canonical_vault_compare_prevents_protected_put(
        tmp_path, signer, policy, clock, stop, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    deadline_reached, expired_observed = threading.Event(), threading.Event()

    def monotonic():
        if deadline_reached.is_set():
            expired_observed.set()
            return .05
        return 0.0

    if stop == 'deadline':
        # Advance only this worker's deadline at the actual final vault read;
        # native waits and the rest of the process retain their real clocks.
        monkeypatch.setattr(refresh, 'time', SimpleNamespace(monotonic=monotonic))

    class BarrierVault(CanonicalVault):
        refresh_reads = 0

        def get(self, reference, binding):
            if threading.current_thread().name == 'carr-entitlement-refresh':
                self.refresh_reads += 1
                if self.refresh_reads == 2:
                    entered.set()
                    assert release.wait(2)
            return super().get(reference, binding)

    vault = BarrierVault()
    anchor = WindowsVaultAnchor(REFERENCE, '1' * 64, product='carr-test',
                               profile_id='trust-test', vault=vault)
    vault.put(anchor.vault_reference, anchor.vault_binding, anchor.encode(Anchor()))
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)
    puts = sum(call[0] == 'put' for call in vault.calls)

    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))

    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test',
                           clock=clock, timeout_seconds=.05 if stop == 'deadline' else 20)
    try:
        assert worker.tick(manual=True)
        assert entered.wait(1)
        if stop == 'close':
            worker.close(0)
        else:
            deadline_reached.set()
    finally:
        release.set()
        try:
            if worker._thread is not None:
                worker._thread.join(2)
            if stop == 'deadline' and deadline_reached.is_set():
                assert expired_observed.is_set()
                assert worker._thread is not None and not worker._thread.is_alive()
                assert not worker._stop.is_set()
                assert worker._cancel is not None and not worker._cancel.is_set()
        finally:
            worker.close()
    assert not worker.running
    assert anchor.load().sequence == 1
    assert store.current_path.read_bytes() == original
    assert sum(call[0] == 'put' for call in vault.calls) == puts


def test_close_after_protected_advance_finishes_committed_grant_publication(tmp_path, signer, policy, clock):
    advanced, release = threading.Event(), threading.Event()

    class BarrierAnchor(MemoryAnchor):
        def compare_and_set(self, expected, replacement, *, guard=lambda: None):
            super().compare_and_set(expected, replacement, guard=guard)
            if replacement.sequence == 2:
                advanced.set()
                assert release.wait(2)

    anchor = BarrierAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    accept(store, envelope(signer, grant(clock)))

    class Provider(RenewalProvider):
        raw = None

        def renew(self, request, cancelled):
            self.raw = envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))
            return self.raw

    provider = Provider()
    worker = RefreshWorker(store, provider, REFERENCE, profile_id='trust-test', clock=clock)
    try:
        assert worker.tick(manual=True)
        assert advanced.wait(1)
        worker.close(0)
    finally:
        release.set()
        if worker._thread is not None:
            worker._thread.join(2)
        worker.close()
    assert not worker.running
    assert anchor.load().sequence == 2
    assert store.current_path.read_bytes() == provider.raw
    assert not store.pending_path.exists()
    assert store.current().sequence == 2


def test_program_only_updater_refuses_managed_installer_ownership_before_verifying_or_launching(tmp_path):
    root = tmp_path / 'managed'
    program = root / 'app'
    program.mkdir(parents=True)
    marker = root / 'managed-install.ini'
    marker.write_bytes(b'Existing standard installation identity')
    calls = []
    adapter = installation.WindowsBundleAdapter(lambda *a, **k: calls.append(a))
    with pytest.raises(TrustError, match='ownership receipts'):
        adapter.verify(program)
    assert calls == [] and marker.read_bytes() == b'Existing standard installation identity'


def test_update_native_runner_uses_the_shared_owned_tree_and_fixed_diagnostic_budget(tmp_path, monkeypatch):
    from carr_library.audio import capture_process
    calls = []
    def run(arguments, **options):
        calls.append((arguments, options))
        return capture_process.ProcessResult(b'', b'', .01, 0)
    monkeypatch.setattr(capture_process, 'run_process', run)
    monkeypatch.setenv('PYTHONPATH', 'untrusted')
    monkeypatch.setenv('PIP_INDEX_URL', 'https://untrusted.invalid')
    adapter = installation.WindowsBundleAdapter(lambda root: None)
    assert adapter._run_owned([tmp_path / 'python.exe', '-I', '--self-test'], root=tmp_path, timeout=9) == 0
    arguments, options = calls[0]
    assert arguments[1:] == ['-I', '--self-test']
    assert options['owned_tree'] is True and options['allow_failure'] is True
    assert options['budget'].timeout == 9 and options['budget'].max_log_bytes == 4 * 1024**2
    assert 'PYTHONPATH' not in options['env'] and 'PIP_INDEX_URL' not in options['env']
    assert options['env']['PIP_NO_INDEX'] == '1'


def test_update_cannot_return_to_rollback_while_native_cleanup_is_unconfirmed(tmp_path, monkeypatch):
    from carr_library.audio import capture_process
    from carr_library.audio.capture_types import AudioCleanupPending
    entered, quiescent = threading.Event(), threading.Event()
    class Owner:
        quiescent_event = quiescent
        def close(self):
            return quiescent.is_set()
    def run(*args, **kwargs):
        entered.set()
        raise AudioCleanupPending('Private native details must not escape', cleanup_owner=Owner())
    monkeypatch.setattr(capture_process, 'run_process', run)
    adapter = installation.WindowsBundleAdapter(lambda root: None)
    outcomes = []
    def work():
        try:
            adapter._run_owned([tmp_path / 'python.exe'], root=tmp_path, timeout=1)
        except Exception as error:
            outcomes.append(error)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(2)
        assert thread.is_alive() and outcomes == []
    finally:
        quiescent.set()
        thread.join(2)
    assert not thread.is_alive() and len(outcomes) == 1
    assert isinstance(outcomes[0], TrustError)
    assert 'Private native' not in str(outcomes[0])


@pytest.mark.parametrize('interruption_type', [KeyboardInterrupt, SystemExit])
def test_native_cleanup_defers_interrupt_and_retries_without_background_thread(tmp_path, monkeypatch, interruption_type):
    from filelock import FileLock, Timeout
    from carr_library.audio import capture_process
    from carr_library.audio.cleanup import pending_cleanup
    attempted, release = threading.Event(), threading.Event()
    lock_path = tmp_path / 'maintenance.lock'
    native_file = tmp_path / 'program-still-owned'
    native_file.write_bytes(b'Owned native program')
    real_start = threading.Thread.start
    def start(thread):
        if thread.name == 'audio-cleanup':
            raise RuntimeError('Synthetic background cleanup admission failure')
        return real_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    owners, outcomes, released = [], [], []
    def run(*args, **kwargs):
        pending = pending_cleanup(lambda: release.is_set(), message='Synthetic pending family')
        owner = pending.cleanup_owner
        owners.append(owner)
        native_event = owner.quiescent_event
        class InterruptedEvent:
            interrupted = False
            def is_set(self):
                return native_event.is_set()
            def set(self):
                native_event.set()
            def wait(self, timeout=None):
                if not self.interrupted:
                    self.interrupted = True
                    attempted.set()
                    raise interruption_type('Synthetic stop while family remains owned')
                return native_event.wait(min(timeout or .01, .01))
        owner.quiescent_event = InterruptedEvent()
        raise pending
    monkeypatch.setattr(capture_process, 'run_process', run)
    adapter = installation.WindowsBundleAdapter(lambda root: None)
    def work():
        with FileLock(lock_path, timeout=0):
            try:
                adapter._run_owned([tmp_path / 'python.exe'], root=tmp_path, timeout=1)
            except BaseException as error:
                outcomes.append(error)
            finally:
                released.append(owners[0].quiescent_event.is_set())
    thread = threading.Thread(target=work, name='maintenance-fixture')
    thread.start()
    try:
        assert attempted.wait(2)
        assert thread.is_alive() and outcomes == [] and released == []
        assert owners[0].last_error is not None
        with pytest.raises(Timeout):
            with FileLock(lock_path, timeout=0):
                pytest.fail('Maintenance lease released before native cleanup.')
        assert native_file.read_bytes() == b'Owned native program'
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and released == [True]
    assert len(outcomes) == 1 and isinstance(outcomes[0], interruption_type)
    with FileLock(lock_path, timeout=0):
        assert owners[0].quiescent_event.is_set()
