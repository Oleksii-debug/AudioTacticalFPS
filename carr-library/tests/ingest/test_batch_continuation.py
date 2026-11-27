"""Real bounded scan pages over the existing private queue and import worker."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan, batches
from carr_library.library_backup import restore_backup


def sources(folder, count):
    folder.mkdir()
    for number in range(count):
        (folder / f'{number:05d}.txt').write_text(f'Unique bounded document {number}.\n', encoding='utf-8')
    return folder


def fixture(tmp_path, monkeypatch, count=5):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('A library')
    folder = sources(tmp_path / 'selected', count)
    return app, workspace, folder


def journal(app, batch):
    return app.root / 'import-batches' / (batch['batch_id'] + '.json')


def test_5001_actual_files_reopen_last_page_import_and_backup_boundary(tmp_path):
    # Real old-limit crossing; only the one tail row is rendered by the real
    # isolated worker. No 5,001 subprocess fixture and no large source payload.
    with TemporaryDirectory(dir=tmp_path) as temporary:
        root = Path(temporary)
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Large selected inventory')
        folder = sources(root / 'selected', 5001)
        first = app.plan_import_batch(workspace, [folder])
        assert len(first['rows']) == first['counts']['queued'] == 5000
        assert first['scan']['next_offset'] == 5000 and first['scan']['unscanned'] == []
        saved = journal(app, first).read_bytes()
        reopened = LibraryApp(app.root)
        last = reopened.next_import_scan_page(workspace, first['batch_id'])
        assert [row['file'] for row in last['rows']] == ['05000.txt']
        assert last['scan']['previous_batch_id'] == first['batch_id']
        assert last['scan']['next_offset'] is last['scan']['next_batch_id'] is None
        assert last['scan']['inventory_sha256'] == first['scan']['inventory_sha256']
        assert journal(app, first).read_bytes() == saved
        assert len({row['source'] for row in first['rows'] + last['rows']}) == 5001
        assert reopened.next_import_scan_page(workspace, first['batch_id']) == last
        final = reopened.run_import_batch(workspace, last['batch_id'])
        assert final['counts']['complete'] == 1
        hit, = reopened.search(workspace, 'document 5000')
        assert reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'document 5000'
        backup = root / 'snapshot'
        token = reopened.backup(backup)
        restored = LibraryApp(restore_backup(backup, root / 'restored', token))
        recovered_first = restored.import_batch(workspace, first['batch_id'])
        recovered_last = restored.import_batch(workspace, last['batch_id'])
        assert recovered_first['counts']['queued'] == 5000
        assert recovered_last['counts']['complete'] == 1
        assert recovered_first['scan']['roots'] == []
        assert recovered_first['scan']['next_offset'] is None
        assert recovered_first['scan']['next_batch_id'] is None
        assert all(Path(row['source']).is_relative_to(
            restored.root / 'pending-imports' / 'originals')
            for batch in (recovered_first, recovered_last) for row in batch['rows'])
        review = restored.review_import_recovery(workspace, 'batch', first['batch_id'])
        assert review['file_count'] == 5000 and review['previous_scan_incomplete'] is True
        assert review['already_approved'] is False
        with pytest.raises(PermissionError, match='Review restored imports'):
            restored.run_import_batch(workspace, first['batch_id'])
        hit, = restored.search(workspace, 'document 5000')
        assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'document 5000'
        assert journal(app, first).read_bytes() == saved


def test_three_saved_pages_keep_choices_ids_and_page_scoped_duplicate_checks(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    collection = app.create_collection(workspace, 'Original choice')
    first = app.plan_import_batch(workspace, [folder], collections=(collection,))
    second = app.next_import_scan_page(workspace, first['batch_id'])
    third = app.next_import_scan_page(workspace, second['batch_id'])
    assert [page['scan']['page'] for page in (first, second, third)] == [1, 2, 3]
    assert [len(page['rows']) for page in (first, second, third)] == [2, 2, 1]
    assert all(page['collections'] == [collection] for page in (first, second, third))
    final = app.run_import_batch(workspace, second['batch_id'])
    assert final['counts']['complete'] == 2
    retried = LibraryApp(app.root).next_import_scan_page(workspace, first['batch_id'])
    assert retried == final
    assert len(app.import_batches(workspace)) == 3
    with pytest.raises(ValueError, match='no continuation'):
        app.next_import_scan_page(workspace, third['batch_id'])


@pytest.mark.parametrize('change', ['append', 'delete', 'rename', 'new_file', 'replace', 'earlier_file'])
def test_changed_inventory_refuses_next_before_any_source_open(tmp_path, monkeypatch, change):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    saved = journal(app, first).read_bytes()
    path = folder / ('00000.txt' if change == 'earlier_file' else '00004.txt')
    if change == 'delete':
        path.unlink()
    elif change == 'rename':
        path.rename(folder / 'renamed.txt')
    elif change == 'new_file':
        (folder / 'new.txt').write_bytes(b'New source')
    elif change == 'replace':
        replacement = folder.parent / 'replacement.txt'
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
    else:
        path.write_bytes(path.read_bytes() + b'Changed')
    monkeypatch.setattr(batch_scan, 'planned_snapshot', lambda *a: pytest.fail('A stale cursor opened source bytes.'))
    with pytest.raises(ValueError, match='inventory changed'):
        app.next_import_scan_page(workspace, first['batch_id'])
    assert journal(app, first).read_bytes() == saved
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 1


@pytest.mark.parametrize('boundary', ['before', 'after_snapshot', 'before_journal'])
def test_cancelled_next_never_publishes_or_changes_previous_page(tmp_path, monkeypatch, boundary):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    saved = journal(app, first).read_bytes()
    stop = boundary == 'before'
    actual = batch_scan.planned_snapshot
    def snapshot(path):
        nonlocal stop
        result = actual(path)
        if boundary == 'after_snapshot':
            stop = True
        return result
    monkeypatch.setattr(batch_scan, 'planned_snapshot', snapshot)
    make = batches.ImportBatchStore._planned_record
    def planned(*args, **kwargs):
        nonlocal stop
        result = make(*args, **kwargs)
        if boundary == 'before_journal':
            stop = True
        return result
    monkeypatch.setattr(batches.ImportBatchStore, '_planned_record', planned)
    with pytest.raises(InterruptedError):
        app.next_import_scan_page(workspace, first['batch_id'], cancelled=lambda: stop)
    assert journal(app, first).read_bytes() == saved
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 1


def test_input_changed_during_next_discards_only_unpublished_page(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    actual = batch_scan.planned_snapshot
    def changing(path):
        result = actual(path)
        (folder / '00000.txt').write_bytes(b'An earlier source changed while the next page ran.')
        return result
    monkeypatch.setattr(batch_scan, 'planned_snapshot', changing)
    with pytest.raises(ValueError, match='inventory changed during'):
        app.next_import_scan_page(workspace, first['batch_id'])
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 1


@pytest.mark.parametrize('scope', ['other', 'archived', 'collection'])
def test_next_page_reauthorizes_before_reading_selected_files(tmp_path, monkeypatch, scope):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    collection = app.create_collection(workspace, 'Selected')
    first = app.plan_import_batch(workspace, [folder], collections=(collection,))
    if scope == 'other':
        workspace = app.create_workspace('A library')
    elif scope == 'archived':
        app.archive_workspace(workspace)
    else:
        with app.catalog_transaction(workspace) as catalog:
            catalog.archive(collection)
    monkeypatch.setattr(batch_scan, 'scan_page', lambda *a, **k: pytest.fail('Unauthorized scan.'))
    with pytest.raises(ValueError):
        app.next_import_scan_page(workspace, first['batch_id'])


def test_scope_revoked_at_scan_completion_prevents_new_journal(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    scan = batch_scan.scan_page
    def revoke(*args, **kwargs):
        result = scan(*args, **kwargs)
        app.archive_workspace(workspace)
        return result
    monkeypatch.setattr(batch_scan, 'scan_page', revoke)
    with pytest.raises(ValueError, match='archived'):
        app.next_import_scan_page(workspace, first['batch_id'])
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 1


@pytest.mark.parametrize('field,value', [('recursive', False), ('collections', []), ('offset', 1),
                                       ('roots', ['/changed']), ('next_offset', 1)])
def test_cursor_is_bound_to_original_options_and_position(tmp_path, monkeypatch, field, value):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    collection = app.create_collection(workspace, 'Selected')
    first = app.plan_import_batch(workspace, [folder], collections=(collection,))
    edited = deepcopy(first)
    (edited if field in ('recursive', 'collections') else edited['scan'])[field] = value
    journal(app, first).write_text(json.dumps(edited), encoding='utf-8')
    monkeypatch.setattr(batch_scan, 'scan_page', lambda *a, **k: pytest.fail('Malformed continuation inspected files.'))
    with pytest.raises(ValueError, match='cursor|continuation'):
        app.next_import_scan_page(workspace, first['batch_id'])


def test_concurrent_next_cannot_create_second_page_and_lost_reply_reuses_one(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    entered, release = Event(), Event()
    actual = batch_scan.planned_snapshot
    def paused(path):
        entered.set()
        if not release.wait(5):
            raise AssertionError('Test did not release source processing.')
        return actual(path)
    monkeypatch.setattr(batch_scan, 'planned_snapshot', paused)
    result, failures = [], []
    def work():
        try:
            result.append(app.next_import_scan_page(workspace, first['batch_id']))
        except BaseException as exc:
            failures.append(exc)
    thread = Thread(target=work)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match='already running'):
            LibraryApp(app.root).next_import_scan_page(workspace, first['batch_id'])
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not failures
    assert app.next_import_scan_page(workspace, first['batch_id']) == result[0]
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 2


def test_structural_entry_cap_reports_selected_scopes_without_partial_folder(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch, 3)
    later = sources(tmp_path / 'z-later', 1)
    monkeypatch.setattr(batch_scan, 'MAX_SCAN_ENTRIES', 2)
    monkeypatch.setattr(batch_scan, 'planned_snapshot', lambda *a: pytest.fail('Oversized folder prefix read.'))
    page = app.plan_import_batch(workspace, [folder, later])
    assert page['counts']['queued'] == 0 and page['counts']['rejected'] == 1
    assert page['scan']['unscanned'] == [str(folder), str(later)]
    assert page['scan']['next_offset'] is None
    from carr_library.ui.import_center import batch_report
    report = batch_report(page)
    assert 'Scan incomplete' in report and str(later) in report


def test_single_operation_deadline_and_journal_failure_preserve_previous_page(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    now = 0.0
    def clock():
        nonlocal now
        now += 1
        return now
    with monkeypatch.context() as patch:
        patch.setattr(batch_scan.time, 'monotonic', clock)
        with pytest.raises(TimeoutError):
            batch_scan.scan_page([folder], timeout=.1)
    saved = journal(app, first).read_bytes()
    replace = batches.os.replace
    def fail(source, destination):
        if Path(destination).name == first['scan']['next_batch_id'] + '.json':
            raise OSError('Injected precommit journal failure')
        return replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(batches.os, 'replace', fail)
        with pytest.raises(OSError, match='precommit'):
            app.next_import_scan_page(workspace, first['batch_id'])
    assert journal(app, first).read_bytes() == saved
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 1
    assert app.next_import_scan_page(workspace, first['batch_id'])['scan']['page'] == 2


def test_existing_v1_journal_remains_readable_and_importable(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch, 1)
    current = app.plan_import_batch(workspace, [folder])
    old = deepcopy(current)
    old['version'] = 1
    old.pop('scan')
    journal(app, current).write_text(json.dumps(old), encoding='utf-8')
    reopened = LibraryApp(app.root)
    assert reopened.import_batch(workspace, current['batch_id']) == old
    assert reopened.run_import_batch(workspace, current['batch_id'])['counts']['complete'] == 1
    with pytest.raises(ValueError, match='no continuation'):
        reopened.next_import_scan_page(workspace, current['batch_id'])


def test_rejected_tail_after_exact_file_limit_is_reachable_once(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch, 2)
    (folder / 'z-unsupported.bin').write_bytes(b'Explicitly unsupported')
    first = app.plan_import_batch(workspace, [folder])
    assert len(first['rows']) == 2 and first['scan']['next_offset'] == 2
    tail = app.next_import_scan_page(workspace, first['batch_id'])
    assert len(tail['rows']) == tail['counts']['rejected'] == 1
    assert tail['rows'][0]['file'] == 'z-unsupported.bin'
    assert tail['scan']['next_offset'] is None


def test_metadata_page_budget_continues_without_discarding_file(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch, 3)
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 5000)
    # Enough for the rejection reserve plus exactly one current short row.
    monkeypatch.setattr(batch_scan, 'MAX_PAGE_METADATA_BYTES', 451200)
    first = app.plan_import_batch(workspace, [folder])
    assert len(first['rows']) == 1 and first['scan']['next_offset'] == 1
    second = app.next_import_scan_page(workspace, first['batch_id'])
    third = app.next_import_scan_page(workspace, second['batch_id'])
    assert [page['rows'][0]['file'] for page in (first, second, third)] == ['00000.txt', '00001.txt', '00002.txt']
    assert third['scan']['next_offset'] is None


def test_changed_library_root_inside_cancellation_cannot_write_old_or_new_root(tmp_path, monkeypatch):
    app, workspace, folder = fixture(tmp_path, monkeypatch)
    first = app.plan_import_batch(workspace, [folder])
    store = batches.ImportBatchStore(app)
    other = LibraryApp(tmp_path / 'other-library')
    original_root = app.root
    def drift():
        app.root = other.root
        return False
    with pytest.raises(ValueError, match='folder changed'):
        store.next_scan_page(workspace, first['batch_id'], cancelled=drift)
    assert len(tuple((original_root / 'import-batches').glob('*.json'))) == 1
    assert not (other.root / 'import-batches').exists()


def test_path_byte_cap_is_explicit_and_never_opens_partial_source_scope(tmp_path, monkeypatch):
    folder = sources(tmp_path / 'selected', 4)
    monkeypatch.setattr(batch_scan, 'MAX_PATH_BYTES', len(os.fsencode(folder)) + 10)
    monkeypatch.setattr(batch_scan, 'planned_snapshot', lambda *a: pytest.fail('Over-budget source was opened.'))
    page = batch_scan.scan_page([folder])
    assert page.unscanned == (str(folder),)
    assert len(page.rows) == 1 and 'path-byte limit' in page.rows[0]['message']
