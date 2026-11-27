"""Real complete library transfer, separate history, secrets and recovery."""
import json
from pathlib import Path
import os
import shutil
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.sync.service import SyncService, SyncConflict, SyncPublishedError
from carr_library.sync.vault import Vault
from carr_library.sync import service as module

PASSWORD = 'Синтетичний пароль 12345'


@pytest.fixture
def populated(tmp_path):
    app = LibraryApp(tmp_path / 'Особиста бібліотека')
    first = app.create_workspace('History')
    second = app.create_workspace('Archived studies')
    topic = app.create_topic(first, 'Exact evidence')
    app.add_text(first, 'Synthetic source', 'Точна цитата. Railway connected cities.', authors=('Author',))
    hit, = app.search(first, 'Точна цитата')
    app.save_hit(first, topic, hit.ref, hit.revision, 'Separate note')
    app.archive_workspace(second)
    return app, first, second, topic


def test_all_libraries_archived_metadata_and_quote_survive_roundtrip(populated, tmp_path):
    app, first, second, topic = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    original = app.evidence(first, topic)[0]
    result = service.publish(folder, 'Research checkpoint', None)
    rows = service.list(folder)
    assert len(rows) == 1 and rows[0]['head'] and rows[0]['id'] == result['token']
    preview = service.inspect(folder, result['token'])
    assert preview['verified'] and preview['restore_mode'] == 'new-folder-only'
    assert set(preview['libraries']) == {'History', 'Archived studies'}
    target = tmp_path / 'restored'
    restored = service.restore(folder, result['token'], target)
    assert restored['destination'] == str(target)
    recovered = LibraryApp(target)
    assert recovered.evidence(first, topic)[0] == original
    assert app.evidence(first, topic)[0] == original
    assert next(w for w in recovered.workspaces(include_archived=True) if w.id == second).archived
    assert app.root != recovered.root


def test_empty_library_keeps_required_empty_original_directory(tmp_path):
    app = LibraryApp(tmp_path / 'empty')
    service = SyncService(app)
    result = service.publish(tmp_path / 'portable', 'Empty library', None)
    assert service.inspect(tmp_path / 'portable', result['token'])['libraries'] == []
    service.restore(tmp_path / 'portable', result['token'], tmp_path / 'recovered')
    assert (tmp_path / 'recovered' / 'originals').is_dir()
    assert LibraryApp(tmp_path / 'recovered').workspaces() == ()


def test_real_encrypted_roundtrip_opaque_payload_and_operation_password(populated, tmp_path):
    app, first, _, topic = populated
    service = SyncService(app)
    folder = tmp_path / 'encrypted'
    result = service.publish(folder, 'Private source title', None, PASSWORD)
    assert result['encrypted']
    for path in folder.rglob('*'):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes()
            assert b'Private source title' not in path.read_bytes()
            assert 'Точна цитата'.encode() not in path.read_bytes()
    for password in (None, 'wrong password with enough length'):
        with pytest.raises(ValueError):
            service.inspect(folder, result['token'], password)
    assert service.inspect(folder, result['token'], PASSWORD)['encrypted']
    service.restore(folder, result['token'], tmp_path / 'decrypted', PASSWORD)
    recovered = LibraryApp(tmp_path / 'decrypted')
    assert recovered.evidence(first, topic)[0] == app.evidence(first, topic)[0]
    assert PASSWORD not in (app.root / module.STATE).read_text()


@pytest.mark.parametrize('token', [None, '', 'a' * 63, 'x' * 64, '0' * 64])
def test_independent_token_required_and_invalid_restore_creates_nothing(populated, tmp_path, token):
    app, *_ = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    service.publish(folder, 'Checkpoint', None)
    with pytest.raises((ValueError, FileNotFoundError)):
        service.restore(folder, token, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


def test_two_devices_history_conflicts_require_explicit_recovery(populated, tmp_path):
    app, first, *_ = populated
    one = SyncService(app)
    folder = tmp_path / 'portable'
    first_receipt = one.publish(folder, 'First', None)
    one.restore(folder, first_receipt['token'], tmp_path / 'second-device')
    two = SyncService(LibraryApp(tmp_path / 'second-device'))
    two.app.add_text(first, 'Device two', 'Its independent update.')
    second_receipt = two.publish(folder, 'Second device checkpoint', first_receipt['id'])
    with pytest.raises(SyncConflict):
        one.publish(folder, 'Stale device', first_receipt['id'])
    with pytest.raises(SyncConflict):
        one.publish(folder, 'Refreshed but divergent', second_receipt['id'])
    assert Vault(folder).head() == second_receipt['id']
    # Old immutable snapshots remain inspectable with their independent code.
    assert one.inspect(folder, first_receipt['token'])['verified']


def test_unknown_nonempty_vault_cannot_be_adopted_by_refresh(populated, tmp_path):
    app, *_ = populated
    folder = tmp_path / 'portable'
    receipt = SyncService(app).publish(folder, 'First', None)
    outsider = SyncService(LibraryApp(tmp_path / 'outsider'))
    with pytest.raises(SyncConflict):
        outsider.publish(folder, 'Unrelated library', receipt['id'])


@pytest.mark.parametrize('encrypted', [False, True])
def test_cancelled_publish_reuses_only_verified_prepared_payload(populated, tmp_path, encrypted):
    app, *_ = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    cancel = Event()
    statuses = []
    def status(text):
        statuses.append(text)
        if text.startswith('Publishing checkpoint:'):
            cancel.set()
    password = PASSWORD if encrypted else None
    with pytest.raises(InterruptedError):
        service.publish(folder, 'Resumable', None, password, cancelled=cancel.is_set, on_status=status)
    pending = list(service.cache_root.glob('*/ready.json'))
    assert len(pending) == 1
    prepared = next((pending[0].parent / 'payload').iterdir()).read_bytes()
    result = service.publish(folder, 'Resumable', None, password, on_status=statuses.append)
    payload = Vault(folder).materialize(result['id'], tmp_path / 'materialized', expected_token=result['token'])
    assert next(payload.iterdir()).read_bytes() == prepared
    assert any('verified chunks reused' in text for text in statuses)
    assert not list(service.cache_root.glob('*/ready.json'))


def test_changed_cached_payload_and_matching_cache_hash_cannot_change_library(populated, tmp_path):
    app, *_ = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    cancel = Event()
    with pytest.raises(InterruptedError):
        service.publish(folder, 'Pending', None, cancelled=cancel.is_set,
            on_status=lambda text: cancel.set() if text.startswith('Publishing checkpoint:') else None)
    ready, = service.cache_root.glob('*/ready.json')
    cached = ready.parent / 'payload' / 'snapshot.zip'
    replacement_app = LibraryApp(tmp_path / 'other')
    other = SyncService(replacement_app)
    other_result = other.publish(tmp_path / 'other-vault', 'Other', None)
    replacement = Vault(tmp_path / 'other-vault').materialize(other_result['id'], tmp_path / 'other-payload', expected_token=other_result['token'])
    shutil.copyfile(replacement / 'snapshot.zip', cached)
    data = json.loads(ready.read_text())
    data['payload_sha256'] = module.hash_file(cached)
    data['archive_sha256'] = data['payload_sha256']
    ready.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='different library'):
        service.publish(folder, 'Pending', None)
    assert Vault(folder).head() is None


def test_restore_cancellation_at_final_status_never_publishes(populated, tmp_path):
    app, *_ = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    result = service.publish(folder, 'Checkpoint', None)
    cancel = Event()
    with pytest.raises(InterruptedError):
        service.restore(folder, result['token'], tmp_path / 'cancelled', cancelled=cancel.is_set,
            on_status=lambda text: cancel.set() if text.startswith('Restoring the checked') else None)
    assert not (tmp_path / 'cancelled').exists()
    assert not list(tmp_path.glob('.carr-portable-restore-*'))


def test_restore_destination_race_keeps_existing_directory(populated, tmp_path, monkeypatch):
    app, *_ = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    result = service.publish(folder, 'Checkpoint', None)
    from carr_library.sync import vault
    original = vault._publish_directory
    destination = tmp_path / 'raced'
    def race(stage, target, *, _committed=None):
        if target == destination:
            destination.mkdir()
            (destination / 'keep').write_text('Unrelated original')
        return original(stage, target, _committed=_committed)
    monkeypatch.setattr(vault, '_publish_directory', race)
    with pytest.raises(FileExistsError):
        service.restore(folder, result['token'], destination)
    assert (destination / 'keep').read_text() == 'Unrelated original'
    assert not (destination / 'library.sqlite').exists()


def test_published_receipt_survives_local_history_write_failure(populated, tmp_path, monkeypatch):
    app, *_ = populated
    service = SyncService(app)
    folder = tmp_path / 'portable'
    original = module._atomic_json
    def fail_after_commit(path, value):
        if value['vaults']:
            raise OSError('Injected history failure')
        return original(path, value)
    monkeypatch.setattr(module, '_atomic_json', fail_after_commit)
    with pytest.raises(SyncPublishedError) as caught:
        service.publish(folder, 'Checkpoint', None)
    receipt = caught.value.receipt
    assert Vault(folder).head() == receipt['id'] == receipt['token']
    assert service.inspect(folder, receipt['token'])['verified']


def test_replaced_data_folder_is_rejected_before_any_transfer(populated, tmp_path):
    app, *_ = populated
    service = SyncService(app)
    moved = tmp_path / 'moved'
    os.rename(app.root, moved)
    LibraryApp(app.root)
    with pytest.raises(ValueError, match='replaced'):
        service.publish(tmp_path / 'portable', 'Wrong root', None)
    assert not (tmp_path / 'portable').exists()


def test_scopes_existing_destination_and_nested_paths_are_rejected(populated, tmp_path):
    app, *_ = populated
    service = SyncService(app)
    with pytest.raises(ValueError):
        service.publish(app.root / 'nested', 'Bad path', None)
    folder = tmp_path / 'portable'
    receipt = service.publish(folder, 'Checkpoint', None)
    existing = tmp_path / 'existing'
    existing.mkdir()
    with pytest.raises(FileExistsError):
        service.restore(folder, receipt['token'], existing)
    for target in (app.root / 'restore', folder / 'restore'):
        with pytest.raises(ValueError):
            service.restore(folder, receipt['token'], target)
