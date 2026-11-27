"""Actual output commits remain distinguishable from later cleanup failures."""
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.sync import service as service_module, vault as vault_module
from carr_library.sync.service import SyncRestoredError, SyncService
from carr_library.sync.vault import Vault


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Retained exact source')
    book = app.add_text(workspace, 'Original', 'Exact source after a late failure.')
    return app, workspace, book


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_vault_commit_returns_its_actual_receipt_after_parent_sync_failure(tmp_path, monkeypatch, error_type):
    prepared = tmp_path / 'prepared'
    prepared.mkdir()
    (prepared / 'original.txt').write_bytes(b'Exact payload')
    vault = Vault(tmp_path / 'portable')
    original = vault_module._sync_directory

    def fail_only_after_head(path):
        if Path(path) == vault.root and (vault.root / 'head.json').exists():
            raise error_type('Synthetic postcommit durability interruption')
        return original(path)

    monkeypatch.setattr(vault_module, '_sync_directory', fail_only_after_head)
    try:
        receipt = vault.publish(prepared, expected_head=None)
    except KeyboardInterrupt:
        pytest.fail('Late interruption hid an already committed snapshot receipt.')
    assert receipt.snapshot_id == receipt.transfer_token == vault.head()
    assert receipt.durability_warning
    monkeypatch.undo()
    restored = Vault(vault.root).materialize(receipt.snapshot_id, tmp_path / 'recovered',
                                           expected_token=receipt.transfer_token)
    assert (restored / 'original.txt').read_bytes() == b'Exact payload'


@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_actual_restore_retains_verified_output_and_completion_receipt(source, tmp_path, monkeypatch, error_type):
    app, workspace, _ = source
    service = SyncService(app)
    folder, destination = tmp_path / 'portable', tmp_path / 'restored'
    published = service.publish(folder, 'Checked source', None)
    original = vault_module._sync_directory

    def fail_after_final_rename(path):
        if Path(path) == destination.parent and destination.exists():
            raise error_type('Synthetic postcommit directory sync interruption')
        return original(path)

    monkeypatch.setattr(vault_module, '_sync_directory', fail_after_final_rename)
    try:
        with pytest.raises(SyncRestoredError) as caught:
            service.restore(folder, published['token'], destination)
    except KeyboardInterrupt:
        pytest.fail('Late interruption hid an already committed restore receipt.')
    assert caught.value.destination == str(destination)
    assert caught.value.token == published['token']
    recovered = LibraryApp(destination)
    assert recovered.search(workspace, 'Exact source')[0].quote == 'Exact source'
    assert app.search(workspace, 'Exact source')[0].quote == 'Exact source'


def test_private_temporary_cleanup_cannot_hide_published_code_and_saved_history(source, tmp_path, monkeypatch):
    app, workspace, _ = source
    service = SyncService(app)
    folder = tmp_path / 'portable'
    original = service_module.TemporaryDirectory.cleanup

    def cleanup_then_fail(owner):
        original(owner)
        if Path(owner.name).name.startswith('prepare-') and (folder / 'head.json').exists():
            raise OSError('Synthetic private cleanup failure after actual publication')

    monkeypatch.setattr(service_module.TemporaryDirectory, 'cleanup', cleanup_then_fail)
    receipt = service.publish(folder, 'Already published', None)
    assert receipt['id'] == receipt['token'] == Vault(folder).head()
    assert receipt['warnings']
    monkeypatch.undo()
    assert SyncService(app).inspect(folder, receipt['token'])['verified']
    assert app.search(workspace, 'Exact source')[0].quote == 'Exact source'
    state = service._state()
    assert state['vaults'][service._vault_key(folder)]['head'] == receipt['token']


@pytest.mark.parametrize('prefix', ['.carr-portable-restore-', 'read-'])
@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
def test_restore_private_cleanup_preserves_exact_saved_output_receipt(source, tmp_path, monkeypatch,
                                                                    prefix, error_type):
    app, workspace, _ = source
    service = SyncService(app)
    folder, destination = tmp_path / 'portable', tmp_path / 'restored'
    published = service.publish(folder, 'Exact source', None)
    original = service_module.TemporaryDirectory.cleanup
    triggered = []
    def cleanup_then_fail(owner):
        original(owner)
        if Path(owner.name).name.startswith(prefix) and destination.exists():
            triggered.append(True)
            raise error_type('Synthetic private cleanup interruption after actual restore')
    monkeypatch.setattr(service_module.TemporaryDirectory, 'cleanup', cleanup_then_fail)
    try:
        with pytest.raises(SyncRestoredError) as caught:
            service.restore(folder, published['token'], destination)
    except KeyboardInterrupt:
        pytest.fail('Late private cleanup hid a committed restored library.')
    assert triggered and caught.value.destination == str(destination)
    assert caught.value.token == published['token']
    assert LibraryApp(destination).search(workspace, 'Exact source')[0].quote == 'Exact source'
    assert app.search(workspace, 'Exact source')[0].quote == 'Exact source'


@pytest.mark.parametrize('change', ['foreign_directory', 'changed_original'])
def test_restore_cleanup_cannot_adopt_replaced_or_mutated_output(source, tmp_path, monkeypatch, change):
    import shutil
    app, _, _ = source
    service = SyncService(app)
    folder, destination = tmp_path / 'portable', tmp_path / 'restored'
    published = service.publish(folder, 'Exact source', None)
    original = service_module.TemporaryDirectory.cleanup
    triggered = []
    def cleanup_then_fail(owner):
        original(owner)
        if Path(owner.name).name.startswith('.carr-portable-restore-') and destination.exists():
            if change == 'foreign_directory':
                previous = tmp_path / 'actual-owned-library'
                destination.rename(previous)
                shutil.copytree(previous, destination)
            else:
                (destination / 'portable-sync-state.json').write_bytes(b'Unrelated state')
            triggered.append(True)
            raise OSError('Synthetic foreign restored output')
    monkeypatch.setattr(service_module.TemporaryDirectory, 'cleanup', cleanup_then_fail)
    with pytest.raises(OSError, match='foreign restored output'):
        service.restore(folder, published['token'], destination)
    assert triggered
