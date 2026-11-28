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


def test_stopped_import_owns_interpreter_and_releases_writer_before_reconciliation(
        app, tmp_path, monkeypatch):
    """A real commit while the worker still owns its lock, including Windows venvs."""
    import filelock
    import json
    import os
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest import worker
    from carr_library.ingest.writer_guard import exclusive_writer

    source = tmp_path / 'owned-interpreter.txt'
    source.write_text('Owned interpreter publication.\n', encoding='utf-8')
    workspace = app.create_workspace('Actual interpreter ownership')
    marker = tmp_path / 'committed-interpreter.json'
    popen = subprocess.Popen
    processes = []
    injection = f'''
import json, os, sys, time
from pathlib import Path
import filelock
from carr_library.application import _CatalogPublisher
from carr_library.ingest import worker as imported_worker
original_publish = _CatalogPublisher.publish_revision
def committed_with_lock(self, *args, **kwargs):
    result = original_publish(self, *args, **kwargs)
    marker = Path({str(marker)!r})
    temporary = marker.with_suffix('.pending')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(dict(pid=os.getpid(), executable=sys.executable, prefix=sys.prefix,
                       dependency=filelock.__file__, module=imported_worker.__file__,
                       isolated=sys.flags.isolated, bytecode=sys.dont_write_bytecode), stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker)
    time.sleep(30)
    return result
_CatalogPublisher.publish_revision = committed_with_lock
'''

    def launch(command, *args, **kwargs):
        command = list(command)
        owned = ('-c' in command and "run_module('carr_library.ingest.worker'" in command[command.index('-c') + 1])
        if owned:
            index = command.index('-c') + 1
            command[index] = command[index].replace('runpy.run_module(',
                'exec(' + repr(injection) + '); runpy.run_module(')
        process = popen(command, *args, **kwargs)
        if owned:
            processes.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    if os.name == 'nt' and sys.prefix != sys.base_prefix:
        # CPython's internal executable override must not replace the selected
        # venv when its redirector is bypassed. No arbitrary child environment.
        monkeypatch.setenv('PYTHONEXECUTABLE', str(tmp_path / 'different-python.exe'))
        monkeypatch.setenv('__PYVENV_LAUNCHER__', str(tmp_path / 'different-venv' / 'python.exe'))
    request = worker.prepare_request(app.root, workspace, source,
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    result = app.resume_import(workspace, request['request_id'], cancelled=marker.exists, timeout=20)
    assert marker.exists(), result
    runtime = json.loads(marker.read_text(encoding='utf-8'))
    process, = processes
    assert runtime['pid'] == process.pid, (runtime, process.pid)
    assert process.poll() is not None
    assert os.path.normcase(runtime['executable']) == os.path.normcase(sys.executable)
    assert os.path.normcase(runtime['prefix']) == os.path.normcase(sys.prefix)
    assert Path(runtime['dependency']).resolve() == Path(filelock.__file__).resolve()
    assert Path(runtime['module']).resolve() == Path(worker.__file__).resolve()
    assert runtime['isolated'] == 1 and runtime['bytecode'] is True
    assert result['state'] == 'complete' and result['publication_recovered'] is True, result
    with exclusive_writer(app.root, timeout=0):
        pass
    with app.read(workspace) as db:
        receipt = db.execute('SELECT request_id,attempt FROM import_publication').fetchone()
        assert receipt[0] == request['request_id']
        saved = worker.load_request(app.root, request['request_id'])
        assert receipt[1] == saved['attempt'] and saved['result'] == result
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
