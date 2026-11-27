"""Actual commit, durability and private-cleanup boundaries for portable copies."""
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.sync import service as module, vault
from carr_library.sync.service import SyncService, SyncRestoredError


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Commit boundary source')
    app.add_text(workspace, 'Original', 'Retained exact source at the commit boundary.')
    return app, workspace, SyncService(app)


@pytest.mark.parametrize('exists', [False, True])
def test_history_read_never_initializes_a_missing_or_empty_vault(library, tmp_path, exists):
    app, _, service = library
    folder = tmp_path / 'new-vault'
    if exists:
        folder.mkdir()
    assert service.list(folder) == []
    assert folder.exists() is exists
    if exists:
        assert list(folder.iterdir()) == []
    assert not (app.root / module.STATE).exists()


def test_history_read_rejects_foreign_folder_without_adding_vault_files(library, tmp_path):
    _, _, service = library
    folder = tmp_path / 'foreign'
    folder.mkdir()
    original = folder / 'keep.txt'
    original.write_bytes(b'Unrelated original bytes')
    with pytest.raises((ValueError, FileNotFoundError)):
        service.list(folder)
    assert list(folder.iterdir()) == [original]
    assert original.read_bytes() == b'Unrelated original bytes'


def test_inspection_never_initializes_a_missing_vault(library, tmp_path):
    _, _, service = library
    folder = tmp_path / 'missing'
    with pytest.raises(FileNotFoundError):
        service.inspect(folder, 'a' * 64)
    assert not folder.exists()


def test_prepare_cleanup_failure_precedes_publication_and_old_copy_still_restores(library, tmp_path, monkeypatch):
    app, workspace, service = library
    folder = tmp_path / 'portable'
    first = service.publish(folder, 'First retained checkpoint', None)
    app.add_text(workspace, 'Later change', 'This later change belongs to the next checkpoint.')
    original = module.TemporaryDirectory

    class FailPrepareCleanup(original):
        def __exit__(self, *args):
            result = super().__exit__(*args)
            if Path(self.name).name.startswith('prepare-'):
                raise OSError('Injected private preparation cleanup failure')
            return result

    with monkeypatch.context() as patch:
        patch.setattr(module, 'TemporaryDirectory', FailPrepareCleanup)
        with pytest.raises(OSError, match='preparation cleanup'):
            service.publish(folder, 'Second checkpoint', first['id'])
    assert vault.Vault.open_existing(folder).head() == first['id']
    assert service.inspect(folder, first['token'])['verified']
    recovered = tmp_path / 'old-copy'
    service.restore(folder, first['token'], recovered)
    assert LibraryApp(recovered).search(workspace, 'Retained exact source')
    assert not LibraryApp(recovered).search(workspace, 'later change')
    second = service.publish(folder, 'Second checkpoint', first['id'])
    assert second['id'] != first['id'] and second['durability_confirmed']


def test_head_commit_survives_flush_failure_as_visible_unconfirmed_receipt(library, tmp_path, monkeypatch):
    _, _, service = library
    folder = tmp_path / 'portable'
    original = vault._write_file

    def fail_after_real_head(path, *args, **options):
        result = original(path, *args, **options)
        if path.name == 'head.json':
            raise OSError('Injected after actual head replacement')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(vault, '_write_file', fail_after_real_head)
        receipt = service.publish(folder, 'Visible checkpoint', None)
    assert receipt['durability_confirmed'] is False
    assert receipt['status'] == 'attention' and receipt['warnings']
    assert vault.Vault.open_existing(folder).head() == receipt['id'] == receipt['token']
    assert service.inspect(folder, receipt['token'])['verified']


def test_cache_cleanup_failure_cannot_hide_commit_or_call_status_after_commit(library, tmp_path, monkeypatch):
    _, _, service = library
    folder = tmp_path / 'portable'
    original = module.shutil.rmtree
    cache_failures = []

    def retain_prepared_cache(path, *args, **options):
        path = Path(path)
        if path.parent == service.cache_root and len(path.name) == 64:
            cache_failures.append(path)
            raise OSError('Injected private cache cleanup failure')
        return original(path, *args, **options)

    def status(message):
        if message.startswith('Checkpoint saved.'):
            raise AssertionError('A callback ran after external publication')

    with monkeypatch.context() as patch:
        patch.setattr(module.shutil, 'rmtree', retain_prepared_cache)
        receipt = service.publish(folder, 'Retained checkpoint', None, on_status=status)
    assert cache_failures and receipt['cleanup_pending'] is True
    assert receipt['durability_confirmed'] is True and receipt['status'] == 'complete'
    assert service.inspect(folder, receipt['token'])['verified']


def test_restore_flush_failure_keeps_exact_destination_and_recovery_code(library, tmp_path, monkeypatch):
    _, workspace, service = library
    folder, destination = tmp_path / 'portable', tmp_path / 'recovered'
    receipt = service.publish(folder, 'Restore checkpoint', None)
    original = vault._sync_directory

    def fail_final_flush(path):
        if Path(path) == destination.parent and destination.exists():
            raise OSError('Injected final destination flush failure')
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(vault, '_sync_directory', fail_final_flush)
        with pytest.raises(SyncRestoredError) as caught:
            service.restore(folder, receipt['token'], destination)
    error = caught.value
    assert error.destination == str(destination) and error.token == receipt['token']
    assert error.durability_confirmed is False
    assert LibraryApp(destination).search(workspace, 'Retained exact source')
    with pytest.raises(FileExistsError):
        service.restore(folder, receipt['token'], destination)


def test_restore_cleanup_failure_is_not_reported_as_absent_output(library, tmp_path, monkeypatch):
    _, workspace, service = library
    folder, destination = tmp_path / 'portable', tmp_path / 'recovered'
    receipt = service.publish(folder, 'Restore checkpoint', None)
    original = module.TemporaryDirectory

    class FailFinalCleanup(original):
        def __exit__(self, *args):
            result = super().__exit__(*args)
            if Path(self.name).name.startswith('.carr-portable-restore-'):
                raise OSError('Injected after final stage cleanup')
            return result

    with monkeypatch.context() as patch:
        patch.setattr(module, 'TemporaryDirectory', FailFinalCleanup)
        with pytest.raises(SyncRestoredError) as caught:
            service.restore(folder, receipt['token'], destination)
    assert caught.value.durability_confirmed is True
    assert caught.value.destination == str(destination)
    assert caught.value.token == receipt['token']
    assert LibraryApp(destination).search(workspace, 'Retained exact source')


def test_verified_read_staging_cleanup_finishes_before_destination_publication(library, tmp_path, monkeypatch):
    _, _, service = library
    folder, destination = tmp_path / 'portable', tmp_path / 'recovered'
    receipt = service.publish(folder, 'Restore checkpoint', None)
    original = module.TemporaryDirectory

    class FailReadCleanup(original):
        def __exit__(self, *args):
            result = super().__exit__(*args)
            if Path(self.name).name.startswith('read-'):
                raise OSError('Injected read staging cleanup failure')
            return result

    with monkeypatch.context() as patch:
        patch.setattr(module, 'TemporaryDirectory', FailReadCleanup)
        with pytest.raises(OSError, match='read staging'):
            service.restore(folder, receipt['token'], destination)
    assert not destination.exists()
    assert service.inspect(folder, receipt['token'])['verified']


@pytest.mark.parametrize('corrupt', [False, True])
def test_private_materialize_warning_never_bypasses_content_validation(library, tmp_path, monkeypatch, corrupt):
    _, _, service = library
    folder = tmp_path / 'portable'
    receipt = service.publish(folder, 'Checked snapshot', None)
    original = vault.Vault.materialize

    def materialize_then_warn(self, snapshot, destination, *, expected_token, **options):
        result = original(self, snapshot, destination, expected_token=expected_token, **options)
        if corrupt:
            (result / 'snapshot.zip').write_bytes(b'Changed after materialization')
        raise vault.VaultMaterializedError(result, expected_token)

    with monkeypatch.context() as patch:
        patch.setattr(vault.Vault, 'materialize', materialize_then_warn)
        if corrupt:
            with pytest.raises(ValueError):
                service.inspect(folder, receipt['token'])
        else:
            assert service.inspect(folder, receipt['token'])['verified']



def test_unreadable_verified_subtree_never_publishes_a_partial_restore(library, tmp_path, monkeypatch):
    _, workspace, service = library
    folder, destination = tmp_path / 'portable', tmp_path / 'recovered'
    receipt = service.publish(folder, 'Complete checkpoint', None)
    original_copy, original_scan = module._copy_verified_tree, module.os.scandir
    seen = []

    def copy_with_unreadable_originals(source, target, cancelled):
        blocked = source / 'originals'
        assert blocked.is_dir()
        def scan(path):
            if not isinstance(path, int) and Path(path) == blocked:
                seen.append(Path(path))
                raise PermissionError('Injected unreadable verified originals')
            return original_scan(path)
        with monkeypatch.context() as patch:
            patch.setattr(module.os, 'scandir', scan)
            return original_copy(source, target, cancelled)

    with monkeypatch.context() as patch:
        patch.setattr(module, '_copy_verified_tree', copy_with_unreadable_originals)
        with pytest.raises(PermissionError, match='unreadable verified originals'):
            service.restore(folder, receipt['token'], destination)
    assert seen and not destination.exists()
    assert service.inspect(folder, receipt['token'])['verified']
    service.restore(folder, receipt['token'], destination)
    assert LibraryApp(destination).search(workspace, 'Retained exact source')

@pytest.mark.parametrize('operation', ['publish', 'restore'])
def test_actual_operation_lock_release_preserves_committed_recovery_identity(library, tmp_path, monkeypatch, operation):
    from filelock import FileLock
    _, workspace, service = library
    folder, destination = tmp_path / 'portable', tmp_path / 'restored'
    saved = service.publish(folder, 'Original checkpoint', None) if operation == 'restore' else None
    release = FileLock.release
    armed = [True]
    def fail_after_release(lock, *args, **kwargs):
        result = release(lock, *args, **kwargs)
        if armed[0] and Path(lock.lock_file) == service.root / '.carr-portable.lock':
            armed[0] = False
            raise OSError('Injected after actual portable operation release')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(FileLock, 'release', fail_after_release)
        if operation == 'publish':
            result = service.publish(folder, 'Saved checkpoint', None)
            assert result['status'] == 'attention' and result['operation_cleanup_pending']
            assert result['history_saved'] and result['durability_confirmed']
        else:
            with pytest.raises(SyncRestoredError) as caught:
                service.restore(folder, saved['token'], destination)
            assert caught.value.destination == str(destination)
            assert caught.value.token == saved['token'] and caught.value.durability_confirmed
    assert not armed[0]
    if operation == 'publish':
        assert vault.Vault.open_existing(folder).head() == result['token']
        assert service.inspect(folder, result['token'])['verified']
    else:
        assert LibraryApp(destination).search(workspace, 'Retained exact source')
