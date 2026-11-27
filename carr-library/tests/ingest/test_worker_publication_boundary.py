"""Actual isolated LibraryApp publication races; no synthetic database or result."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


def source_file(tmp_path, kind):
    path = tmp_path / ('Original source.' + kind)
    if kind == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph('Committed railway source. Київ.')
        document.save(path)
    elif kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        document = Canvas(str(path))
        document.drawString(72, 700, 'Committed railway source.')
        document.save()
    elif kind == 'epub':
        from zipfile import ZipFile
        with ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml',
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="book.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr('book.opf',
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata/><manifest><item id="chapter" href="chapter.xhtml" '
                'media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="chapter"/></spine></package>')
            archive.writestr('chapter.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Source</title>'
                '</head><body><p>Committed railway source. Київ.</p></body></html>')
    else:
        path.write_text('Committed railway source. Київ.', encoding='utf-8')
    return path


def publication_barrier(monkeypatch, marker, *, before=False, crash=False):
    """Only instrument the trusted child bootstrap around its real DB commit."""
    popen = worker.subprocess.Popen
    processes = []

    def launch(command, *args, **kwargs):
        if isinstance(command, list) and '-c' in command:
            index = command.index('-c') + 1
            if "runpy.run_module('carr_library.ingest.worker'" in command[index]:
                command = list(command)
                anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"
                hook = ("import os,time; from pathlib import Path; "
                        "from carr_library import application as product; ")
                if before:
                    body = [
                        "def barrier(*args, **kwargs):",
                        "    result = original(*args, **kwargs)",
                        "    Path(" + repr(str(marker)) + ").write_text('before publication', encoding='utf-8')",
                        "    time.sleep(60)",
                        "    return result",
                    ]
                    hook += ("original=product.register_batch; exec(" + repr("\n".join(body))
                             + "); product.register_batch=barrier; ")
                else:
                    body = [
                        "def barrier(self, *args, **kwargs):",
                        "    result = original(self, *args, **kwargs)",
                        "    Path(" + repr(str(marker)) + ").write_text('after commit', encoding='utf-8')",
                        "    os._exit(17)" if crash else "    time.sleep(60)",
                        "    return result",
                    ]
                    hook += ("original=product._CatalogPublisher.publish_revision; exec(" + repr("\n".join(body))
                             + "); product._CatalogPublisher.publish_revision=barrier; ")
                assert anchor in command[index]
                command[index] = command[index].replace(anchor, hook + anchor)
                process = popen(command, *args, **kwargs)
                processes.append(process)
                return process
        return popen(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    return processes


@pytest.mark.parametrize('kind', ('docx', 'epub', 'pdf', 'txt'))
@pytest.mark.parametrize('stop', ('cancel', 'timeout', 'crash'))
def test_durable_publication_survives_stopped_worker(tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Publication boundary')
    source = source_file(tmp_path, kind)
    original = source.read_bytes()
    marker = tmp_path / 'committed'
    processes = publication_barrier(monkeypatch, marker, crash=stop == 'crash')
    if stop == 'timeout':
        monotonic = time.monotonic
        monkeypatch.setattr(worker, 'time', SimpleNamespace(
            monotonic=lambda: monotonic() + (100 if marker.exists() else 0),
            sleep=time.sleep))
    cancelled = lambda: stop == 'cancel' and marker.exists()
    if kind == 'txt':
        batch = app.plan_import_batch(workspace, [source])
        completed_batch = app.run_import_batch(workspace, batch['batch_id'],
                                               cancelled=cancelled, timeout=30)
        result = completed_batch['rows'][0]['result']
        assert completed_batch['counts']['complete'] == 1
    else:
        result = app.import_file(workspace, source, cancelled=cancelled, timeout=30)
    assert marker.is_file(), result
    assert result['state'] == 'complete', result
    assert result['recovered_publication'] is True
    assert result['worker_state'] == {'cancel': 'cancelled', 'timeout': 'timed_out', 'crash': 'failed'}[stop]
    assert result['completed'] == 1
    assert len(processes) == 1 and processes[0].poll() is not None
    assert source.read_bytes() == original
    request = worker.load_request(app.root, result['request_id'])
    assert request['state'] == 'complete' and request['result'] == result
    source.unlink()
    resumed = LibraryApp(app.root).resume_import(workspace, result['request_id'])
    assert resumed == result
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1


def test_precommit_cancellation_remains_cancelled_without_publication(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Before commit')
    source = source_file(tmp_path, 'docx')
    marker = tmp_path / 'before-commit'
    processes = publication_barrier(monkeypatch, marker, before=True)
    result = app.import_file(workspace, source, cancelled=marker.exists, timeout=30)
    assert marker.is_file(), result
    assert result['state'] == 'cancelled'
    assert 'recovered_publication' not in result
    assert len(processes) == 1 and processes[0].poll() is not None
    assert app.browse(workspace) == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0


def test_prelaunch_cancellation_never_starts_or_recovers_a_worker(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Before launch')
    source = source_file(tmp_path, 'docx')
    def forbidden(*args, **kwargs):
        raise AssertionError('A pre-cancelled import must not launch a process.')
    monkeypatch.setattr(worker.subprocess, 'Popen', forbidden)
    result = app.import_file(workspace, source, cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    assert 'recovered_publication' not in result
    assert app.browse(workspace) == ()


def test_preexisting_replay_is_not_new_completion_after_cancel(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay boundary')
    source = source_file(tmp_path, 'docx')
    work_id, edition_id = uuid4().hex, uuid4().hex
    first = app.import_file(workspace, source, work_id=work_id, edition_id=edition_id)
    assert first['state'] == 'complete'
    marker = tmp_path / 'replay-before-commit'
    publication_barrier(monkeypatch, marker, before=True)
    cancelled = app.import_file(workspace, source, work_id=work_id, edition_id=edition_id,
                               cancelled=marker.exists, timeout=30)
    assert marker.is_file(), cancelled
    assert cancelled['state'] == 'cancelled'
    assert 'recovered_publication' not in cancelled
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_other_import_after_parent_snapshot_is_not_this_workers_commit(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Concurrent replay boundary')
    source = source_file(tmp_path, 'docx')
    work_id, edition_id = uuid4().hex, uuid4().hex
    marker = tmp_path / 'replay-before-commit'
    processes = publication_barrier(monkeypatch, marker, before=True)
    launch = worker.subprocess.Popen
    published = []

    def intervening_import(command, *args, **kwargs):
        if not published:
            # The parent read snapshot is already released, while this worker
            # has not acquired the shared writer guard. Another real import
            # completes the same source/edition before our child can replay it.
            published.append(app._import_file(workspace, source,
                work_id=work_id, edition_id=edition_id))
        return launch(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', intervening_import)
    result = app.import_file(workspace, source, work_id=work_id, edition_id=edition_id,
                            cancelled=marker.exists, timeout=30)
    assert published[0]['state'] == 'complete'
    assert marker.is_file() and len(processes) == 1
    assert result['state'] == 'cancelled', result
    assert 'recovered_publication' not in result
    assert app.browse(workspace)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize('change', ('scope', 'work', 'asset', 'configuration'))
def test_changed_request_identity_cannot_authorize_reconciliation(tmp_path, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Request binding')
    source = source_file(tmp_path, 'docx')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete'
    initial = worker.load_request(app.root, result['request_id'])
    changed = deepcopy(initial)
    if change == 'scope':
        changed['workspace'] = app.create_workspace('Other')
    elif change == 'work':
        changed['metadata']['work_id'] = uuid4().hex
    elif change == 'asset':
        changed['asset_sha256'] = 'f' * 64
    else:
        changed['config']['dpi'] += 1
    worker._store(app.root, result['request_id'], changed)
    assert worker._published_after_stop(app, initial, 0) is None


def test_corrupted_retained_bytes_cannot_authorize_reconciliation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original binding')
    source = source_file(tmp_path, 'docx')
    result = app.import_file(workspace, source)
    initial = worker.load_request(app.root, result['request_id'])
    retained = app.assets.resolve(initial['asset_sha256'])
    raw = bytearray(retained.read_bytes())
    raw[-1] ^= 1
    retained.write_bytes(raw)
    assert worker._published_after_stop(app, initial, 0) is None


def test_recovered_superseded_txt_does_not_claim_current_verified_completion(tmp_path):
    """Exercise both receipt states against real storage and retained bytes.

    The fixture reselects an older sealed revision after a genuine child import.
    It does not replace the verifier, query results, source bytes or worker.
    """
    from carr_library.application import _CatalogPublisher
    from carr_library.catalog.intake import snapshot
    from carr_library.catalog.registration import Registration
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.writer_guard import exclusive_writer
    from carr_library.storage.revisions import Page, Storage

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Superseded publication')
    source = source_file(tmp_path, 'txt')
    original = source.read_bytes()
    metadata = dict(title='Original title', authors=(), language='en',
                    edition=None, volume=None, collections=(),
                    work_id=uuid4().hex, edition_id=uuid4().hex)
    initial = worker.prepare_request(app.root, workspace, source, metadata, Config())
    asset = snapshot(source.parent, source.name)
    old_settings = worker.canonical({'adapter': 'historical-fixture',
                                     'kind': 'logical_segment'})
    old_revision = sha256(worker.canonical(
        [workspace, metadata['edition_id'], asset.digest, old_settings]).encode('utf-8')).hexdigest()
    registration = Registration(metadata['work_id'], metadata['edition_id'],
        asset.manifest(), metadata['title'], (), metadata['language'], None, None, ())
    with exclusive_writer(app.root), closing(Storage(app.database, schema_version=4)) as storage:
        app.assets.retain(asset.data, asset.mime, asset.digest)
        _CatalogPublisher(storage, registration).publish_revision(
            workspace, metadata['edition_id'], old_revision, asset.digest,
            old_settings, [Page(original.decode('utf-8'))])

    # A real isolated worker publishes a new revision after that old boundary.
    result = worker.run_request(app.root, workspace, source, {}, None,
                                request_id=initial['request_id'])
    assert result['state'] == 'complete' and result['verified'] is True
    active = worker._published_after_stop(app, initial, 0)
    assert active['state'] == 'complete' and active['verified'] is True

    # Retain both immutable revisions and construct a valid historical pointer
    # using the same index/active relationship enforced by Storage._check.
    with exclusive_writer(app.root), closing(Storage(app.database, schema_version=4)) as storage:
        with storage.transaction():
            storage.db.execute('DELETE FROM indexed WHERE id IN '
                '(SELECT id FROM active_page WHERE workspace=? AND edition=?)',
                (workspace, metadata['edition_id']))
            storage.db.execute('UPDATE active SET revision=? WHERE workspace=? AND edition=?',
                (old_revision, workspace, metadata['edition_id']))
            storage.db.execute('INSERT INTO indexed SELECT id,raw FROM page WHERE revision=?',
                (old_revision,))
            storage._check()

    recovered = worker._published_after_stop(app, initial, 0)
    assert recovered['state'] == 'superseded'
    assert recovered['id'] == result['id'] and recovered['verified'] is False
    assert recovered['recovered_publication'] is True
    assert source.read_bytes() == original
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 2
        assert db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                          (workspace, metadata['edition_id'])).fetchone()[0] == old_revision
