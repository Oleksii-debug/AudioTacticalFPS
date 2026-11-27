"""Typed scan lineage and retained pending inputs survive full-library backups."""
from copy import deepcopy
from hashlib import sha256
import json
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.scan_pages import successor_id
from carr_library.library_backup import create_backup, restore_backup, verify_backup


def planned(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Restorable scan pages')
    selected = tmp_path / 'selected originals'
    selected.mkdir()
    for number in range(5):
        (selected / f'{number}.txt').write_text(f'Original source {number}.', encoding='utf-8')
    first = app.plan_import_batch_page(workspace, [selected], file_budget=2)
    return app, workspace, selected, first


def test_restore_rebackup_preserves_saved_inputs_but_never_reactivates_external_cursor(tmp_path, monkeypatch):
    app, workspace, selected, first = planned(tmp_path)
    second = app.continue_import_scan(workspace, first['batch_id'])
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['pending']['version'] == 2
    summaries = manifest['pending']['scan_pages']
    assert {row['batch_id'] for row in summaries} == {first['batch_id'], second['batch_id']}
    assert all(row['unscanned_sources'] == 'external_not_included'
               and row['restore_traversal'] == 'inactive' for row in summaries)
    verify_backup(backup, digest)
    shutil.rmtree(selected)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    a = restored.import_batch(workspace, first['batch_id'])
    b = restored.import_batch(workspace, second['batch_id'])
    assert not a['scan']['resumable'] and not b['scan']['resumable']
    assert a['scan']['roots'] == first['scan']['roots']
    assert all(row['source'].startswith(str(restored.root / 'originals')) for row in a['rows'] + b['rows'])
    monkeypatch.setattr(batch_scan, 'scan_paths_page',
                        lambda *a, **k: pytest.fail('Historical roots must never be scanned.'))
    assert restored.continue_import_scan(workspace, a['batch_id'])['batch_id'] == b['batch_id']
    with pytest.raises(ValueError, match='historical only'):
        restored.continue_import_scan(workspace, b['batch_id'])
    for batch in (a, b):
        assert restored.run_import_batch(workspace, batch['batch_id'])['counts']['complete'] == 2
    assert len(restored.search(workspace, 'Original')) == 4
    rebased = tmp_path / 'rebackup'
    second_digest = create_backup(restored, rebased)
    again = LibraryApp(restore_backup(rebased, tmp_path / 'twice restored', second_digest))
    assert len(again.search(workspace, 'Original')) == 4
    assert not again.import_batch(workspace, b['batch_id'])['scan']['resumable']


def test_orphan_successor_from_interrupted_parent_link_is_backed_up_and_recovered(tmp_path, monkeypatch):
    app, workspace, selected, first = planned(tmp_path)
    original = ImportBatchStore._store
    def fail_parent(self, record, **kwargs):
        if record['batch_id'] == first['batch_id'] and record['scan']['next_batch'] is not None:
            raise OSError('injected interrupted parent link')
        return original(self, record, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(ImportBatchStore, '_store', fail_parent)
        with pytest.raises(OSError, match='parent link'):
            app.continue_import_scan(workspace, first['batch_id'])
    child_id = successor_id(first['batch_id'], first['scan']['next_cursor'])
    assert app.import_batch(workspace, first['batch_id'])['scan']['next_batch'] is None
    archive = tmp_path / 'orphan backup'
    digest = create_backup(app, archive)
    shutil.rmtree(selected)
    restored = LibraryApp(restore_backup(archive, tmp_path / 'restored orphan', digest))
    monkeypatch.setattr(batch_scan, 'scan_paths_page',
                        lambda *a, **k: pytest.fail('Orphan recovery must not read the old folder.'))
    assert restored.continue_import_scan(workspace, first['batch_id'])['batch_id'] == child_id
    assert restored.import_batch(workspace, first['batch_id'])['scan']['next_batch'] == child_id


def test_backup_refuses_a_live_real_scan_page_lock(tmp_path):
    app, workspace, selected, first = planned(tmp_path)
    destination = tmp_path / 'must not publish'
    with ImportBatchStore(app)._lock(first['batch_id']):
        with pytest.raises(ValueError, match='batch is running'):
            create_backup(app, destination)
    assert not destination.exists()
    assert app.import_batch(workspace, first['batch_id'])['scan']['next_batch'] is None


def test_complete_backup_refuses_missing_linked_successor(tmp_path):
    app, workspace, selected, first = planned(tmp_path)
    second = app.continue_import_scan(workspace, first['batch_id'])
    (app.root / 'import-batches' / (second['batch_id'] + '.json')).unlink()
    with pytest.raises(ValueError, match='successor is missing'):
        create_backup(app, tmp_path / 'refused archive')
    assert not (tmp_path / 'refused archive').exists()


def test_declared_scan_summary_is_revalidated_against_hashed_batch_records(tmp_path):
    app, workspace, selected, first = planned(tmp_path)
    archive = tmp_path / 'backup'
    create_backup(app, archive)
    path = archive / 'manifest.json'
    metadata = json.loads(path.read_bytes())
    metadata['pending']['scan_pages'][0]['has_continuation'] = False
    raw = (json.dumps(metadata, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')
    path.write_bytes(raw)
    # Even a caller-supplied replacement manifest checksum cannot make its
    # pure summary disagree with the independently hashed saved batch.
    with pytest.raises(ValueError, match='summary differs'):
        verify_backup(archive, sha256(raw).hexdigest())


def test_cancelled_scan_backup_publishes_no_archive_and_preserves_plan(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, workspace, selected, first = planned(tmp_path)
    path = app.root / 'import-batches' / (first['batch_id'] + '.json')
    before = path.read_bytes()
    original, stopped = pending_backup.planned_snapshot, False
    def cancel_after_copy_source(source):
        nonlocal stopped
        value = original(source)
        stopped = True
        return value
    monkeypatch.setattr(pending_backup, 'planned_snapshot', cancel_after_copy_source)
    with pytest.raises(InterruptedError):
        create_backup(app, tmp_path / 'cancelled backup', cancelled=lambda: stopped)
    assert not (tmp_path / 'cancelled backup').exists()
    assert path.read_bytes() == before
