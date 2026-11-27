"""V5 integration: real queued imports, exact legacy shapes and crash recovery."""
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from uuid import uuid4
from threading import Event

import pytest

from carr_library import library_backup, pending_backup
from carr_library.application import LibraryApp
from carr_library.extract.document_formats import MIME_SUFFIXES
from carr_library.extract.page_ocr import Config
from carr_library.ingest.batch_scan import planned_snapshot
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.worker import load_request, prepare_request, record_progress
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def _files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def _seal(folder, manifest):
    raw = (canonical(manifest) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _refresh(folder, manifest, name):
    raw = (folder / 'pending' / name).read_bytes()
    entry = next(entry for entry in manifest['pending']['files'] if entry['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


def _prepare(app, workspace, original):
    metadata = dict(title='Queued source', authors=['Exact author'], language=None, edition=None,
                    volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    return prepare_request(app.root, workspace, original, metadata, Config(),
                           expected_manifest=planned_snapshot(original).manifest())


@lru_cache(maxsize=2)
def _fixtures(name):
    # Reuse the original synthetic parser fixtures; no production parser is mocked.
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'extract' / name))


def _document(suffix):
    if suffix in ('.odt', '.odp', '.ods', '.pptx', '.xlsx', '.fb2', '.fb3'):
        make = _fixtures('test_document_formats_xml.py')
        if suffix in ('.odt', '.odp', '.ods'):
            return make['odf'](suffix)
        if suffix in ('.pptx', '.xlsx'):
            return make['office'](suffix)
        return make[suffix[1:]]()
    if suffix == '.epub':
        return _fixtures('test_epub_ordered.py')['epub']()
    if suffix == '.docx':
        from docx import Document
        document, output = Document(), BytesIO()
        document.add_paragraph('Queued DOCX original survives relocation.')
        document.save(output)
        return output.getvalue()
    if suffix == '.pdf':
        from reportlab.pdfgen.canvas import Canvas
        output = BytesIO()
        canvas = Canvas(output, invariant=1, pageCompression=0)
        canvas.drawString(50, 700, 'Queued PDF original survives relocation.')
        canvas.save()
        return output.getvalue()
    return {
        '.txt': b'Queued text survives relocation.',
        '.html': b'<!doctype html><html><body><p>Queued HTML original.</p></body></html>',
        '.md': b'# Queued source\n\nOriginal **literal** notation.\n',
        '.eml': b'From: fixture@example.invalid\r\nSubject: Queued original\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nQueued email original.\r\n',
    }[suffix]


@pytest.mark.parametrize('mime,suffix', list(MIME_SUFFIXES.items()), ids=list(MIME_SUFFIXES.values()))
def test_each_real_supported_prepared_document_survives_lost_external_source(tmp_path, mime, suffix):
    app = LibraryApp(tmp_path / 'Бібліотека')
    workspace = app.create_workspace('Queued documents')
    original = tmp_path / ('Розділ.v1' + suffix)
    raw = _document(suffix)
    original.write_bytes(raw)
    before = _prepare(app, workspace, original)
    assert before['expected_manifest']['mime'] == mime
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    original.unlink()
    backup_bytes = _files(folder)
    manifest = verify_backup(folder, checksum)
    assert manifest['version'] == 5 and len(manifest['pending']['assets']) == 1
    restored = LibraryApp(restore_backup(folder, tmp_path / 'Відновлена', checksum))
    after = load_request(restored.root, before['request_id'])
    source = restored.assets_dir / (sha256(raw).hexdigest() + suffix)
    assert source.read_bytes() == raw
    assert after == dict(before, data_dir=str(restored.root), source=str(source))
    result = restored.resume_import(workspace, before['request_id'])
    assert result['state'] == 'complete' and result['edition'] == before['metadata']['edition_id']
    assert ReadingDesk(restored, workspace, result['edition']).page(1).text.strip()
    assert _files(folder) == backup_bytes
    # A completed request can be backed up again without losing its original identity.
    second = tmp_path / 'after-completion'
    verify_backup(second, create_backup(restored, second))


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Backup boundary')
    original = tmp_path / 'queued.txt'
    original.write_bytes(b'Queued exact bytes.\fSecond exact page.')
    request = _prepare(app, workspace, original)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    return app, workspace, original, request, folder, checksum


@pytest.mark.parametrize('shape', ['sidecars', 'state-pending'])
def test_both_exact_v4_shapes_remain_readable_without_rewriting(tmp_path, shape):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Legacy compatibility')
    app.add_text(workspace, 'Original', 'Exact source.', edition_id='edition')
    if shape == 'sidecars':
        page = ReadingDesk(app, workspace, 'edition').page(1)
        ReadingState(app.root).remember(page.location)
    else:
        original = tmp_path / 'queued.txt'
        original.write_bytes(b'Queued canonical V4 original.')
        request = _prepare(app, workspace, original)
    folder = tmp_path / 'legacy'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['version'] = 4
    if shape == 'sidecars':
        del manifest['pending']
        shutil.rmtree(folder / 'pending')
    else:
        assert manifest.pop('sidecars') == []
        manifest['state'] = []
        (folder / 'state').mkdir()
        original.unlink()
    checksum = _seal(folder, manifest)
    before = _files(folder)
    assert verify_backup(folder, checksum) == manifest
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', checksum))
    assert restored.search(workspace, 'Exact')[0].quote == 'Exact'
    if shape == 'sidecars':
        assert ReadingState(restored.root).resume(workspace, 'edition') == page.location
    else:
        assert restored.resume_import(workspace, request['request_id'])['state'] == 'complete'
    assert _files(folder) == before


def test_legacy_v4_unknown_state_producer_fails_explicitly_before_storage(prepared, monkeypatch, tmp_path):
    _, _, _, _, folder, _ = prepared
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    del manifest['sidecars']
    manifest['version'] = 4
    raw = b'{"unreviewed_producer":"synthetic"}'
    name = 'assistant/job.json'
    target = folder / 'state' / name
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    manifest['state'] = [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())]
    checksum = _seal(folder, manifest)
    before = _files(folder)
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened writable storage'))
    with pytest.raises(ValueError, match='validated backup adapter'):
        restore_backup(folder, tmp_path / 'rejected', checksum)
    assert _files(folder) == before and not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('change', ['bytes', 'retarget', 'foreign-workspace', 'unlisted', 'hardlink'])
def test_shared_verify_and_restore_reject_pending_tampering(prepared, tmp_path, change):
    _, _, _, request, folder, checksum = prepared
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    name = 'import-requests/' + request['request_id'] + '.json'
    path = folder / 'pending' / name
    if change == 'bytes':
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'Queued source', b'Forged source'))
    elif change == 'retarget':
        manifest['pending']['sources'][0]['source'] = str(tmp_path / 'other.txt')
        checksum = _seal(folder, manifest)
    elif change == 'foreign-workspace':
        data = json.loads(path.read_bytes())
        data['workspace'] = 'foreign-workspace'
        path.write_bytes((canonical(data) + '\n').encode())
        _refresh(folder, manifest, name)
        checksum = _seal(folder, manifest)
    elif change == 'unlisted':
        (folder / 'pending' / 'unknown.json').write_bytes(b'{}')
    else:
        os.link(path, tmp_path / 'foreign-link')
    before = _files(folder)
    for operation in (lambda: verify_backup(folder, checksum),
                      lambda: restore_backup(folder, tmp_path / 'rejected', checksum)):
        with pytest.raises(ValueError):
            operation()
    assert _files(folder) == before and not (tmp_path / 'rejected').exists()


def test_completed_request_cannot_claim_another_selected_originals_publication(prepared, tmp_path):
    app, workspace, _, request, _, _ = prepared
    work, edition = uuid4().hex, uuid4().hex
    published = app.add_text(workspace, 'Different original', 'An unrelated original source.',
                             work_id=work, edition_id=edition)
    folder = tmp_path / 'with-publication'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    name = 'import-requests/' + request['request_id'] + '.json'
    path = folder / 'pending' / name
    data = json.loads(path.read_bytes())
    data['metadata'].update(work_id=work, edition_id=edition)
    data.update(state='complete', result=published)
    path.write_bytes((canonical(data) + '\n').encode())
    _refresh(folder, manifest, name)
    checksum = _seal(folder, manifest)
    before = _files(folder)
    for operation in (lambda: verify_backup(folder, checksum),
                      lambda: restore_backup(folder, tmp_path / 'rejected', checksum)):
        with pytest.raises(ValueError, match='source|publication'):
            operation()
    assert _files(folder) == before and not (tmp_path / 'rejected').exists()


def test_pdf_request_configuration_must_match_its_actual_checkpointed_job(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Exact PDF checkpoint settings')
    original = tmp_path / 'source.pdf'
    canvas = Canvas(str(original), invariant=1, pageCompression=0)
    for text in ('First native PDF page.', 'Second native PDF page.'):
        canvas.drawString(50, 700, text)
        canvas.showPage()
    canvas.save()
    metadata = dict(title='Exact PDF', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    config = Config(dpi=300)
    planned = planned_snapshot(original).manifest()
    request = prepare_request(app.root, workspace, original, metadata, config, expected_manifest=planned)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 2
    result = app._import_file(workspace, original, **metadata, config=config, cancelled=cancelled,
                              _request_id=request['request_id'], _expected_manifest=planned)
    assert result['state'] == 'cancelled' and result['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=result)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    name = 'import-requests/' + request['request_id'] + '.json'
    changed = json.loads((folder / 'pending' / name).read_bytes())
    changed['config']['dpi'] = 200
    raw = (canonical(changed) + '\n').encode()
    (folder / 'pending' / name).write_bytes(raw)
    (app.root / name).write_bytes(raw)
    _refresh(folder, manifest, name)
    checksum = _seal(folder, manifest)
    before = _files(folder)
    for operation in (lambda: create_backup(app, tmp_path / 'rejected'),
                      lambda: verify_backup(folder, checksum),
                      lambda: restore_backup(folder, tmp_path / 'rejected', checksum)):
        with pytest.raises(ValueError, match='configuration'):
            operation()
    assert _files(folder) == before and not (tmp_path / 'rejected').exists()


def test_pending_bytes_and_manifest_count_toward_complete_backup_budget(prepared, tmp_path):
    app, _, _, _, folder, _ = prepared
    total = sum(len(raw) for raw in _files(folder).values())
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'too-small', max_bytes=total - 1)
    assert not (tmp_path / 'too-small').exists()
    exact = tmp_path / 'exact'
    checksum = create_backup(app, exact, max_bytes=total)
    assert sum(len(raw) for raw in _files(exact).values()) == total
    verify_backup(exact, checksum)


def test_low_pending_budget_rejects_large_source_before_opening_its_bytes(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Bounded snapshot')
    original = tmp_path / 'large.txt'
    original.write_bytes(b'A' * (3 * 1024 * 1024))
    _prepare(app, workspace, original)
    monkeypatch.setattr(pending_backup, 'planned_snapshot',
                        lambda *a, **k: pytest.fail('read source beyond remaining backup budget'))
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'rejected', max_bytes=app.database.stat().st_size + 65536)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('operation', ['verify', 'restore'])
def test_cancellation_during_pending_verification_preserves_backup(prepared, tmp_path, monkeypatch, operation):
    _, _, _, _, folder, checksum = prepared
    before = _files(folder)
    stopped = False
    copy = pending_backup._copy
    def stop(source, target, entry, cancelled):
        nonlocal stopped
        copy(source, target, entry, cancelled)
        stopped = True
    monkeypatch.setattr(pending_backup, '_copy', stop)
    with pytest.raises(InterruptedError):
        if operation == 'verify':
            verify_backup(folder, checksum, cancelled=lambda: stopped)
        else:
            restore_backup(folder, tmp_path / 'rejected', checksum, cancelled=lambda: stopped)
    assert _files(folder) == before and not (tmp_path / 'rejected').exists()


def test_sqlite_cancellation_during_pending_schema_validation_is_normalized(prepared, monkeypatch, tmp_path):
    app, workspace, _, _, _, _ = prepared
    app.add_text(workspace, 'Journal source', 'Actual committed journal.')
    folder = tmp_path / 'with-journal'
    checksum = create_backup(app, folder)
    before = _files(folder)
    cancelled = False
    def slow_schema(db):
        nonlocal cancelled
        cancelled = True
        # Real SQLite work reaches the installed progress handler. The rest of
        # the shared verification path and the journal remain genuine.
        db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000) '
                   'SELECT sum(x) FROM n').fetchone()
        pytest.fail('SQLite ignored cancellation')
    monkeypatch.setattr(pending_backup, '_journal_schema', slow_schema)
    with pytest.raises(InterruptedError):
        verify_backup(folder, checksum, cancelled=lambda: cancelled)
    assert _files(folder) == before


def test_real_exit_after_v5_manifest_leaves_all_pending_files_independently_verifiable(prepared, tmp_path):
    app, workspace, original, request, _, _ = prepared
    destination = tmp_path / 'crash-backup'
    code = '''
import os,sys
from pathlib import Path
from carr_library import library_backup
from carr_library.application import LibraryApp
target=Path(sys.argv[2])
sync=library_backup._sync_directory
def stop(path):
    sync(path)
    if path==target and (target/'manifest.json').is_file():
        os._exit(74)
library_backup._sync_directory=stop
library_backup.create_backup(LibraryApp(sys.argv[1]),target)
'''
    source = Path(library_backup.__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(source), str(source.parent)))}
    result = subprocess.run([sys.executable, '-c', code, str(app.root), str(destination)],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 74, (result.stdout, result.stderr)
    original.unlink()
    before = _files(destination)
    assert all((destination / name).stat().st_nlink == 1 for name in before)
    checksum = sha256(before['manifest.json']).hexdigest()
    verify_backup(destination, checksum)
    restored = LibraryApp(restore_backup(destination, tmp_path / 'restored', checksum))
    assert restored.resume_import(workspace, request['request_id'])['state'] == 'complete'
    assert _files(destination) == before


def test_assistant_pause_during_final_backup_publication_resumes_same_snapshot(prepared, tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    from carr_library.library_assistant import LocalLibraryAssistant
    app, workspace, _, _, _, _ = prepared
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = LocalLibraryAssistant(app, workspace, output_root=outputs, allow_full_backup=True)
    job = service.start(service.plan('backup: preserved'), approved=True)
    stopped = Event()
    publish = backup_sidecars.publish
    def pause_at_publication(*args, **kwargs):
        stopped.set()
        return publish(*args, **kwargs)
    monkeypatch.setattr(backup_sidecars, 'publish', pause_at_publication)
    first = service.run(job['id'], cancelled=stopped.is_set)
    assert first['state'] == 'paused'
    target = outputs / 'preserved' / 'snapshot'
    before = _files(target)
    assert 'manifest.json' in before
    checksum = sha256(before['manifest.json']).hexdigest()
    verify_backup(target, checksum)
    monkeypatch.setattr(app, 'backup', lambda *a, **k: pytest.fail('republished an existing snapshot'))
    resumed = service.run(job['id'])
    assert resumed['state'] == 'complete', resumed
    assert resumed['steps'][0]['result']['manifest_sha256'] == checksum
    assert _files(target) == before
