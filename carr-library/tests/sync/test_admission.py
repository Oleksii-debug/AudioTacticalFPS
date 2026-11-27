"""Shared real reservations, measured volumes and retained pending cleanup."""
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceUnavailable
from carr_library.sync import admission, service as module
from carr_library.sync.service import SyncService


def app_at(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Shared transfer resources')
    app.add_text(workspace, 'Exact source', 'Measured retained source bytes.')
    return app


@pytest.mark.parametrize('resource', ['memory', 'disk'])
def test_resource_refusal_precedes_backup_encryption_or_external_initialization(tmp_path, monkeypatch, resource):
    app = app_at(tmp_path)
    limits = ResourceLimits(cpu_slots=1,
        memory_bytes=1 if resource == 'memory' else 512 * 1024**2,
        disk_bytes=1 if resource == 'disk' else 512 * 1024**2, min_free_disk_bytes=0)
    broker = ResourceBroker.for_app(app.root, limits=limits)
    calls = []
    monkeypatch.setattr(module, 'create_backup', lambda *a, **k: calls.append('backup'))
    with pytest.raises(ResourceUnavailable):
        SyncService(app, broker=broker).publish(tmp_path / 'vault', 'Denied', None,
                                                'Synthetic admission password')
    assert calls == []
    assert not (tmp_path / 'vault').exists()
    assert not (app.root / module.STATE).exists()
    assert broker.snapshot()['leases'] == []


def test_real_encryption_and_read_reserve_actual_sizes_and_selected_volumes(tmp_path, monkeypatch):
    from carr_library.sync import crypto
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1,
        memory_bytes=512 * 1024**2, disk_bytes=1024**3, min_free_disk_bytes=0))
    calls = []
    encrypt, decrypt = crypto.encrypt_file, crypto.decrypt_file

    def observing(function, phase):
        def run(*args, **kwargs):
            leases = broker.snapshot()['leases']
            assert len(leases) == 1
            request = leases[0]['request']
            assert request['cpu_slots'] == 1 and request['memory_bytes'] == admission.MEMORY_BYTES
            assert 0 < request['disk_bytes'] < 256 * 1024**2
            assert Path(request['disk_path']) == tmp_path
            calls.append((phase, request))
            return function(*args, **kwargs)
        return run

    monkeypatch.setattr(crypto, 'encrypt_file', observing(encrypt, 'encrypt'))
    monkeypatch.setattr(crypto, 'decrypt_file', observing(decrypt, 'decrypt'))
    service = SyncService(app, broker=broker)
    folder = tmp_path / 'vault'
    receipt = service.publish(folder, 'Actual encrypted transfer', None,
                              'Synthetic admission password')
    assert service.inspect(folder, receipt['token'], 'Synthetic admission password')['verified']
    target_parent = tmp_path / 'chosen-volume'
    target_parent.mkdir()
    service.restore(folder, receipt['token'], target_parent / 'new-library',
                    'Synthetic admission password')
    assert [phase for phase, _ in calls] == ['encrypt', 'decrypt', 'decrypt']
    assert any(Path(path) == target_parent for path, _ in calls[-1][1]['additional_disks'])
    assert broker.snapshot()['leases'] == []


def test_growth_during_actual_admission_is_refused_before_copy(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    real = broker.reserve
    changed = app.root / 'reading-state.json'

    @contextmanager
    def grow(*args, **kwargs):
        with real(*args, **kwargs) as lease:
            changed.write_bytes(b'x' * 4096)
            yield lease

    monkeypatch.setattr(broker, 'reserve', grow)
    called = []
    monkeypatch.setattr(module, 'create_backup', lambda *a, **k: called.append(True))
    with pytest.raises(ValueError, match='grew while waiting'):
        SyncService(app, broker=broker).publish(tmp_path / 'vault', 'Changed', None)
    assert not called and not (tmp_path / 'vault').exists()
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('masked', [False, True], ids=['direct', 'wrapped'])
def test_pending_native_owner_retains_real_lock_stage_and_broker_until_quiet(
        tmp_path, monkeypatch, masked):
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    done = Event()
    owner = CleanupOwner(done.is_set)
    pending = AudioCleanupPending('Synthetic owned verifier is still stopping.', cleanup_owner=owner)
    retained = []

    def stopped(_app, destination, **kwargs):
        destination.mkdir()
        (destination / 'owned-original').write_bytes(b'Exact private bytes.')
        retained.append(destination)
        if masked:
            try:
                raise pending
            except AudioCleanupPending as error:
                raise OSError('Synthetic failed journal') from error
        raise pending

    monkeypatch.setattr(module, 'create_backup', stopped)
    service = SyncService(app, broker=broker)
    with pytest.raises((AudioCleanupPending, OSError)):
        service.publish(tmp_path / 'vault', 'Pending verifier', None)
    assert len(broker.snapshot()['leases']) == 1
    assert retained[0].joinpath('owned-original').read_bytes() == b'Exact private bytes.'
    with pytest.raises(RuntimeError, match='Another portable-snapshot'):
        with SyncService(app, broker=broker)._operation():
            pytest.fail('Pending ownership was released.')
    assert owner.close() is False
    assert retained[0].exists()
    done.set()
    assert owner.close() is True
    assert not retained[0].exists()
    assert broker.snapshot()['leases'] == []
    with service._operation():
        pass


def test_cancelled_admission_and_entry_limit_do_not_start_copy(tmp_path, monkeypatch):
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    called = []
    monkeypatch.setattr(module, 'create_backup', lambda *a, **k: called.append(True))
    with pytest.raises(InterruptedError):
        SyncService(app, broker=broker).publish(tmp_path / 'vault', 'Cancelled', None,
                                               cancelled=lambda: True)
    assert not called and not (tmp_path / 'vault').exists()
    tree = tmp_path / 'many'
    tree.mkdir()
    for index in range(4):
        (tree / str(index)).mkdir()
    monkeypatch.setattr(admission, 'MAX_FILES', 3)
    with pytest.raises(ValueError, match='entry budget'):
        admission.tree_bytes(tree)


def test_restore_lease_and_policy_callback_cover_final_copy_publication_and_cleanup(
        tmp_path, monkeypatch):
    from carr_library.sync import vault
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    service = SyncService(app, broker=broker)
    folder = tmp_path / 'vault'
    saved = service.publish(folder, 'Retain the reservation', None)
    target = tmp_path / 'restored'
    captured, phases = [], []
    reserve = broker.reserve

    @contextmanager
    def observing_reservation(*args, **kwargs):
        with reserve(*args, **kwargs) as lease:
            captured.append(lease)
            yield lease

    original_restore = module._prepare_restore
    def restore(*args):
        assert args[-1].__self__ is captured[-1]
        assert captured[-1]._lock.held and len(broker.snapshot()['leases']) == 1
        phases.append('final-copy')
        return original_restore(*args)

    original_publish = vault._publish_directory
    def publish(stage, destination, **kwargs):
        if Path(destination) == target:
            assert len(broker.snapshot()['leases']) == 1
            assert captured[-1]._lock.held
            phases.append('publish')
        return original_publish(stage, destination, **kwargs)

    original_temporary = module.TemporaryDirectory
    class CheckedCleanup(original_temporary):
        def __exit__(self, *args):
            if Path(self.name).name.startswith('.carr-portable-restore-'):
                assert target.exists()
                assert len(broker.snapshot()['leases']) == 1
                phases.append('cleanup')
            return super().__exit__(*args)

    monkeypatch.setattr(broker, 'reserve', observing_reservation)
    monkeypatch.setattr(module, '_prepare_restore', restore)
    monkeypatch.setattr(vault, '_publish_directory', publish)
    monkeypatch.setattr(module, 'TemporaryDirectory', CheckedCleanup)
    result = service.restore(folder, saved['token'], target)
    assert result['verified'] and phases == ['final-copy', 'publish', 'cleanup']
    assert len(captured) == 1 and broker.snapshot()['leases'] == []


def test_latched_lease_stop_is_obeyed_by_canonical_restore_even_when_ui_token_is_clear(
        tmp_path, monkeypatch):
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    service = SyncService(app, broker=broker)
    saved = service.publish(tmp_path / 'vault', 'Lease cancellation', None)
    original = module._prepare_restore
    def stop(source, destination, checksum, final, cancelled):
        cancelled.__self__._stopped = True
        return original(source, destination, checksum, final, cancelled)
    monkeypatch.setattr(module, '_prepare_restore', stop)
    with pytest.raises(InterruptedError):
        service.restore(tmp_path / 'vault', saved['token'], tmp_path / 'rejected',
                        cancelled=lambda: False)
    assert not (tmp_path / 'rejected').exists()
    assert broker.snapshot()['leases'] == []


def test_postcommit_lease_release_error_retains_the_exact_restore_receipt(tmp_path, monkeypatch):
    from carr_library.sync.service import SyncRestoredError
    app = app_at(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    service = SyncService(app, broker=broker)
    saved = service.publish(tmp_path / 'vault', 'Durable receipt', None)
    real = broker.reserve
    @contextmanager
    def failed_release(*args, **kwargs):
        with real(*args, **kwargs) as lease:
            yield lease
        raise RuntimeError('Synthetic resource finalization failure')
    monkeypatch.setattr(broker, 'reserve', failed_release)
    target = tmp_path / 'restored'
    with pytest.raises(SyncRestoredError) as caught:
        service.restore(tmp_path / 'vault', saved['token'], target)
    assert caught.value.destination == str(target)
    assert caught.value.token == saved['token'] and caught.value.durability_confirmed
    assert LibraryApp(target).workspaces()
    assert broker.snapshot()['leases'] == []
