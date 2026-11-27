"""Actual isolated LibraryApp publication races; no synthetic database or result."""
from hashlib import sha256
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker


def source_file(tmp_path, kind):
    path = tmp_path / ('Оригінал source.' + kind)
    if kind == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph('Committed railway source. Київ.')
        document.save(path)
    elif kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, 'Committed railway source.')
        canvas.save()
    elif kind == 'epub':
        with ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml',
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr('content.opf',
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata/><manifest><item id="chapter" href="chapter.xhtml" '
                'media-type="application/xhtml+xml"/></manifest>'
                '<spine><itemref idref="chapter"/></spine></package>')
            archive.writestr('chapter.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Source</title></head>'
                '<body><p>Committed railway source. Київ.</p></body></html>')
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


def capture_attempt(monkeypatch):
    invoke = worker._invoke
    launches = []
    def launch(message, **options):
        if 'request_id' in message:
            launches.append((worker.load_request(message['data_dir'], message['request_id']),
                             message['attempt_id']))
        return invoke(message, **options)
    monkeypatch.setattr(worker, '_invoke', launch)
    return launches


@pytest.mark.parametrize('kind', ('docx', 'epub', 'pdf', 'txt', 'md'))
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


@pytest.mark.parametrize('change', ('scope', 'work', 'asset', 'configuration', 'language'))
def test_changed_request_identity_cannot_authorize_reconciliation(tmp_path, monkeypatch, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Request binding')
    source = source_file(tmp_path, 'docx')
    launches = capture_attempt(monkeypatch)
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete'
    initial, attempt = launches[0]
    assert worker._published_after_stop(app, initial, attempt)['state'] == 'complete'
    changed = worker.load_request(app.root, result['request_id'])
    if change == 'scope':
        changed['workspace'] = app.create_workspace('Other')
    elif change == 'work':
        changed['metadata']['work_id'] = uuid4().hex
    elif change == 'asset':
        changed['asset_sha256'] = 'f' * 64
    elif change == 'configuration':
        changed['config']['dpi'] += 1
    else:
        changed['metadata']['language'] = 'uk'
    worker._store(app.root, result['request_id'], changed)
    assert worker._published_after_stop(app, initial, attempt) is None
    with pytest.raises(ValueError):
        worker._reconcile_publication(app.root, result['request_id'], attempt, initial=initial)


def test_corrupted_retained_bytes_cannot_authorize_reconciliation(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original binding')
    source = source_file(tmp_path, 'docx')
    launches = capture_attempt(monkeypatch)
    result = app.import_file(workspace, source)
    initial, attempt = launches[0]
    assert worker._published_after_stop(app, initial, attempt)['state'] == 'complete'
    current = worker.load_request(app.root, result['request_id'])
    retained = app.assets.resolve(current['asset_sha256'])
    raw = bytearray(retained.read_bytes())
    raw[-1] ^= 1
    retained.write_bytes(raw)
    assert worker._published_after_stop(app, initial, attempt) is None
    with pytest.raises(ValueError):
        worker._reconcile_publication(app.root, result['request_id'], attempt, initial=initial)


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


@pytest.mark.parametrize('conflict', ('language', 'authors', 'title', 'edition', 'volume', 'work', 'collections'))
def test_another_requests_fresh_publication_cannot_hide_metadata_or_membership_failure(
        tmp_path, monkeypatch, conflict):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Concurrent request identities')
    collection = app.create_collection(workspace, 'Required membership')
    source = source_file(tmp_path, 'docx')
    wanted = dict(title='Requested title', authors=('Research; Team',), language='uk',
                  edition='Second', volume='II', collections=(collection,),
                  work_id='shared-work', edition_id='shared-edition')
    other = dict(wanted)
    changes = dict(language='en', authors=('Research', 'Team'), title='Different title',
                   edition='First', volume='I', work_id='other-work', collections=())
    field = 'work_id' if conflict == 'work' else conflict
    other[field] = changes[field]
    marker = tmp_path / 'before-request-publication'
    processes = publication_barrier(monkeypatch, marker, before=True)
    original_popen = worker.subprocess.Popen
    competing = []

    def launch(command, *args, **kwargs):
        if (isinstance(command, list) and '-c' in command
                and "runpy.run_module('carr_library.ingest.worker'" in command[command.index('-c') + 1]):
            assert not competing
            # A genuine independent publication after this parent's frozen
            # request snapshot, before its actual child. No row or result
            # is fabricated, and no watermark grants completion authority.
            competing.append(app._import_file(workspace, source, **other))
            assert competing[0]['state'] == 'complete'
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    result = app.import_file(workspace, source, **wanted, cancelled=marker.exists, timeout=30)
    assert result['state'] == ('cancelled' if conflict == 'collections' else 'failed'), result
    assert 'recovered_publication' not in result
    assert len(processes) == 1 and processes[0].poll() is not None
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication WHERE request_id=?',
                          (result['request_id'],)).fetchone()[0] == 0
        assert tuple(db.execute('SELECT language,edition,volume FROM catalog_edition').fetchone()) == (
            other['language'], other['edition'], other['volume'])
        assert tuple(row[0] for row in db.execute('SELECT name FROM catalog_author ORDER BY position')) == other['authors']
    if conflict == 'collections':
        assert app.browse(workspace, collection=collection) == ()



@pytest.mark.parametrize('binding', ('job', 'configuration'))
def test_pdf_resume_attempt_rejects_a_replaced_frozen_job_or_config(
        tmp_path, monkeypatch, binding):
    """A saved PDF journal cannot be rebound after the parent admits an attempt."""
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Frozen PDF resume binding')
    source = source_file(tmp_path, 'pdf')
    original = source.read_bytes()
    launches = capture_attempt(monkeypatch)
    first = app.import_file(workspace, source)
    assert first['state'] == 'complete'
    initial, first_attempt = launches[0]
    assert worker._published_after_stop(app, initial, first_attempt)['state'] == 'complete'
    saved = worker.load_request(app.root, first['request_id'])
    assert saved['job'] == first['id']
    with app.read(workspace) as db:
        receipt = tuple(db.execute('SELECT * FROM import_publication').fetchone())

    # Reproduce losing the parent result. The next native launch has a fresh
    # attempt, but its saved PDF job and extraction configuration remain pinned.
    worker.record_progress(app.root, first['request_id'], state='running', result=None)
    invoke = worker._invoke
    frozen, replies = [], []

    def changed_before_child(message, **options):
        record = worker.load_request(app.root, message['request_id'])
        frozen.append((json.loads(json.dumps(record)), message['attempt_id']))
        if binding == 'job':
            record['job'] = 'f' * 64 if record['job'] != 'f' * 64 else 'e' * 64
        else:
            record['config']['dpi'] += 1
        worker._store(app.root, message['request_id'], record)
        reply = invoke(message, **options)
        replies.append(reply)
        return reply

    monkeypatch.setattr(worker, '_invoke', changed_before_child)
    result = app.resume_import(workspace, first['request_id'], timeout=30)
    assert len(frozen) == len(replies) == 1
    admitted, attempt = frozen[0]
    assert attempt != first_attempt
    assert admitted['job'] == first['id'] and admitted['config'] == saved['config']
    assert replies[0]['state'] == 'failed', replies
    assert 'changed before this worker attempt' in replies[0]['message']
    assert result['state'] == 'failed' and 'recovered_publication' not in result
    assert worker._published_after_stop(app, admitted, attempt) is None
    assert source.read_bytes() == original
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt


@pytest.mark.parametrize('binding', ('job', 'configuration'))
def test_pdf_reconciliation_rechecks_config_against_a_valid_captured_attempt(
        tmp_path, monkeypatch, binding):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Committed PDF configuration')
    source = source_file(tmp_path, 'pdf')
    launches = capture_attempt(monkeypatch)
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete'
    initial, attempt = launches[0]
    # This positive control rules out a wrong/legacy argument that would make
    # the following rejection pass without consulting a real committed receipt.
    recovered = worker._published_after_stop(app, initial, attempt)
    assert recovered['state'] == 'complete' and recovered['id'] == result['id']
    request = worker.load_request(app.root, result['request_id'])
    assert request['job'] == result['id']
    if binding == 'job':
        request['job'] = 'f' * 64 if request['job'] != 'f' * 64 else 'e' * 64
    else:
        request['config']['dpi'] += 1
    worker._store(app.root, result['request_id'], request)
    assert worker._published_after_stop(app, initial, attempt) is None


@pytest.mark.parametrize('boundary', (None, 0))
def test_legacy_watermark_survives_backup_validation_but_is_not_restored(tmp_path, boundary):
    from carr_library.library_backup import create_backup, restore_backup

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Legacy request backup')
    source = source_file(tmp_path, 'txt')
    result = worker.run_request(app.root, workspace, source,
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    assert result['state'] == 'complete'
    request = worker.load_request(app.root, result['request_id'])
    request['publication_watermark'] = boundary
    request['state'] = 'running'  # The parent never received its final reply.
    request.pop('result')
    worker._store(app.root, result['request_id'], request)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    source.unlink()

    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = worker.load_request(restored_root, result['request_id'])
    assert not {'publication_watermark', 'attempt', 'publication_candidate'} & restored.keys()
    reopened = LibraryApp(restored_root)
    resumed = reopened.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == result['id']
    with reopened.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1


@pytest.mark.parametrize('boundary', (True, -1, 2 ** 63, '0'))
def test_malformed_legacy_watermark_cannot_enter_backup(tmp_path, boundary):
    from carr_library.library_backup import create_backup

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Rejected legacy request')
    result = worker.run_request(app.root, workspace, source_file(tmp_path, 'txt'),
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    assert result['state'] == 'complete'
    request = worker.load_request(app.root, result['request_id'])
    request['publication_watermark'] = boundary
    worker._store(app.root, result['request_id'], request)
    with pytest.raises(ValueError, match='publication boundary'):
        create_backup(app, tmp_path / 'rejected-backup')
