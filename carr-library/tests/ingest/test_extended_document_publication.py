"""Additional formats use the existing transactional receipt and recovery path.

Actual source bytes, worker processes, SQLite publication, search and backups
remain in use. Only the trusted test bootstrap installs publication barriers.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from tests.e2e.test_extended_document_workflows import (
    FIRST, FORMATS, archive_bytes, odf_bytes, source_bytes,
)
from tests.ingest.test_worker_publication_boundary import publication_barrier
from tests.ingest.test_worker_publication_outcome import child_barrier


@pytest.mark.parametrize('extension', FORMATS)
@pytest.mark.parametrize('point', ('before-commit', 'after-commit-crash'))
def test_extended_worker_commit_boundary_uses_its_exact_receipt(tmp_path, monkeypatch, extension, point):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Real document publication')
    source = tmp_path / ('source' + extension)
    original = source_bytes(extension)
    source.write_bytes(original)
    marker = tmp_path / 'publication-boundary'
    processes = publication_barrier(monkeypatch, marker,
        before=point == 'before-commit', crash=point == 'after-commit-crash')
    result = app.import_file(workspace, source, cancelled=marker.exists, timeout=30)
    assert marker.exists() and len(processes) == 1
    assert processes[0].poll() is not None
    assert source.read_bytes() == original
    request = worker.load_request(app.root, result['request_id'])
    assert request['result'] == result
    with app.read(workspace) as db:
        receipts = db.execute('SELECT revision,request_id,attempt FROM import_publication').fetchall()
    if point == 'before-commit':
        assert result['state'] == 'cancelled' and not result.get('recovered_publication')
        assert receipts == [] and app.browse(workspace) == ()
        assert app.search(workspace, 'Librarymarker') == ()
    else:
        assert result['state'] == 'complete' and result['recovered_publication'] is True
        assert result['verified'] is False
        assert result['format'] and result['warnings']
        assert [tuple(row) for row in receipts] == [
            (result['id'], request['request_id'], request['attempt'])]
        hit, = app.search(workspace, 'Librarymarker')
        assert hit.revision == result['id'] and hit.verified is False
        assert FIRST in app.preview(workspace, hit.ref, hit.revision)
        source.unlink()
        # Reopening a recovered request does not parse a missing original or
        # duplicate its already committed source.
        assert LibraryApp(app.root).resume_import(workspace, result['request_id']) == result


@pytest.mark.parametrize('wrong_owner', ('request', 'attempt'))
def test_equal_extended_revision_cannot_complete_another_request_or_attempt(tmp_path, wrong_owner):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Receipt owner')
    source = tmp_path / 'source.md'
    source.write_bytes(source_bytes('.md'))
    first = app.import_file(workspace, source)
    initial = worker.load_request(app.root, first['request_id'])
    changed = deepcopy(initial)
    if wrong_owner == 'request':
        changed['request_id'] = uuid4().hex
    else:
        changed['attempt'] = uuid4().hex
        changed['publication_candidate']['attempt'] = changed['attempt']
    worker._store(app.root, changed['request_id'], changed)
    assert worker._reconcile_publication(app.root, changed['request_id'], changed['attempt'],
        library=app, initial=changed) is None
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert tuple(db.execute('SELECT request_id,attempt FROM import_publication').fetchone()) == (
            initial['request_id'], initial['attempt'])
    assert len(app.search(workspace, 'Librarymarker')) == 1


def test_interrupted_extended_membership_replay_requires_explicit_resume(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay receipt')
    source = tmp_path / 'source.odt'
    original = source_bytes('.odt')
    source.write_bytes(original)
    metadata = dict(title='Stable title', work_id='work', edition_id='edition')
    first = app.import_file(workspace, source, **metadata)
    with app.read(workspace) as db:
        receipt = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    collection = app.create_collection(workspace, 'New membership')
    marker = tmp_path / 'committed-membership'
    with monkeypatch.context() as patch:
        processes = child_barrier(patch, marker, point='after-import')
        stopped = app.import_file(workspace, source, **metadata,
            collections=(collection,), cancelled=marker.exists, timeout=30)
    assert marker.exists() and stopped['state'] == 'cancelled'
    assert not stopped.get('recovered_publication')
    assert all(process.poll() is not None for process in processes)
    assert len(app.browse(workspace, collection=collection)) == 1
    assert worker.load_request(app.root, stopped['request_id'])['publication_candidate'] is None
    resumed = app.resume_import(workspace, stopped['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == first['id']
    assert len(app.browse(workspace, collection=collection)) == 1
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt
    assert source.read_bytes() == original


def test_paused_ten_format_batch_restores_and_resumes_without_original_paths(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Portable pending documents')
    folder = tmp_path / 'selected'
    folder.mkdir()
    originals = {}
    for extension in FORMATS:
        original = source_bytes(extension)
        (folder / ('source' + extension)).write_bytes(original)
        originals[sha256(original).hexdigest()] = original
    planned = app.plan_import_batch(workspace, [folder])
    assert planned['counts']['queued'] == 10
    paused = False
    def progress(batch):
        nonlocal paused
        paused = bool(batch['counts']['complete'])
    partial = app.run_import_batch(workspace, planned['batch_id'],
        progress=progress, cancelled=lambda: paused)
    assert partial['state'] == 'paused' and partial['counts']['complete'] == 1
    identities = [(row['row_id'], row['work_id'], row['edition_id']) for row in partial['rows']]
    checksum = create_backup(app, tmp_path / 'backup')
    for source in folder.iterdir():
        source.unlink()
    folder.rmdir()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum))
    done = restored.run_import_batch(workspace, planned['batch_id'])
    assert done['state'] == 'complete' and done['counts']['complete'] == 10
    assert [(row['row_id'], row['work_id'], row['edition_id']) for row in done['rows']] == identities
    assert len(restored.search(workspace, 'Librarymarker')) == 10
    for book in restored.browse(workspace):
        path = restored.assets.resolve(book.asset)
        assert path.read_bytes() == originals[book.asset]
        assert not ReadingDesk(restored, workspace, book.edition_id).page(1).verified
    again = restored.run_import_batch(workspace, planned['batch_id'])
    assert again['counts']['complete'] == 10 and len(restored.browse(workspace)) == 10


def test_real_spreadsheet_locations_cannot_exceed_publication_configuration_budget(tmp_path):
    from io import BytesIO
    from zipfile import ZipFile
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded document locations')
    with ZipFile(BytesIO(odf_bytes('.ods'))) as archive:
        entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
    # Valid source data: one repeated text cell under a long stored sheet name.
    # Each individual locator is valid, but 5000 locations exceed 4 MiB. The
    # configuration ceiling remains independent of the 64 MiB source limit.
    modified = [(name, raw.replace(b'table:name="Research"', b'table:name="' + b'N' * 1000 + b'"')
                 .replace(b'<table:table-row>', b'<table:table-row table:number-rows-repeated="2500">')
                 if name == 'content.xml' else raw) for name, raw in entries]
    source = tmp_path / 'bounded.ods'
    source.write_bytes(archive_bytes(modified, stored=('mimetype',)))
    result = app.import_file(workspace, source)
    assert result['state'] == 'failed' and 'configuration exceeds' in result['message']
    assert app.browse(workspace) == () and app.search(workspace, 'Librarymarker') == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0


def test_extended_replay_after_metadata_edit_preserves_current_source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Metadata revision')
    source = tmp_path / 'source.html'
    source.write_bytes(source_bytes('.html'))
    first = app.import_file(workspace, source, title='Old title', work_id='work', edition_id='edition')
    current = app.update_metadata(workspace, 'edition', title='Reviewed title')
    collection = app.create_collection(workspace, 'New collection')
    repeated = app.import_file(workspace, source, title='Reviewed title', work_id='work',
        edition_id='edition', collections=(collection,))
    assert repeated['state'] == 'superseded' and repeated['id'] == first['id']
    hit, = app.search(workspace, 'Librarymarker', collection=collection)
    assert hit.revision == current and hit.title == 'Reviewed title' and not hit.verified
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1


def test_extended_document_record_limit_does_not_inherit_pdf_page_limit(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Logical records')
    source = tmp_path / 'source.fb2'
    source.write_bytes(('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
        '<body><section>' + ''.join(f'<p>Original paragraph {n}.</p>' for n in range(1, 1002))
        + '</section></body></FictionBook>').encode('utf-8'))
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['completed'] == 1001
    request = worker.load_request(app.root, result['request_id'])
    recovered = worker._reconcile_publication(app.root, result['request_id'], request['attempt'],
        library=app, initial=request)
    assert recovered['completed'] == 1001 and recovered['verified'] is False
    page = ReadingDesk(app, workspace, result['edition']).page(1001)
    assert page.raw_text == 'Original paragraph 1001.' and not page.verified


def test_extended_replay_cancellation_rolls_back_new_membership(tmp_path, monkeypatch):
    from threading import Event
    from carr_library.storage.revisions import Storage
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay cancellation')
    source = tmp_path / 'source.html'
    original = source_bytes('.html')
    source.write_bytes(original)
    metadata = dict(title='Original title', work_id='work', edition_id='edition')
    first = app.import_file(workspace, source, **metadata)
    collection = app.create_collection(workspace, 'Cancelled new membership')
    stopped = Event()
    check = Storage._check
    def checked_then_cancelled(storage):
        check(storage)
        if storage.db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? AND collection=?',
                              (workspace, collection)).fetchone():
            stopped.set()
    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', checked_then_cancelled)
        with pytest.raises(InterruptedError, match='cancelled before commit'):
            app._import_file(workspace, source, collections=(collection,),
                cancelled=stopped.is_set, **metadata)
    assert stopped.is_set() and app.browse(workspace, collection=collection) == ()
    hit, = app.search(workspace, 'Librarymarker')
    assert hit.revision == first['id'] and not hit.verified
    assert source.read_bytes() == original
    assert app.assets.resolve(sha256(original).hexdigest()).read_bytes() == original
