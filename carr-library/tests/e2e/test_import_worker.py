"""Real native-parser subprocess cancellation, resume and recovery checks."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import monotonic

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.ocr_runtime import discover_ocr_runtime
from carr_library.ingest.worker import load_request, _store


def pdf(path, pages=1):
    from reportlab.pdfgen.canvas import Canvas
    canvas = Canvas(str(path))
    for index in range(pages):
        canvas.drawString(72, 700, f'Native railway source page {index + 1}')
        canvas.showPage()
    canvas.save()


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'library')


def test_native_parsers_run_in_a_separate_process_and_persist_fingerprint(app, tmp_path):
    source = tmp_path / 'Київ.pdf'
    pdf(source)
    workspace = app.create_workspace('Native sources')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['isolated'] is True
    assert result['verified'] is False
    request = load_request(app.root, result['request_id'])
    assert request['metadata']['title'] == 'Київ'
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        settings = json.loads(db.execute('SELECT config FROM job WHERE id=?',
                                        (result['id'],)).fetchone()[0])
    assert settings['runtime_identity']['packages']['pypdf']
    assert 'page_ocr.py' in settings['runtime_identity']['source_sha256']
    assert app.browse(workspace)[0].title == 'Київ'
    assert app.import_requests(workspace)[0]['state'] == 'complete'


def test_cancelled_native_request_can_resume_after_original_disappears(app, tmp_path):
    source = tmp_path / 'Архів.pdf'
    pdf(source, 12)
    workspace = app.create_workspace('Resume')
    checkpoints = 0

    def cancel_after_checkpoint():
        nonlocal checkpoints
        journal = app.root / 'imports.sqlite'
        if not journal.exists():
            return False
        try:
            with sqlite3.connect(journal, timeout=0.1) as db:
                checkpoints = db.execute('SELECT count(*) FROM checkpoint').fetchone()[0]
        except sqlite3.Error:
            return False
        return checkpoints >= 1

    result = app.import_file(workspace, source, cancelled=cancel_after_checkpoint)
    assert result['state'] == 'cancelled' and checkpoints >= 1
    assert app.browse(workspace) == ()
    record = load_request(app.root, result['request_id'])
    assert record['asset_sha256'] and record['job']
    source.unlink()
    resumed = app.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete' and resumed['completed'] == 12
    assert resumed['id'] == record['job']
    assert app.browse(workspace)[0].title == 'Архів'
    assert len(app.search(workspace, 'railway')) == 12


def test_worker_deadline_returns_without_hanging_the_caller(app, tmp_path):
    source = tmp_path / 'many-pages.pdf'
    pdf(source, 20)
    workspace = app.create_workspace('Deadline')
    start = monotonic()
    result = app.import_file(workspace, source, timeout=0.1)
    assert result['state'] == 'timed_out'
    assert monotonic() - start < 5
    resumed = app.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete'


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
def test_lost_worker_reply_after_commit_recovers_idempotently(app, tmp_path, kind):
    source = tmp_path / ('Original filename.' + kind)
    if kind == 'pdf':
        pdf(source)
    else:
        from docx import Document
        document = Document()
        document.add_paragraph('Native railway source')
        document.save(source)
    workspace = app.create_workspace('Commit boundary')
    first = app.import_file(workspace, source)
    assert first['state'] == 'complete'
    request = load_request(app.root, first['request_id'])
    # Exact durable state after DB commit but before the parent saved a reply.
    request['state'] = 'running'
    request.pop('result')
    _store(app.root, first['request_id'], request)
    source.unlink()
    resumed = app.resume_import(workspace, first['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == first['id']
    assert app.browse(workspace)[0].title == 'Original filename'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1


def _reply_test_source(tmp_path, kind):
    source = tmp_path / ('Reply boundary.' + kind)
    if kind == 'txt':
        source.write_text('Synthetic railway publication\n', encoding='utf-8')
    elif kind == 'pdf':
        pdf(source)
    elif kind == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph('Synthetic railway publication')
        document.save(source)
    else:
        # Reuse the real, lawful EPUB fixture; do not substitute the parser.
        import runpy
        fixture = runpy.run_path(str(Path(__file__).with_name('test_epub_import.py')))
        fixture['write_epub'](source)
    return source


def _intercept_worker_reply(monkeypatch, *, fail=False):
    """Keep actual parsing/publication; stop only the post-commit reply."""
    from carr_library.ingest import worker
    actual_popen = subprocess.Popen
    after = "raise RuntimeError('synthetic post-publication failure')" if fail else 'time.sleep(30)'
    inject = f'''
import time
from carr_library.application import LibraryApp
original_import = LibraryApp._import_file
def held_reply(self, *args, **kwargs):
    result = original_import(self, *args, **kwargs)
    if result.get('state') == 'complete':
        {after}
    return result
LibraryApp._import_file = held_reply
'''

    def child(command, *args, **kwargs):
        if '-c' in command:
            index = command.index('-c') + 1
            if "run_module('carr_library.ingest.worker'" in command[index]:
                command = list(command)
                command[index] = command[index].replace('runpy.run_module(',
                    'exec(' + repr(inject) + '); runpy.run_module(')
        return actual_popen(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', child)


@pytest.mark.parametrize('kind', ['pdf', 'docx', 'epub', 'txt'])
@pytest.mark.parametrize('stop', ['cancelled', 'timed_out'])
def test_stop_after_actual_child_commit_reports_durable_completion(app, tmp_path, monkeypatch, kind, stop):
    source = _reply_test_source(tmp_path, kind)
    workspace = app.create_workspace('Actual committed reply boundary')
    _intercept_worker_reply(monkeypatch)

    def committed():
        with app.read(workspace) as db:
            return bool(db.execute('SELECT count(*) FROM revision').fetchone()[0])

    arguments = dict(cancelled=committed if stop == 'cancelled' else lambda: False,
                     timeout=5 if stop == 'timed_out' else 20)
    if kind == 'txt':
        # TXT in a batch goes through the same prepared worker request.
        from carr_library.extract.page_ocr import Config
        from carr_library.ingest.worker import prepare_request
        request = prepare_request(app.root, workspace, source,
            dict(title=None, authors=(), language=None, edition=None, volume=None,
                 collections=(), work_id=None, edition_id=None), Config())
        result = app.resume_import(workspace, request['request_id'], **arguments)
    else:
        result = app.import_file(workspace, source, **arguments)
    assert result['state'] == 'complete', result
    assert result['verified'] is (kind == 'txt')
    assert 'before its worker stopped' in result['message']
    request = load_request(app.root, result['request_id'])
    assert request['state'] == 'complete' and request['result'] == result
    source.unlink()
    # A subsequent explicit resume reuses this checked result, with no new job.
    assert app.resume_import(workspace, result['request_id']) == result
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == result['completed']
        if kind == 'txt':
            # Exact source-text verification does not invent printed-page labels.
            assert tuple(tuple(page) for page in db.execute(
                'SELECT physical,printed,verified FROM page')) == ((None, None, 0),)


def test_true_child_error_after_publication_is_not_reclassified_as_success(app, tmp_path, monkeypatch):
    source = _reply_test_source(tmp_path, 'docx')
    workspace = app.create_workspace('Real error')
    _intercept_worker_reply(monkeypatch, fail=True)
    result = app.import_file(workspace, source)
    assert result['state'] == 'failed'
    assert 'synthetic post-publication failure' in result['message']
    assert len(app.browse(workspace)) == 1
    assert load_request(app.root, result['request_id'])['state'] == 'failed'


def test_cancellation_before_parsing_does_not_reconcile_unrelated_same_title(app, tmp_path):
    source = _reply_test_source(tmp_path, 'pdf')
    workspace = app.create_workspace('Separate requests')
    first = app.import_file(workspace, source)
    second = app.import_file(workspace, source, cancelled=lambda: True)
    assert first['state'] == 'complete' and second['state'] == 'cancelled'
    assert first['request_id'] != second['request_id']
    assert len(app.browse(workspace)) == 1


@pytest.mark.parametrize('change', ['asset', 'edition', 'work', 'title', 'config', 'collection', 'workspace'])
def test_reconciliation_rejects_mismatched_request_identity(app, tmp_path, change):
    from carr_library.ingest.publication import completed_request
    source = _reply_test_source(tmp_path, 'pdf')
    workspace = app.create_workspace('Exact identity')
    result = app.import_file(workspace, source)
    request = load_request(app.root, result['request_id'])
    request['state'] = 'running'
    request.pop('result')
    if change == 'asset':
        request['asset_sha256'] = '0' * 64
    elif change == 'config':
        request['config']['dpi'] = 200
    elif change == 'collection':
        collection = app.create_collection(workspace, 'Not requested by completed publication')
        request['metadata']['collections'] = [collection]
    elif change == 'workspace':
        request['workspace'] = app.create_workspace('Other workspace')
    else:
        field = {'edition': 'edition_id', 'work': 'work_id', 'title': 'title'}[change]
        request['metadata'][field] = 'different-identity'
    _store(app.root, result['request_id'], request)
    try:
        recovered = completed_request(app.root, workspace, result['request_id'])
    except ValueError:
        recovered = None
    assert recovered is None
    assert load_request(app.root, result['request_id']) == request
    assert len(app.browse(workspace)) == 1


def test_reconciliation_rejects_changed_retained_bytes(app, tmp_path):
    from carr_library.ingest.publication import completed_request
    source = _reply_test_source(tmp_path, 'docx')
    workspace = app.create_workspace('Retained source identity')
    result = app.import_file(workspace, source)
    request = load_request(app.root, result['request_id'])
    retained = app.assets.resolve(request['asset_sha256'])
    data = retained.read_bytes()
    retained.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(ValueError, match='digest changed'):
        completed_request(app.root, workspace, result['request_id'])


def test_reconciliation_budget_leaves_interrupted_request_resumable(app, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.ingest import publication, worker
    source = _reply_test_source(tmp_path, 'pdf')
    workspace = app.create_workspace('Bounded reconciliation')
    first = app.import_file(workspace, source)
    request = load_request(app.root, first['request_id'])
    request['state'] = 'running'
    request.pop('result')
    _store(app.root, first['request_id'], request)
    moments = iter([0.0, 2.0])
    monkeypatch.setattr(publication, 'time', SimpleNamespace(monotonic=lambda: next(moments)))
    monkeypatch.setattr(worker, '_invoke', lambda *args, **kwargs: {
        'state': 'cancelled', 'message': 'Import worker stopped.'})
    result = app.resume_import(workspace, first['request_id'])
    assert result['state'] == 'cancelled'
    assert 'resume to check it' in result['message']
    assert load_request(app.root, first['request_id'])['state'] == 'cancelled'
    assert len(app.browse(workspace)) == 1


def test_busy_writer_does_not_delay_stopped_request_reconciliation(app, tmp_path):
    from carr_library.ingest.publication import completed_request
    from carr_library.ingest.writer_guard import exclusive_writer
    source = _reply_test_source(tmp_path, 'pdf')
    workspace = app.create_workspace('Concurrent writer')
    result = app.import_file(workspace, source)
    with exclusive_writer(app.root):
        start = monotonic()
        with pytest.raises(ValueError, match='another library writer'):
            completed_request(app.root, workspace, result['request_id'])
        assert monotonic() - start < 1


@pytest.mark.parametrize('change', ['job', 'checkpoint'])
def test_text_worker_reconciliation_requires_its_exact_journal(app, tmp_path, change):
    from hashlib import sha256
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.pipeline import canonical
    from carr_library.ingest.publication import completed_request
    from carr_library.ingest.worker import prepare_request
    source = _reply_test_source(tmp_path, 'txt')
    workspace = app.create_workspace('Exact text journal')
    prepared = prepare_request(app.root, workspace, source,
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    result = app.resume_import(workspace, prepared['request_id'])
    assert result['state'] == 'complete' and result['verified'] is True
    request = load_request(app.root, result['request_id'])
    request['state'] = 'running'
    request.pop('result')
    if change == 'job':
        request['job'] = '0' * 64
    else:
        with sqlite3.connect(app.root / 'imports.sqlite') as journal:
            payload = json.loads(journal.execute(
                'SELECT payload FROM checkpoint WHERE job=? AND ordinal=1',
                (result['id'],)).fetchone()[0])
            payload['raw_text'] = 'A different source despite a correctly hashed checkpoint'
            raw = canonical(payload)
            journal.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=1',
                (raw, sha256(raw.encode('utf-8')).hexdigest(), result['id']))
    _store(app.root, result['request_id'], request)
    try:
        recovered = completed_request(app.root, workspace, result['request_id'])
    except ValueError:
        recovered = None
    assert recovered is None
    assert load_request(app.root, result['request_id']) == request
    assert len(app.browse(workspace)) == 1


def test_resume_rejects_changed_runtime_configuration(app, tmp_path):
    source = tmp_path / 'source.pdf'
    pdf(source)
    workspace = app.create_workspace('Runtime identity')
    result = app.import_file(workspace, source)
    request = load_request(app.root, result['request_id'])
    request['state'] = 'cancelled'
    request.pop('result')
    request['config']['dpi'] = 200
    _store(app.root, result['request_id'], request)
    resumed = app.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'failed'
    assert 'configuration changed' in resumed['message']
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_parallel_resume_cannot_overwrite_request_completion(app, tmp_path):
    from filelock import FileLock
    source = tmp_path / 'source.pdf'
    pdf(source)
    workspace = app.create_workspace('Parallel resume')
    result = app.import_file(workspace, source)
    path = app.root / 'import-requests' / (result['request_id'] + '.json')
    before = path.read_bytes()
    with FileLock(path.with_suffix('.lock')).acquire(timeout=0):
        with pytest.raises(ValueError, match='already running'):
            app.resume_import(workspace, result['request_id'])
    assert path.read_bytes() == before


def test_request_scope_and_malformed_pdf_fail_closed(app, tmp_path):
    source = tmp_path / 'invalid.pdf'
    source.write_bytes(b'%PDF-1.7\nmalformed')
    one = app.create_workspace('One')
    two = app.create_workspace('Two')
    result = app.import_file(one, source)
    assert result['state'] == 'failed' and result['isolated']
    assert app.browse(one) == ()
    assert app.import_requests(two) == ()
    with pytest.raises(ValueError, match='another library'):
        app.resume_import(two, result['request_id'])


def test_worker_rejects_unbounded_or_unknown_protocol_fields(tmp_path):
    from carr_library.ingest.worker import _invoke
    with pytest.raises(ValueError, match='too large'):
        _invoke({'source': 'x' * 64_001}, timeout=10)
    result = _invoke({'untrusted': True}, timeout=10)
    assert result['state'] == 'failed' and 'request schema' in result['message']


def scanned_pdf(path, texts):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.utils import ImageReader
    canvas = Canvas(str(path), pagesize=(612, 792))
    for text in texts:
        image = Image.new('RGB', (1200, 300), 'white')
        try:
            font = ImageFont.truetype('DejaVuSans.ttf', 72)
        except OSError:
            # The synthetic scan is ASCII; Pillow supplies this font on Windows.
            font = ImageFont.load_default(size=72)
        ImageDraw.Draw(image).text((30, 70), text, font=font, fill='black')
        canvas.drawImage(ImageReader(image), 50, 500, 500, 125)
        canvas.showPage()
    canvas.save()


@pytest.mark.skipif(discover_ocr_runtime() is None, reason='local OCR engine unavailable')
def test_scanned_pdf_requires_explicit_review_for_each_exact_page_before_publication(app, tmp_path):
    source = tmp_path / 'Scanned railway.pdf'
    scanned_pdf(source, ['Railway bridge', 'Railway station'])
    workspace = app.create_workspace('Scanned sources')
    result = app.import_file(workspace, source)
    assert result['state'] == 'quarantined' and result['completed'] == 1
    request = result['request_id']
    assert app.browse(workspace) == ()
    pending = app.pending_review(workspace, request)
    assert pending['raw_text'].strip() == 'Railway bridge'
    assert pending['physical'] == 1
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        original = db.execute('SELECT payload,digest FROM checkpoint WHERE job=? AND ordinal=1',
                              (result['id'],)).fetchone()
    payload = json.loads(original[0])
    assert payload['status'] == 'review_required'
    assert payload['provenance']['method'] != 'native'
    with pytest.raises(ValueError, match='must be confirmed'):
        app.confirm_import_review(workspace, request, 'Test reviewer',
                                  expected_checksum=pending['checksum'])
    with pytest.raises(ValueError, match='displayed review changed'):
        app.confirm_import_review(workspace, request, 'Test reviewer', confirmed=True,
                                  expected_checksum='0' * 64)
    other = app.create_workspace('Other library')
    with pytest.raises(ValueError, match='another library'):
        app.pending_review(other, request)
    app.confirm_import_review(workspace, request, 'Test reviewer', confirmed=True,
                              expected_checksum=pending['checksum'])
    resumed = app.resume_import(workspace, request)
    assert resumed['state'] == 'quarantined' and resumed['completed'] == 2
    assert app.browse(workspace) == ()
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        assert db.execute('SELECT payload,digest FROM checkpoint WHERE job=? AND ordinal=1',
                          (result['id'],)).fetchone() == original
    second = app.pending_review(workspace, request)
    assert second['physical'] == 2 and second['raw_text'].strip() == 'Railway station'
    app.confirm_import_review(workspace, request, 'Test reviewer', confirmed=True,
                              expected_checksum=second['checksum'])
    final = app.resume_import(workspace, request)
    assert final['state'] == 'complete' and final['completed'] == 2
    assert app.pending_review(workspace, request) is None
    hits = app.search(workspace, 'Railway')
    assert len(hits) == 2 and all(hit.verified for hit in hits)
    assert all(app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Railway' for hit in hits)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM source_page_review WHERE active=1').fetchone()[0] == 2
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        assert db.execute('SELECT payload,digest FROM checkpoint WHERE job=? AND ordinal=1',
                          (result['id'],)).fetchone() == original
    # A crash after the atomic publication may lose only the parent reply.
    record = load_request(app.root, request)
    record['state'] = 'running'
    record.pop('result')
    _store(app.root, request, record)
    source.unlink()
    recovered = LibraryApp(app.root).resume_import(workspace, request)
    assert recovered['state'] == 'complete' and recovered['id'] == result['id']
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM source_page_review WHERE active=1').fetchone()[0] == 2
    first = next(hit for hit in hits if hit.quote == 'Railway')
    request_record = load_request(app.root, request)
    app.revoke_pdf_review(workspace, request_record['metadata']['edition_id'], first.revision, 1)
    refreshed = app.search(workspace, 'bridge')
    assert len(refreshed) == 1 and not refreshed[0].verified
    with pytest.raises(ValueError):
        app.copy(workspace, refreshed[0].ref, refreshed[0].revision, 'quote')


@pytest.mark.skipif(discover_ocr_runtime() is None, reason='local OCR engine unavailable')
def test_reviewed_import_rejects_changed_approval_without_publishing(app, tmp_path):
    source = tmp_path / 'scan.pdf'
    scanned_pdf(source, ['Railway bridge'])
    workspace = app.create_workspace('Review binding')
    result = app.import_file(workspace, source)
    assert result['state'] == 'quarantined'
    request = result['request_id']
    pending = app.pending_review(workspace, request)
    app.confirm_import_review(workspace, request, 'Test reviewer', confirmed=True,
                              expected_checksum=pending['checksum'])
    path = app.root / 'import-requests' / 'reviews' / (request + '.json')
    approvals = json.loads(path.read_text())
    approvals['1']['raw_sha256'] = '0' * 64
    path.write_text(json.dumps(approvals), encoding='utf-8')
    resumed = app.resume_import(workspace, request)
    assert resumed['state'] == 'failed' and 'checkpoint identity' in resumed['message']
    assert app.browse(workspace) == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0

