"""Direct document imports retain exact pending identities without a TXT/PDF journal."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.worker import _invoke, load_request, prepare_request, record_progress, run_request
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.library_backup import create_backup, restore_backup
from carr_library.pending_backup import snapshot_pending, restore_pending
from carr_library.storage.revisions import Storage


TEXT = 'Immutable direct source. Український текст.'


def _source(tmp_path, suffix):
    source = tmp_path / ('direct-source' + suffix)
    if suffix == '.html':
        source.write_text('<!doctype html><html><body><p>' + TEXT + '</p></body></html>', encoding='utf-8')
    elif suffix == '.docx':
        from docx import Document
        document = Document()
        document.add_paragraph(TEXT)
        document.save(source)
    else:
        assert suffix == '.epub'
        with ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml',
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                '<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/>'
                '</rootfiles></container>')
            archive.writestr('book.opf',
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="id">urn:carr:pending-backup</dc:identifier>'
                '<dc:title>Direct import</dc:title><dc:language>uk</dc:language>'
                '<meta property="dcterms:modified">2026-11-27T00:00:00Z</meta></metadata>'
                '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
                '</manifest><spine><itemref idref="chapter"/></spine></package>')
            archive.writestr('chapter.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
                '<body><p>' + TEXT + '</p></body></html>')
    return source


def _metadata():
    return dict(title='Direct import', authors=[], language=None, edition=None, volume=None,
                collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)


def _stopped(app, workspace, source, monkeypatch):
    metadata = _metadata()
    planned = snapshot(source.parent, source.name).manifest()
    request = prepare_request(app.root, workspace, source, metadata, Config(), expected_manifest=planned)

    def stop_before_publication(*args, **kwargs):
        raise InterruptedError('Stopped after extraction and before publication.')

    with monkeypatch.context() as patch:
        patch.setattr(_CatalogPublisher, 'publish_revision', stop_before_publication)
        with pytest.raises(InterruptedError, match='before publication'):
            app._import_file(workspace, source, **metadata, _request_id=request['request_id'],
                             _expected_manifest=planned)
    record_progress(app.root, request['request_id'], state='cancelled')
    request = load_request(app.root, request['request_id'])
    assert request.get('job') and request.get('asset_sha256')
    assert not (app.root / 'imports.sqlite').exists()
    with app.read(workspace) as db:
        assert not db.execute('SELECT 1 FROM revision WHERE id=?', (request['job'],)).fetchone()
    return request


def _mutate(path, change):
    value = json.loads(path.read_bytes())
    change(value)
    path.write_text(canonical(value) + '\n', encoding='utf-8')


@pytest.mark.parametrize('suffix,state', [
    ('.docx', 'complete'), ('.epub', 'complete'), ('.html', 'complete'),
    ('.html', 'running'), ('.html', 'cancelled')])
def test_sealed_direct_import_round_trip_keeps_exact_job_and_never_reextracts(
        tmp_path, monkeypatch, suffix, state):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Direct imports')
    source = _source(tmp_path, suffix)
    original = source.read_bytes()
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['verified'] is False
    record_progress(app.root, result['request_id'], state=state)
    request = load_request(app.root, result['request_id'])
    assert not (app.root / 'imports.sqlite').exists()
    source.unlink()

    def no_reextract(*args, **kwargs):
        raise AssertionError('A sealed import must use its stored historical extraction identity.')

    with monkeypatch.context() as patch:
        from carr_library.ingest import worker
        patch.setattr(worker, '_invoke', no_reextract)
        backup = tmp_path / 'backup'
        checksum = create_backup(app, backup)
        root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(root)
    after = load_request(root, request['request_id'])
    assert after['job'] == request['job'] and after['state'] == state
    assert after['result'] == request['result']
    assert 'attempt_id' not in after and 'publication' not in after
    assert Path(after['source']).read_bytes() == original
    assert not (root / 'imports.sqlite').exists()
    again = restored.resume_import(workspace, request['request_id'])
    assert again['state'] == 'complete' and again['id'] == request['job']
    assert 'Immutable' in restored.search(workspace, 'Immutable')[0].quote
    assert load_request(app.root, request['request_id']) == request


@pytest.mark.parametrize('suffix', ['.html', '.docx', '.epub'])
def test_unpublished_direct_job_is_verified_in_isolated_worker_then_restores_and_resumes(
        tmp_path, monkeypatch, suffix):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Stopped direct import')
    source = _source(tmp_path, suffix)
    original = source.read_bytes()
    request = _stopped(app, workspace, source, monkeypatch)
    source.unlink()
    from carr_library.ingest import worker
    invoke = worker._invoke
    calls = []

    def observe(message, **kwargs):
        calls.append(dict(message))
        return invoke(message, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker, '_invoke', observe)
        backup = tmp_path / 'backup'
        checksum = create_backup(app, backup)
        root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert len(calls) == 2 and {call['action'] for call in calls} == {'direct-identity'}
    assert all(call['sha256'] == request['asset_sha256'] for call in calls)
    restored = LibraryApp(root)
    after = load_request(root, request['request_id'])
    assert after['job'] == request['job'] and after['state'] == 'cancelled'
    assert Path(after['source']).read_bytes() == original
    result = restored.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['id'] == request['job']
    assert not (root / 'imports.sqlite').exists()
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize('field', ['workspace', 'edition', 'asset', 'job'])
def test_direct_request_cannot_claim_foreign_scope_or_revision(tmp_path, field):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original scope')
    source = _source(tmp_path, '.html')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete'
    foreign = app.create_workspace('Foreign scope')
    other = b'<html><body><p>A different retained source.</p></body></html>'
    other_hash = sha256(other).hexdigest()
    app.assets.retain(other, 'text/html', other_hash)

    def change(value):
        value['state'] = 'cancelled'
        for key in ('publication', 'attempt_id', 'result'):
            value.pop(key, None)
        if field == 'workspace':
            value['workspace'] = foreign
        elif field == 'edition':
            value['metadata']['edition_id'] = uuid4().hex
        elif field == 'asset':
            value['asset_sha256'] = other_hash
            if 'expected_manifest' in value:
                value['expected_manifest'] = dict(sha256=other_hash, mime='text/html', size=len(other))
        else:
            value['job'] = '0' * 64
    _mutate(app.root / 'import-requests' / (result['request_id'] + '.json'), change)
    with pytest.raises(ValueError, match='foreign|identity'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('field', ['config_sha256', 'pages_sha256', 'active_revision'])
def test_direct_publication_receipt_is_checked_against_catalog_not_only_its_own_shape(tmp_path, field):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Receipt scope')
    result = app.import_file(workspace, _source(tmp_path, '.html'))
    assert result['state'] == 'complete'

    def change(value):
        if field == 'active_revision':
            value['publication']['before'][field] = 'f' * 64
        else:
            value['publication'][field] = 'f' * 64
    _mutate(app.root / 'import-requests' / (result['request_id'] + '.json'), change)
    with pytest.raises(ValueError, match='receipt|pre-state'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_missing_text_journal_does_not_enter_the_direct_document_exception(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Journal required')
    source = tmp_path / 'original.txt'
    source.write_text(TEXT, encoding='utf-8')
    result = run_request(app.root, workspace, source, _metadata(), Config())
    assert result['state'] == 'complete' and (app.root / 'imports.sqlite').exists()
    (app.root / 'imports.sqlite').unlink()
    with pytest.raises(ValueError, match='foreign journal'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_resealed_component_cannot_retarget_an_unpublished_direct_job(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original scope')
    request = _stopped(app, workspace, _source(tmp_path, '.html'), monkeypatch)
    backup = tmp_path / 'component'
    backup.mkdir()
    with exclusive_writer(app.root), closing(Storage(app.database)) as storage:
        storage.backup(backup / 'library.sqlite')
        manifest = snapshot_pending(app, backup / 'pending')
    name = 'import-requests/' + request['request_id'] + '.json'
    path = backup / 'pending' / name
    _mutate(path, lambda value: value.update(job='0' * 64))
    raw = path.read_bytes()
    entry = next(entry for entry in manifest['files'] if entry['filename'] == name)
    entry.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    stage = tmp_path / 'restore-stage'
    stage.mkdir()
    shutil.copyfile(backup / 'library.sqlite', stage / 'library.sqlite')
    with pytest.raises(ValueError, match='foreign|identity'):
        restore_pending(backup / 'pending', stage, tmp_path / 'restored', manifest)
    assert not (stage / 'import-requests').exists()
    assert not (tmp_path / 'restored').exists()


def test_direct_identity_child_checks_actual_bytes_and_never_creates_a_catalog(tmp_path):
    source = _source(tmp_path, '.html')
    original = source.read_bytes()
    message = dict(action='direct-identity', source=str(source), sha256=sha256(original).hexdigest(),
                   mime='text/html', workspace='scope', edition='edition', timeout=30)
    reply = _invoke(message, timeout=30)
    assert set(reply) == {'revision', 'config_sha256', 'page_count', 'pages_sha256'}
    assert reply['page_count'] == 1
    source.write_bytes(original.replace(b'Immutable', b'Different'))
    assert _invoke(message, timeout=30)['state'] == 'failed'
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]


def test_cancel_during_direct_identity_child_cleans_the_complete_backup(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Cancellation scope')
    _stopped(app, workspace, _source(tmp_path, '.html'), monkeypatch)
    from carr_library.ingest import worker
    popen = worker.subprocess.Popen
    children = []

    def started(*args, **kwargs):
        process = popen(*args, **kwargs)
        if args[0][0] == sys.executable:
            children.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', started)
    with pytest.raises(InterruptedError):
        create_backup(app, tmp_path / 'cancelled', cancelled=lambda: bool(children))
    assert len(children) == 1 and children[0].poll() is not None
    assert not (tmp_path / 'cancelled').exists()
