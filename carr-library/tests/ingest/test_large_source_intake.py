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
        with sqlite3.connect(journal) as observer:
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
    races = runpy.run_path(str(Path(__file__).with_name('test_worker_publication_boundary.py')))
    processes = races['publication_barrier'](monkeypatch, marker)
    result = app.import_file(workspace, original['path'], timeout=600,
                             cancelled=marker.exists, **metadata())
    assert marker.is_file() and result['state'] == 'complete', result
    assert result['recovered_publication'] and result['worker_state'] == 'cancelled'
    assert len(processes) == 1 and processes[0].poll() is not None
    assert result['asset'] == original['sha256']
    if kind == 'txt':
        exact_text_pages(app, workspace, result['id'], original)
    assert file_digest(original['path']) == original['sha256']
    original['path'].unlink()
    assert LibraryApp(app.root).resume_import(workspace, result['request_id'], timeout=600) == result
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1


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
