"""Actual isolated imports and durable queue recovery, without a new ingest path."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.ingest import batches, worker


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'private-library')


def source(tmp_path, name='source.txt', text='Unique railway manuscript quotation.'):
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


def identities(batch):
    return tuple(tuple(row[k] for k in ('row_id', 'work_id', 'edition_id', 'request_id'))
                 for row in batch['rows'])


def test_folder_plan_real_txt_import_search_copy_and_scope_safe_duplicates(app, tmp_path):
    folder = tmp_path / 'selected'
    folder.mkdir()
    first = source(folder, 'a.txt')
    source(folder, 'b.txt', first.read_text(encoding='utf-8'))
    source(folder, 'c.txt', 'Another railway document.')
    (folder / 'bad.pdf').write_bytes(b'not a PDF')
    workspace = app.create_workspace('Batch sources')
    collection = app.create_collection(workspace, 'Selected originals')
    planned = app.plan_import_batch(workspace, [folder], collections=(collection,))
    assert planned['counts']['queued'] == 2
    assert planned['counts']['duplicate'] == planned['counts']['rejected'] == 1
    assert app.browse(workspace) == () and not list(app.assets_dir.glob('*.txt'))
    journal = app.root / 'import-batches' / (planned['batch_id'] + '.json')
    assert str(first) in {row['source'] for row in json.loads(journal.read_text(encoding='utf-8'))['rows']}
    assert first.read_text(encoding='utf-8') not in journal.read_text(encoding='utf-8')
    if os.name != 'nt':
        assert journal.stat().st_mode & 0o777 == 0o600
        assert journal.parent.stat().st_mode & 0o777 == 0o700
    final = app.run_import_batch(workspace, planned['batch_id'])
    assert identities(final) == identities(planned)
    assert final['state'] == 'attention' and final['counts']['complete'] == 2
    complete = [row for row in final['rows'] if row['state'] == 'complete']
    assert all(row['result']['isolated'] and row['result']['verified'] for row in complete)
    assert {row['edition_id'] for row in complete} == {
        view.edition_id for view in app.browse(workspace, collection=collection)}
    hit, = app.search(workspace, 'manuscript quotation', collection=collection)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'manuscript quotation'
    assert app.plan_import_batch(workspace, [first])['rows'][0]['state'] == 'duplicate'
    other = app.create_workspace('Separate library')
    assert app.plan_import_batch(other, [first])['rows'][0]['state'] == 'queued'
    summary = app.import_batches(workspace)[-1]
    assert 'source' not in summary and 'data_dir' not in summary
    assert all(row['total'] >= 1 for row in app.import_batches(workspace))


def test_cancel_before_start_and_between_files_then_reopen_without_reimport(app, tmp_path):
    paths = [source(tmp_path, f'{i}.txt', f'Railway source number {i}.') for i in range(3)]
    workspace = app.create_workspace('Pause and resume')
    planned = app.plan_import_batch(workspace, paths)
    before = app.run_import_batch(workspace, planned['batch_id'], cancelled=lambda: True)
    assert before['state'] == 'paused' and before['counts']['queued'] == 3
    assert not list((app.root / 'import-requests').glob('*.json'))
    cancel = False
    def progress(batch):
        nonlocal cancel
        if batch['counts']['complete']:
            cancel = True
    partial = app.run_import_batch(workspace, planned['batch_id'],
                                   cancelled=lambda: cancel, progress=progress)
    assert partial['state'] == 'paused'
    assert partial['counts']['complete'] == 1 and partial['counts']['queued'] == 2
    completed = next(row for row in partial['rows'] if row['state'] == 'complete')
    Path(completed['source']).unlink()
    assert len(list((app.root / 'import-requests').glob('*.json'))) == 1
    reopened = LibraryApp(app.root)
    final = reopened.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 3
    assert identities(final) == identities(planned)
    assert len(reopened.browse(workspace)) == 3
    assert next(row for row in final['rows'] if row['row_id'] == completed['row_id']) == completed


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize('boundary', ['prepared', 'worker_reply', 'batch_reply'])
def test_crash_boundaries_keep_stable_request_work_edition_and_single_publication(
        app, tmp_path, monkeypatch, boundary):
    original = source(tmp_path)
    workspace = app.create_workspace('Crash recovery')
    planned = app.plan_import_batch(workspace, [original])
    actual = app.resume_import
    def interrupted(*args, **kwargs):
        if boundary != 'prepared':
            actual(*args, **kwargs)
            if boundary == 'worker_reply':
                record = worker.load_request(app.root, args[1])
                record['state'] = 'running'
                record.pop('result')
                worker._store(app.root, args[1], record)
        raise SimulatedCrash()
    monkeypatch.setattr(app, 'resume_import', interrupted)
    with pytest.raises(SimulatedCrash):
        app.run_import_batch(workspace, planned['batch_id'])
    interrupted_batch = json.loads((app.root / 'import-batches' /
        (planned['batch_id'] + '.json')).read_text(encoding='utf-8'))
    assert interrupted_batch['rows'][0]['state'] == 'running'
    if boundary != 'prepared':
        original.unlink()
    if boundary == 'batch_reply':
        def no_second_worker(*args, **kwargs):
            pytest.fail('A completed request must reconcile without invoking another worker.')
        monkeypatch.setattr(worker, '_invoke', no_second_worker)
    reopened = LibraryApp(app.root)
    final = reopened.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete' and identities(final) == identities(planned)
    with reopened.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_work').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    hit, = reopened.search(workspace, 'manuscript')
    assert reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'manuscript'


def test_changed_source_fails_before_retention_and_requires_explicit_retry(app, tmp_path):
    original = source(tmp_path)
    unchanged = original.read_bytes()
    workspace = app.create_workspace('Planned bytes')
    planned = app.plan_import_batch(workspace, [original])
    original.write_bytes(b'Different bytes after inspection.')
    failed = app.run_import_batch(workspace, planned['batch_id'])
    assert failed['rows'][0]['state'] == 'failed'
    assert 'changed after import planning' in failed['rows'][0]['message']
    assert app.browse(workspace) == () and not list(app.assets_dir.glob('*.txt'))
    request = worker.load_request(app.root, planned['rows'][0]['request_id'])
    assert request['expected_manifest'] == planned['rows'][0]['manifest']
    assert 'asset_sha256' not in request
    original.write_bytes(unchanged)
    assert app.run_import_batch(workspace, planned['batch_id'])['counts']['failed'] == 1
    assert app.browse(workspace) == ()
    final = app.run_import_batch(workspace, planned['batch_id'], retry_failed=True)
    assert final['state'] == 'complete' and identities(final) == identities(planned)
    assert app.assets.resolve(sha256(unchanged).hexdigest()).read_bytes() == unchanged


def test_expected_manifest_compares_the_same_snapshot_that_is_retained(app, tmp_path, monkeypatch):
    from carr_library.ingest import batch_scan
    original = source(tmp_path)
    frozen = snapshot(original.parent, original.name)
    workspace = app.create_workspace('One source snapshot')
    guarded_snapshot = batch_scan.planned_snapshot
    def read_then_change(path, *, cancelled):
        selected = guarded_snapshot(path, cancelled=cancelled)
        original.write_bytes(b'Replaced immediately after the intake read.')
        return selected
    with monkeypatch.context() as patch:
        patch.setattr(batch_scan, 'planned_snapshot', read_then_change)
        result = app._import_file(workspace, original, _expected_manifest=frozen.manifest())
    assert result['state'] == 'complete'
    assert app.assets.resolve(frozen.digest).read_bytes() == frozen.data
    assert app.copy(workspace, app.search(workspace, 'manuscript')[0].ref,
                    result['id'], 'quote') == 'manuscript'


def test_pending_plan_rechecks_duplicates_after_another_batch_finishes(app, tmp_path):
    original = source(tmp_path)
    workspace = app.create_workspace('Two plans')
    one = app.plan_import_batch(workspace, [original])
    two = app.plan_import_batch(workspace, [original])
    assert app.run_import_batch(workspace, one['batch_id'])['counts']['complete'] == 1
    final = app.run_import_batch(workspace, two['batch_id'])
    assert final['counts']['duplicate'] == 1
    assert len(app.browse(workspace)) == 1
    assert not (app.root / 'import-requests' / (two['rows'][0]['request_id'] + '.json')).exists()


def test_selection_only_changes_never_prepared_rows(app, tmp_path):
    original = source(tmp_path)
    workspace = app.create_workspace('Selection')
    planned = app.plan_import_batch(workspace, [original])
    row = planned['rows'][0]
    excluded = app.set_import_batch_row(workspace, planned['batch_id'], row['row_id'], included=False)
    assert excluded['counts']['excluded'] == 1
    assert app.run_import_batch(workspace, planned['batch_id'])['counts']['excluded'] == 1
    assert app.browse(workspace) == ()
    included = app.set_import_batch_row(workspace, planned['batch_id'], row['row_id'], included=True)
    assert included['counts']['queued'] == 1
    assert app.run_import_batch(workspace, planned['batch_id'])['counts']['complete'] == 1
    with pytest.raises(ValueError, match='never started'):
        app.set_import_batch_row(workspace, planned['batch_id'], row['row_id'], included=False)


def test_prepared_request_cannot_be_excluded_even_before_processing(app, tmp_path):
    original = source(tmp_path)
    workspace = app.create_workspace('Prepared selection')
    planned = app.plan_import_batch(workspace, [original])
    row = planned['rows'][0]
    worker.prepare_request(app.root, workspace, row['source'],
        batches.ImportBatchStore._metadata(row, planned), batches.Config(**planned['config']),
        request_id=row['request_id'], expected_manifest=row['manifest'])
    with pytest.raises(ValueError, match='already been prepared'):
        app.set_import_batch_row(workspace, planned['batch_id'], row['row_id'], included=False)
    assert app.browse(workspace) == ()


@pytest.mark.parametrize('first_action', ['exclude', 'source_changed'])
def test_unused_identical_alternate_can_be_included_after_primary_is_unavailable(
        app, tmp_path, first_action):
    first = source(tmp_path, 'a.txt')
    second = source(tmp_path, 'b.txt')
    workspace = app.create_workspace('Choose an identical copy')
    planned = app.plan_import_batch(workspace, [first, second])
    primary, alternate = planned['rows']
    assert primary['state'] == 'queued' and alternate['state'] == 'duplicate'
    if first_action == 'exclude':
        app.set_import_batch_row(workspace, planned['batch_id'], primary['row_id'], included=False)
    else:
        first.write_bytes(b'Changed primary after preflight.')
        assert app.run_import_batch(workspace, planned['batch_id'])['counts']['failed'] == 1
    selected = app.set_import_batch_row(workspace, planned['batch_id'], alternate['row_id'], included=True)
    assert selected['rows'][1]['state'] == 'queued'
    assert selected['rows'][1]['duplicate_of'] is None and selected['rows'][1]['message'] == ''
    final = app.run_import_batch(workspace, planned['batch_id'])
    assert final['counts']['complete'] == 1
    assert len(app.browse(workspace)) == 1
    existing = app.plan_import_batch(workspace, [second])
    assert existing['rows'][0]['duplicate_of'].startswith('existing:')
    with pytest.raises(ValueError, match='never started'):
        app.set_import_batch_row(workspace, existing['batch_id'],
                                  existing['rows'][0]['row_id'], included=True)


@pytest.mark.parametrize('replacement', ['hardlink', 'ancestor_link'])
def test_planned_source_link_policy_is_rechecked_before_worker_retention(app, tmp_path, replacement):
    folder = tmp_path / 'selected'
    folder.mkdir()
    original = source(folder)
    workspace = app.create_workspace('Post-plan source policy')
    planned = app.plan_import_batch(workspace, [original])
    if replacement == 'hardlink':
        try:
            os.link(original, tmp_path / 'second-name.txt')
        except OSError as exc:
            pytest.skip(f'Hard links unavailable: {exc}')
    else:
        relocated = tmp_path / 'relocated'
        folder.rename(relocated)
        try:
            folder.symlink_to(relocated, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f'Directory links unavailable: {exc}')
    final = app.run_import_batch(workspace, planned['batch_id'])
    assert final['counts']['failed'] == 1
    assert 'link' in final['rows'][0]['message'].lower()
    assert app.browse(workspace) == () and not list(app.assets_dir.glob('*.txt'))


def test_refresh_reconciles_external_resume_without_overwriting_live_batch(app, tmp_path):
    original = source(tmp_path)
    workspace = app.create_workspace('External resume')
    planned = app.plan_import_batch(workspace, [original])
    row = planned['rows'][0]
    worker.prepare_request(app.root, workspace, row['source'],
        batches.ImportBatchStore._metadata(row, planned), batches.Config(**planned['config']),
        request_id=row['request_id'], expected_manifest=row['manifest'])
    assert app.resume_import(workspace, row['request_id'])['state'] == 'complete'
    original.unlink()
    path = app.root / 'import-batches' / (planned['batch_id'] + '.json')
    before = path.read_bytes()
    with FileLock(path.with_suffix('.lock')).acquire(timeout=0):
        refreshed = app.import_batch(workspace, planned['batch_id'])
        assert refreshed['state'] == 'complete' and refreshed['counts']['complete'] == 1
        assert app.import_batches(workspace)[0]['counts']['complete'] == 1
    assert path.read_bytes() == before
    assert app.run_import_batch(workspace, planned['batch_id'])['state'] == 'complete'
    assert json.loads(path.read_text(encoding='utf-8'))['state'] == 'complete'


def test_workspace_and_collection_authorization_precedes_source_actions(app, tmp_path, monkeypatch):
    from carr_library.ingest import batch_scan
    original = source(tmp_path)
    workspace = app.create_workspace('Authorized')
    other = app.create_workspace('Other')
    collection = app.create_collection(workspace, 'Target collection')
    planned = app.plan_import_batch(workspace, [original], collections=(collection,))
    for action in (app.import_batch, app.run_import_batch):
        with pytest.raises(ValueError, match='another library'):
            action(other, planned['batch_id'])
    assert app.import_batches(other) == ()
    def no_scan(*args, **kwargs):
        pytest.fail('Unauthorized selections must not inspect sources.')
    monkeypatch.setattr(batch_scan, 'scan_paths', no_scan)
    with pytest.raises(ValueError, match='collection'):
        app.plan_import_batch(other, [original], collections=(collection,))
    app.archive_workspace(workspace)
    for action, args in ((app.import_batches, (workspace,)),
                         (app.import_batch, (workspace, planned['batch_id'])),
                         (app.run_import_batch, (workspace, planned['batch_id'])),
                         (app.plan_import_batch, (workspace, [original]))):
        with pytest.raises(ValueError, match='archived'):
            action(*args)
    app.restore_workspace(workspace)
    with app.catalog_transaction(workspace) as catalog:
        catalog.archive(collection)
    with pytest.raises(ValueError, match='archived collection'):
        app.run_import_batch(workspace, planned['batch_id'])
    assert not list((app.root / 'import-requests').glob('*.json'))


def test_batch_lock_atomic_failure_and_directory_binding(app, tmp_path, monkeypatch):
    original = source(tmp_path)
    workspace = app.create_workspace('Private queue')
    planned = app.plan_import_batch(workspace, [original])
    path = app.root / 'import-batches' / (planned['batch_id'] + '.json')
    initial = path.read_bytes()
    with FileLock(path.with_suffix('.lock')).acquire(timeout=0):
        with pytest.raises(ValueError, match='already running'):
            app.run_import_batch(workspace, planned['batch_id'])
    replace = os.replace
    def failed_replace(source_path, destination):
        if Path(destination) == path:
            raise OSError('Synthetic interrupted journal replacement')
        replace(source_path, destination)
    with monkeypatch.context() as patch:
        patch.setattr(batches.os, 'replace', failed_replace)
        with pytest.raises(OSError, match='interrupted journal'):
            app.run_import_batch(workspace, planned['batch_id'])
    assert path.read_bytes() == initial
    assert not list(path.parent.glob('*.tmp'))
    assert app.browse(workspace) == ()
    assert LibraryApp(app.root).run_import_batch(workspace, planned['batch_id'])['state'] == 'complete'
    another = LibraryApp(tmp_path / 'another-private-root')
    other = another.create_workspace('Another root')
    forged = deepcopy(planned)
    forged['workspace'] = other
    directory = another.root / 'import-batches'
    directory.mkdir(mode=0o700)
    (directory / path.name).write_text(json.dumps(forged), encoding='utf-8')
    with pytest.raises(ValueError, match='binding'):
        another.import_batch(other, planned['batch_id'])


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
def test_batch_native_formats_keep_existing_unverified_authority(app, tmp_path, kind):
    original = tmp_path / ('source.' + kind)
    if kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(original))
        canvas.drawString(72, 700, 'Native railway document')
        canvas.save()
    else:
        from docx import Document
        document = Document()
        document.add_paragraph('Native railway document')
        document.save(original)
    workspace = app.create_workspace('Native batch')
    planned = app.plan_import_batch(workspace, [original])
    final = app.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete'
    row = final['rows'][0]
    assert row['result']['isolated'] is True and row['result']['verified'] is False
    assert row['request_id'] == planned['rows'][0]['request_id']
    hit, = app.search(workspace, 'railway')
    assert not hit.verified
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_folder_epub_batch_keeps_planned_bytes_stable_identity_and_unverified_authority(app, tmp_path):
    from tests.e2e.test_epub_import import FIRST_TEXT, SECOND_TEXT, write_epub
    selected = tmp_path / 'selected-books'
    selected.mkdir()
    original = selected / 'Academic source.epub'
    original_bytes = write_epub(original)
    digest = sha256(original_bytes).hexdigest()
    workspace = app.create_workspace('EPUB batch library')
    collection = app.create_collection(workspace, 'Selected books')
    planned = app.plan_import_batch(workspace, [selected], collections=(collection,))
    assert planned['total'] == planned['counts']['queued'] == 1
    planned_row, = planned['rows']
    assert planned_row['file'] == 'Academic source.epub'
    assert planned_row['manifest'] == {
        'sha256': digest, 'mime': 'application/epub+zip', 'size': len(original_bytes)}
    final = app.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1
    assert identities(final) == identities(planned)
    row, = final['rows']
    assert row['result']['isolated'] is True and row['result']['verified'] is False
    assert row['result']['completed'] == 2
    request = worker.load_request(app.root, row['request_id'])
    assert request['expected_manifest'] == planned_row['manifest']
    assert request['metadata']['title'] == 'Academic source'
    book, = app.browse(workspace, collection=collection)
    assert book.edition_id == row['edition_id'] and book.asset == digest
    assert book.title == 'Academic source'
    retained = app.assets.resolve(digest)
    assert retained.suffix == '.epub' and retained.read_bytes() == original_bytes
    hits = app.search(workspace, 'Railway', collection=collection)
    assert len(hits) == 2
    assert all(not hit.verified and 'Segment' in hit.page and 'PDF page' not in hit.page
               for hit in hits)
    first, = app.search(workspace, 'Originmarker', collection=collection)
    preview = app.preview(workspace, first.ref, first.revision)
    assert FIRST_TEXT in preview and SECOND_TEXT not in preview
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, first.ref, first.revision, 'quote')
    original.unlink()
    reopened = LibraryApp(app.root)
    resumed = reopened.run_import_batch(workspace, planned['batch_id'])
    assert resumed['state'] == 'complete' and resumed['rows'] == final['rows']
    assert len(reopened.browse(workspace, collection=collection)) == 1
    assert reopened.assets.resolve(digest).read_bytes() == original_bytes


