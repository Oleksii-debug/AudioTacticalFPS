"""Durable bounded scan pages over the existing real import worker and queues."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.scan_pages import successor_id, validate_scan_relations


def sources(tmp_path, count=5):
    directory = tmp_path / 'selected originals'
    directory.mkdir()
    for number in range(count):
        (directory / f'{number:06d}.txt').write_text(f'Immutable source {number}.', encoding='utf-8')
    return directory


def journal(app, identity):
    return app.root / 'import-batches' / (identity + '.json')


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    return app, app.create_workspace('Scan pages')


def test_actual_5003_files_form_finite_saved_successors_after_reopen(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 5003)
    first = app.plan_import_batch_page(workspace, [root])
    assert first['version'] == 2 and first['total'] == 5000
    assert first['scan']['page'] == 1 and first['scan']['next_cursor']
    reopened = LibraryApp(app.root)
    second = reopened.continue_import_scan(workspace, first['batch_id'])
    assert second['total'] == 3 and second['scan']['next_cursor'] is None
    assert second['batch_id'] == successor_id(first['batch_id'], first['scan']['next_cursor'])
    assert [row['file'] for row in first['rows'] + second['rows']] == [f'{i:06d}.txt' for i in range(5003)]
    assert len({row['request_id'] for row in first['rows'] + second['rows']}) == 5003
    assert reopened.import_batch(workspace, first['batch_id'])['scan']['next_batch'] == second['batch_id']
    assert reopened.continue_import_scan(workspace, first['batch_id'])['batch_id'] == second['batch_id']
    assert len(reopened.import_batches(workspace)) == 2
    assert (root / '005002.txt').read_bytes() == b'Immutable source 5002.'


def test_successive_pages_reuse_real_worker_and_preserve_reviewed_exclusions(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 3)
    hashes = {path.name: sha256(path.read_bytes()).hexdigest() for path in root.iterdir()}
    first = app.plan_import_batch_page(workspace, [root], file_budget=2)
    first = app.set_import_batch_row(workspace, first['batch_id'], first['rows'][0]['row_id'], included=False)
    first = app.run_import_batch(workspace, first['batch_id'])
    assert first['counts']['excluded'] == 1 and first['counts']['complete'] == 1
    second = app.continue_import_scan(workspace, first['batch_id'])
    assert second['scan']['next_cursor'] is None
    second = app.run_import_batch(workspace, second['batch_id'])
    assert second['counts']['complete'] == 1
    reopened = LibraryApp(app.root)
    assert reopened.import_batch(workspace, first['batch_id'])['rows'][0]['state'] == 'excluded'
    assert len(reopened.search(workspace, 'Immutable')) == 2
    assert hashes == {path.name: sha256(path.read_bytes()).hexdigest() for path in root.iterdir()}


def test_cancelled_successor_does_not_change_parent_or_publish_child(library, tmp_path, monkeypatch):
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    before = journal(app, first['batch_id']).read_bytes()
    original, stopped = batch_scan.planned_snapshot, False
    def stop_after_snapshot(path):
        nonlocal stopped
        result = original(path)
        stopped = True
        return result
    with monkeypatch.context() as patch:
        patch.setattr(batch_scan, 'planned_snapshot', stop_after_snapshot)
        with pytest.raises(InterruptedError):
            app.continue_import_scan(workspace, first['batch_id'], cancelled=lambda: stopped)
    assert journal(app, first['batch_id']).read_bytes() == before
    assert not journal(app, successor_id(first['batch_id'], first['scan']['next_cursor'])).exists()
    second = app.continue_import_scan(workspace, first['batch_id'])
    assert second['rows'][0]['file'] == '000001.txt'


def test_crash_after_child_publication_recovers_without_rescanning_or_replacing(library, tmp_path, monkeypatch):
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    original = ImportBatchStore._store
    def fail_parent(self, record, **options):
        if record['batch_id'] == first['batch_id'] and record['scan']['next_batch'] is not None:
            raise OSError('injected parent-link interruption')
        return original(self, record, **options)
    with monkeypatch.context() as patch:
        patch.setattr(ImportBatchStore, '_store', fail_parent)
        with pytest.raises(OSError, match='parent-link'):
            app.continue_import_scan(workspace, first['batch_id'])
    child_id = successor_id(first['batch_id'], first['scan']['next_cursor'])
    before = journal(app, child_id).read_bytes()
    monkeypatch.setattr(batch_scan, 'scan_paths_page', lambda *a, **k: pytest.fail('Saved successor was rescanned.'))
    second = LibraryApp(app.root).continue_import_scan(workspace, first['batch_id'])
    assert second['batch_id'] == child_id and journal(app, child_id).read_bytes() == before
    assert app.import_batch(workspace, first['batch_id'])['scan']['next_batch'] == child_id


def test_post_move_sync_failure_is_retried_before_child_acknowledgement(library, tmp_path, monkeypatch):
    from carr_library.audio import jobs
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    child_id = successor_id(first['batch_id'], first['scan']['next_cursor'])
    original = jobs._sync_directory
    def fail(path):
        if Path(path).name == 'import-batches':
            raise OSError('injected directory durability failure')
        return original(path)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, '_sync_directory', fail)
        with pytest.raises(OSError, match='durability'):
            app.continue_import_scan(workspace, first['batch_id'])
        assert journal(app, child_id).exists()
        saved = journal(app, child_id).read_bytes()
        with pytest.raises(OSError, match='durability'):
            app.continue_import_scan(workspace, first['batch_id'])
        assert app.import_batch(workspace, first['batch_id'])['scan']['next_batch'] is None
    second = app.continue_import_scan(workspace, first['batch_id'])
    assert second['batch_id'] == child_id and journal(app, child_id).read_bytes() == saved


def test_foreign_successor_is_never_overwritten_or_adopted(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    child_id = successor_id(first['batch_id'], first['scan']['next_cursor'])
    foreign = deepcopy(first)
    foreign['batch_id'] = child_id
    journal(app, child_id).write_text(json.dumps(foreign), encoding='utf-8')
    original = journal(app, child_id).read_bytes()
    with pytest.raises(ValueError, match='predecessor'):
        app.continue_import_scan(workspace, first['batch_id'])
    assert journal(app, child_id).read_bytes() == original
    assert app.import_batch(workspace, first['batch_id'])['scan']['next_batch'] is None


def test_changed_folder_cannot_advance_or_change_saved_page(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    before = journal(app, first['batch_id']).read_bytes()
    root.rename(tmp_path / 'moved original folder')
    root.mkdir()
    (root / '000000.txt').write_text('Replacement original', encoding='utf-8')
    with pytest.raises(ValueError, match='folder changed'):
        app.continue_import_scan(workspace, first['batch_id'])
    assert journal(app, first['batch_id']).read_bytes() == before


def test_ordinary_legacy_plan_remains_version_one(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 1)
    legacy = app.plan_import_batch(workspace, [root])
    assert legacy['version'] == 1 and 'scan' not in legacy
    with pytest.raises(ValueError, match='no scan continuation'):
        app.continue_import_scan(workspace, legacy['batch_id'])


def test_current_real_batch_lock_refuses_a_second_scan_owner(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    before = journal(app, first['batch_id']).read_bytes()
    with ImportBatchStore(app)._lock(first['batch_id']):
        with pytest.raises(ValueError, match='already running'):
            app.continue_import_scan(workspace, first['batch_id'])
    assert journal(app, first['batch_id']).read_bytes() == before


def test_chain_relations_reject_missing_and_foreign_pages(library, tmp_path):
    app, workspace = library
    root = sources(tmp_path, 3)
    first = app.plan_import_batch_page(workspace, [root], file_budget=1)
    second = app.continue_import_scan(workspace, first['batch_id'])
    first = app.import_batch(workspace, first['batch_id'])
    valid = {first['batch_id']: first, second['batch_id']: second}
    validate_scan_relations(valid)
    with pytest.raises(ValueError, match='successor is missing'):
        validate_scan_relations({first['batch_id']: first})
    broken = deepcopy(valid)
    broken[second['batch_id']]['scan']['roots'] = [str(tmp_path / 'different')]
    with pytest.raises(ValueError):
        validate_scan_relations(broken)


def test_metadata_budget_failure_can_retry_same_cursor_with_smaller_page(library, tmp_path, monkeypatch):
    from carr_library.ingest import scan_pages
    app, workspace = library
    root = sources(tmp_path, 8)
    first = app.plan_import_batch_page(workspace, [root], file_budget=3)
    before = journal(app, first['batch_id']).read_bytes()
    one = len(scan_pages._canonical(batch_scan._row(root / '000003.txt', root,
        'queued', 'Ready to import.', dict(sha256='a' * 64, mime='text/plain', size=19))))
    with monkeypatch.context() as patch:
        patch.setattr(scan_pages, 'MAX_METADATA_BYTES', one + 10)
        with pytest.raises(OverflowError, match='metadata budget'):
            app.continue_import_scan(workspace, first['batch_id'])
        assert journal(app, first['batch_id']).read_bytes() == before
        child = app.continue_import_scan(workspace, first['batch_id'], file_budget=1)
    assert child['total'] == 1 and child['scan']['file_budget'] == 1
    assert child['rows'][0]['file'] == '000003.txt'
