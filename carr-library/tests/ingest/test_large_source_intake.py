"""Actual 80 MiB TXT / 128 MiB PDF ingest, review, recovery and full backup.

These are synthetic source-integrity/resource fixtures, not model, OCR or human
reading-quality qualification. No source file or parser size is mocked.
"""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import MAX_BYTES, source_snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.ingest.pipeline import Ingest
from carr_library.library_backup import create_backup, restore_backup

helpers = runpy.run_path(str(Path(__file__).parent / 'large_source_fixtures.py'))
write_large_text = helpers['write_large_text']
write_large_pdf = helpers['write_large_pdf']
file_digest = helpers['file_digest']
observe_import_peak = helpers['observe_import_peak']
instrument_import_worker = helpers['instrument_import_worker']
MIB = helpers['MIB']


def metadata():
    return dict(title='Large retained source', authors=['Synthetic fixture'], language=None,
                edition=None, volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)


def exact_text_pages(app, workspace, job, original):
    with app.read(workspace) as db:
        rows = db.execute('SELECT ordinal,raw,digest FROM page WHERE revision=? ORDER BY ordinal', (job,))
        total, count = 0, 0
        for count, row in enumerate(rows, 1):
            encoded = row['raw'].encode('utf-8')
            assert row['ordinal'] == count
            assert row['digest'] == sha256(encoded).hexdigest() == original['segments'][count - 1][2]
            assert len(encoded) == original['segments'][count - 1][1]
            total += len(encoded)
        assert count == len(original['segments']) == 20
        assert total == original['size'] - count + 1 > MAX_BYTES
        assert original['meaningful_bytes'] > MAX_BYTES
        assert db.execute('SELECT count(*) FROM sealed WHERE revision=?', (job,)).fetchone()[0] == 1
        assert db.execute('SELECT sum(length(CAST(raw AS BLOB))) FROM indexed').fetchone()[0] == total


def checked_peak(path, processes):
    assert processes and all(process.poll() is not None for process in processes)
    observed = json.loads(path.read_text(encoding='utf-8'))
    assert observed['metric'] in ('PeakWorkingSetSize', 'ru_maxrss')
    # An actual worker metric, not a claim about all documents under the profile.
    assert 0 < observed['peak_bytes'] < 1536 * MIB, observed
    return observed


def test_large_txt_all_meaningful_bytes_publish_quote_and_restore_with_measured_worker(tmp_path, monkeypatch):
    original = write_large_text(tmp_path / 'Київ-original.txt')
    source = original['path']
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large TXT')
    peak = tmp_path / 'peak.json'
    with monkeypatch.context() as patch:
        processes = observe_import_peak(patch, peak)
        result = app.import_file(workspace, source, timeout=600, **metadata())
    assert result['state'] == 'complete' and result['isolated'] and result['verified'], result
    checked_peak(peak, processes)
    assert result['asset'] == original['sha256']
    exact_text_pages(app, workspace, result['id'], original)
    assert file_digest(source) == original['sha256']
    hit, = app.search_page(workspace, 'Largeanchor000', timeout=120).results
    assert hit.verified and app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Largeanchor000'
    retained = app.assets.resolve(original['sha256'])
    assert file_digest(retained) == original['sha256']

    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    assert any(item['sha256'] == original['sha256'] and item['size_bytes'] == original['size']
               for item in manifest['assets'])
    source.unlink()
    shutil.rmtree(app.root)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    exact_text_pages(restored, workspace, result['id'], original)
    restored_original = restored.assets.resolve(original['sha256'])
    assert file_digest(restored_original) == original['sha256']
    hit, = restored.search_page(workspace, 'Largeanchor000', timeout=120).results
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Largeanchor000'
    with restored_original.open('r+b') as stream:
        stream.write(b'Changed!')
    with pytest.raises(ValueError, match='digest|original|source'):
        restored.copy(workspace, hit.ref, hit.revision, 'quote')


def test_real_128_mib_pdf_uses_original_stream_and_explicit_rendered_review(tmp_path, monkeypatch):
    original = write_large_pdf(tmp_path / 'original.pdf')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large PDF')
    peak = tmp_path / 'peak.json'
    with monkeypatch.context() as patch:
        processes = observe_import_peak(patch, peak)
        result = app.import_file(workspace, original['path'], timeout=600, **metadata())
    assert result['state'] == 'complete' and result['isolated'] and result['verified'] is False, result
    checked_peak(peak, processes)
    assert result['asset'] == original['sha256'] and result['completed'] == 1
    hit, = app.search_page(workspace, 'railway', timeout=120).results
    assert hit.verified is False
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    # Explicit synthetic review tests the real isolated PDFium render path.
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic source reviewer', confirmed=True)
    reviewed, = app.search_page(workspace, 'railway', timeout=120).results
    assert reviewed.verified and app.copy(workspace, reviewed.ref, reviewed.revision, 'quote') == 'railway'
    assert file_digest(original['path']) == original['sha256']
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    original['path'].unlink()
    shutil.rmtree(app.root)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    assert file_digest(restored.assets.resolve(original['sha256'])) == original['sha256']
    reopened, = restored.search_page(workspace, 'railway', timeout=120).results
    assert reopened.verified and restored.copy(workspace, reopened.ref, reopened.revision, 'quote') == 'railway'


def test_large_partial_checkpoint_backup_restores_exact_job_after_originals_disappear(tmp_path):
    original = write_large_text(tmp_path / 'original.txt')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large pending TXT')
    manifest = source_snapshot(tmp_path, original['path'].name).manifest()
    fields = metadata()
    request = worker.prepare_request(app.root, workspace, original['path'], fields, Config(),
                                     expected_manifest=manifest)
    def cancelled():
        journal = app.root / 'imports.sqlite'
        if not journal.exists():
            return False
        with closing(sqlite3.connect(journal)) as observer:
            return observer.execute('SELECT count(*) FROM checkpoint').fetchone()[0] >= 1
    initial = app._import_file(workspace, original['path'], **fields,
        cancelled=cancelled, _request_id=request['request_id'], _expected_manifest=manifest)
    assert initial['state'] == 'cancelled' and initial['completed'] == 1
    assert app.browse(workspace) == ()
    worker.record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    original['path'].unlink()
    shutil.rmtree(app.root)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    rebound = worker.load_request(restored.root, request['request_id'])
    assert rebound['expected_manifest'] == manifest
    assert Path(rebound['source']).is_relative_to(restored.root)
    assert file_digest(rebound['source']) == original['sha256']
    final = restored.resume_import(workspace, request['request_id'], timeout=600)
    assert final['state'] == 'complete' and final['id'] == initial['id']
    exact_text_pages(restored, workspace, final['id'], original)


@pytest.mark.parametrize('kind', ['txt', 'pdf'])
def test_large_source_completed_before_real_child_cancel_is_recovered_without_republication(tmp_path, monkeypatch, kind):
    original = (write_large_text(tmp_path / 'original.txt') if kind == 'txt'
                else write_large_pdf(tmp_path / 'original.pdf'))
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large publication boundary')
    marker = tmp_path / 'committed'
    boundary_path = Path(__file__).resolve().parents[1] / 'e2e' / 'test_import_worker_publication.py'
    races = runpy.run_path(str(boundary_path))
    reconcile = worker._reconcile_publication
    observed = []

    def observed_reconciliation(*args, **kwargs):
        # Observe the current verifier, including its real one-second budget.
        # Do not replace a timeout with success or increase the stopping budget.
        assert kwargs['timeout'] == 1.0
        try:
            committed = reconcile(*args, **kwargs)
        except TimeoutError as error:
            observed.append(('pending', error))
            raise
        else:
            observed.append(('receipt', committed))
            return committed

    processes = []
    try:
        with monkeypatch.context() as patch:
            races['_barrier'](patch, marker, boundary='after')
            processes = instrument_import_worker(patch)
            patch.setattr(worker, '_reconcile_publication', observed_reconciliation)
            result = app.import_file(workspace, original['path'], timeout=600,
                                     cancelled=marker.exists, **metadata())
        assert marker.is_file(), result
        # Actual death is checked before the failure-only cleanup below.
        assert len(processes) == 1 and processes[0].poll() is not None
        assert len(observed) == 1, (result, observed)
        outcome, evidence = observed[0]
        if outcome == 'receipt':
            assert isinstance(evidence, dict) and evidence['publication_recovered'] is True
            assert result['state'] == 'complete' and result['publication_recovered'] is True, result
            assert result['worker_state'] == 'cancelled'
        else:
            assert outcome == 'pending' and isinstance(evidence, TimeoutError)
            assert result['state'] == 'cancelled' and not result.get('publication_recovered'), result
            assert 'Publication verification is pending' in result['message']
    finally:
        for process in processes:
            if process.poll() is None:
                worker._stop(process)

    request_id = result['request_id']
    saved = worker.load_request(app.root, request_id)
    candidate = saved['publication_candidate']
    assert candidate['attempt'] == saved['attempt']
    assert candidate['manifest'] == dict(sha256=original['sha256'],
        mime='text/plain' if kind == 'txt' else 'application/pdf', size=original['size'])
    revision = candidate['revision']
    with app.read(workspace) as db:
        receipts = [tuple(row) for row in db.execute(
            'SELECT revision,request_id,attempt,sha256 FROM import_publication')]
        assert receipts == [(revision, request_id, saved['attempt'], candidate['sha256'])]
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sealed WHERE revision=?', (revision,)).fetchone()[0] == 1
    if kind == 'txt':
        exact_text_pages(app, workspace, revision, original)
    assert file_digest(original['path']) == original['sha256']
    assert file_digest(app.assets.resolve(original['sha256'])) == original['sha256']

    # Explicit resume uses the actual retained original after the selected
    # external file disappears. A real child trap forbids another publication.
    original['path'].unlink()
    forbidden = tmp_path / 'unexpected-second-publication'
    code = (
        'from pathlib import Path\n'
        'from carr_library.application import _CatalogPublisher\n'
        'def _forbid_publication(*args, **kwargs):\n'
        f'    Path({str(forbidden)!r}).write_text("called", encoding="utf-8")\n'
        '    raise AssertionError("A committed large import must not publish again.")\n'
        '_CatalogPublisher.publish_revision = _forbid_publication\n')
    resumed_processes = []
    try:
        with monkeypatch.context() as patch:
            resumed_processes = instrument_import_worker(patch, code=code)
            final = LibraryApp(app.root).resume_import(workspace, request_id, timeout=600)
        assert final['state'] == 'complete' and final['id'] == revision, final
        assert final['asset'] == original['sha256']
        assert final['verified'] is (kind == 'txt')
        assert not forbidden.exists()
        assert all(process.poll() is not None for process in resumed_processes)
        if outcome == 'receipt':
            assert final == result and resumed_processes == []
        else:
            assert len(resumed_processes) == 1
    finally:
        for process in resumed_processes:
            if process.poll() is None:
                worker._stop(process)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert [tuple(row) for row in db.execute(
            'SELECT revision,request_id,attempt,sha256 FROM import_publication')] == receipts
    if kind == 'txt':
        exact_text_pages(app, workspace, revision, original)
    assert file_digest(app.assets.resolve(original['sha256'])) == original['sha256']


def test_cancelled_stream_setup_keeps_pending_job_retryable_without_publication(tmp_path):
    original = write_large_text(tmp_path / 'original.txt')
    from carr_library.storage.revisions import Storage
    from carr_library.ingest.retained_assets import RetainedAssets
    root = tmp_path / 'pipeline'
    root.mkdir()
    retained = root / 'originals'
    retained.mkdir()
    with closing(Storage(root / 'revisions.sqlite')) as storage:
        with closing(Ingest(root / 'imports.sqlite', storage)) as ingest:
            store = RetainedAssets(retained)
            job = store.start(ingest, 'A', 'E', tmp_path, original['path'].name)
            calls = 0
            def cancelled():
                nonlocal calls
                calls += 1
                return calls >= 4
            result = ingest.run('A', job, retained, original['sha256'] + '.txt', cancelled=cancelled)
            assert result['state'] == 'cancelled' and result['completed'] == 0
            assert storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
            assert file_digest(store.resolve(original['sha256'])) == original['sha256']
            ingest.retry('A', job)
            assert ingest.inspect('A', job)['state'] == 'pending'


def test_interrupted_publication_read_releases_lock_with_retained_traceback(tmp_path):
    from carr_library.ingest.publication import _read_database
    from carr_library.pending_backup import MAX_PAYLOAD

    source = tmp_path / 'three-pages.txt'
    source.write_text('First exact page.\fSecond exact page.\fThird exact page.', encoding='utf-8')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Interrupted receipt')
    # Small TXT is normally inline; this regression needs a real durable request.
    result = worker.run_request(app.root, workspace, source, metadata(), Config(),
                                timeout=60)
    assert result['state'] == 'complete' and result['completed'] == 3 and result['isolated'], result
    request = worker.load_request(app.root, result['request_id'])
    candidate = request['publication_candidate']
    assert candidate['revision'] == result['id']
    page_queries = []

    def observe_query(statement):
        if (statement.startswith('SELECT ordinal,physical,printed,verified,')
                and 'FROM page WHERE revision=' in statement):
            page_queries.append(statement)

    def interrupt_pages():
        if page_queries:
            raise TimeoutError('Synthetic stop during receipt page iteration.')

    # This small deterministic regression exercises the real read connection,
    # SQLite cursor and receipt verifier. The existing large-source cases retain
    # the real one-second reconciliation deadline and actual child termination.
    with pytest.raises(TimeoutError, match='Synthetic stop during receipt page iteration') as failure:
        with _read_database(app.database, lambda: False, MAX_PAYLOAD) as db:
            db.set_trace_callback(observe_query)
            try:
                worker._publication_state(db, request, candidate['revision'],
                                          check=interrupt_pages, max_payload=MAX_PAYLOAD)
            finally:
                db.set_trace_callback(None)
    assert len(page_queries) == 1
    traceback = failure.value.__traceback__
    while traceback is not None and traceback.tb_frame.f_code is not worker._publication_state.__code__:
        traceback = traceback.tb_next
    assert traceback is not None and 'pages' in traceback.tb_frame.f_locals

    # Keep the exception and verifier frame alive while a separate writer must
    # commit immediately. No garbage collection, busy retry or timeout increase
    # may be needed to release the interrupted reader's statement.
    with closing(sqlite3.connect(app.database, isolation_level=None, timeout=0)) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE workspace_meta SET description=? WHERE workspace=?',
                       ('Writer committed after interrupted receipt.', workspace))
        writer.commit()
    reopened = LibraryApp(app.root)
    with reopened.read(workspace) as db:
        assert db.execute('SELECT description FROM workspace_meta WHERE workspace=?',
                          (workspace,)).fetchone()[0] == 'Writer committed after interrupted receipt.'
        assert [row[0] for row in db.execute(
            'SELECT raw FROM page WHERE revision=? ORDER BY ordinal', (result['id'],))] == [
                'First exact page.', 'Second exact page.', 'Third exact page.']
        receipt, = [tuple(row) for row in db.execute(
            'SELECT revision,request_id,attempt,sha256 FROM import_publication')]
        assert receipt == (result['id'], result['request_id'], request['attempt'], candidate['sha256'])
    assert failure.value.__traceback__ is not None and traceback.tb_frame.f_locals['pages'] is not None
