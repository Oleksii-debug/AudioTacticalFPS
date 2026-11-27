"""Actual batch publication, restart, imports and typed-backup continuation."""
from copy import deepcopy
from pathlib import Path
import os

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan, batches
from carr_library.library_backup import create_backup, restore_backup


@pytest.fixture
def selected(tmp_path, monkeypatch):
    # Exercise the same continuation at a small resource budget; the scanner's
    # separate filesystem regressions cross the actual 5000/20000 boundaries.
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 1)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Continued import')
    folder = tmp_path / 'selected'
    folder.mkdir()
    for index in range(3):
        (folder / f'{index}.txt').write_text(f'Original page number {index}.', encoding='utf-8')
    return app, workspace, folder


def test_saved_scan_reopens_and_imports_each_page_through_actual_workers(selected):
    app, workspace, folder = selected
    first = app.plan_import_batch(workspace, [folder], paged=True)
    assert first['version'] == 2 and first['scan']['cursor'] is not None
    reopened = LibraryApp(app.root)
    current, ids, sources = first, set(), set()
    for _ in range(5):
        ids.add(current['batch_id'])
        sources.update(row['source'] for row in current['rows'])
        final = reopened.run_import_batch(workspace, current['batch_id'])
        assert all(row['state'] == 'complete' for row in final['rows'])
        if current['scan']['cursor'] is None:
            break
        current = reopened.continue_import_scan(workspace, current['batch_id'])
    else:
        pytest.fail('The finite scan did not finish.')
    assert len(sources) == 3 and len(ids) >= 3
    assert len(reopened.browse(workspace)) == 3
    assert len(reopened.search(workspace, 'Original')) == 3


def test_lost_next_page_reply_reuses_the_already_durable_batch(selected, monkeypatch):
    app, workspace, folder = selected
    first = app.plan_import_batch(workspace, [folder], paged=True)
    actual = batches.ImportBatchStore._store
    expected_next = first['scan']['next_batch_id']

    class LostReply(BaseException):
        pass

    def publish_then_crash(self, record, **options):
        actual(self, record, **options)
        if record['batch_id'] == expected_next:
            raise LostReply()

    with monkeypatch.context() as patch:
        patch.setattr(batches.ImportBatchStore, '_store', publish_then_crash)
        with pytest.raises(LostReply):
            app.continue_import_scan(workspace, first['batch_id'])
    committed = app.import_batch(workspace, expected_next)
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
                        lambda *_, **_options: pytest.fail('A committed scan page was read a second time.'))
    replay = LibraryApp(app.root).continue_import_scan(workspace, first['batch_id'])
    assert replay == committed
    assert len(list((app.root / 'import-batches').glob('*.json'))) == 2


def test_pending_pages_keep_existing_cross_batch_duplicate_publication_guard(selected):
    app, workspace, folder = selected
    for path in folder.iterdir():
        path.write_text('Identical original in separate pages.', encoding='utf-8')
    first = app.plan_import_batch(workspace, [folder], paged=True)
    second = app.continue_import_scan(workspace, first['batch_id'])
    assert first['rows'][0]['state'] == second['rows'][0]['state'] == 'queued'
    assert app.run_import_batch(workspace, first['batch_id'])['counts']['complete'] == 1
    assert app.run_import_batch(workspace, second['batch_id'])['counts']['duplicate'] == 1
    assert len(app.browse(workspace)) == 1


@pytest.mark.parametrize('kind', ['workspace', 'cancel', 'changed-directory'])
def test_denied_continuation_does_not_publish_another_batch(selected, monkeypatch, kind):
    app, workspace, folder = selected
    first = app.plan_import_batch(workspace, [folder], paged=True)
    before = {path.name: path.read_bytes() for path in (app.root / 'import-batches').glob('*.json')}
    if kind == 'workspace':
        workspace = app.create_workspace('Other library')
    elif kind == 'changed-directory':
        (folder / 'new.txt').write_text('New source', encoding='utf-8')
        info = folder.stat()
        os.utime(folder, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
                        lambda *_, **_options: pytest.fail('A rejected continuation reached a source.'))
    with pytest.raises((ValueError, InterruptedError)):
        app.continue_import_scan(workspace, first['batch_id'], cancelled=lambda: kind == 'cancel')
    assert {path.name: path.read_bytes() for path in (app.root / 'import-batches').glob('*.json')} == before


def test_backup_restores_scanned_bytes_but_never_reacquires_external_scan_scope(selected, tmp_path, monkeypatch):
    app, workspace, folder = selected
    first = app.plan_import_batch(workspace, [folder], paged=True)
    row = first['rows'][0]
    expected = Path(row['source']).read_bytes()
    digest = create_backup(app, tmp_path / 'backup')
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    saved = restored.import_batch(workspace, first['batch_id'])
    assert saved['scan']['review_required'] is True
    assert saved['scan']['unscanned_sources'] == 'external_not_copied'
    assert Path(saved['rows'][0]['source']).read_bytes() == expected
    with monkeypatch.context() as patch:
        patch.setattr(batch_scan, 'planned_snapshot',
                      lambda *_, **_options: pytest.fail('Restoration followed an unscanned external root.'))
        with pytest.raises(ValueError, match='not copied'):
            restored.continue_import_scan(workspace, first['batch_id'])
    complete = restored.run_import_batch(workspace, first['batch_id'])
    assert complete['counts']['complete'] == 1 and len(restored.browse(workspace)) == 1


def test_backup_refuses_malformed_cursor_even_when_scanned_rows_are_valid(selected, tmp_path):
    from carr_library.ingest.pipeline import canonical
    app, workspace, folder = selected
    first = app.plan_import_batch(workspace, [folder], paged=True)
    changed = deepcopy(first)
    changed['scan']['cursor']['stack'][0]['path'] = str(tmp_path)
    path = app.root / 'import-batches' / (first['batch_id'] + '.json')
    path.write_text(canonical(changed) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='scan|root'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_legacy_unpaged_batch_keeps_its_original_schema(selected):
    app, workspace, folder = selected
    old = app.plan_import_batch(workspace, [folder])
    assert old['version'] == 1 and 'scan' not in old
    with pytest.raises(ValueError, match='complete'):
        app.continue_import_scan(workspace, old['batch_id'])
