"""Actual isolated LibraryApp publication races; no synthetic database or result."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
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
    outcomes = []
    invoke = worker._invoke

    def observe_invocation(*args, **kwargs):
        result = invoke(*args, **kwargs)
        outcomes.append(result['state'])
        return result

    # Observe the actual stopped child; do not replace its result with a fixture.
    monkeypatch.setattr(worker, '_invoke', observe_invocation)
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
    assert result['publication_recovered'] is True
    assert outcomes == [{'cancel': 'cancelled', 'timeout': 'timed_out', 'crash': 'failed'}[stop]]
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
        receipt = db.execute('SELECT request_id,attempt,sha256 FROM import_publication '
                             'WHERE revision=?', (result['id'],)).fetchone()
        candidate = request['publication_candidate']
        assert candidate['revision'] == result['id'] and candidate['attempt'] == request['attempt']
        assert tuple(receipt) == (result['request_id'], request['attempt'], candidate['sha256'])


def test_precommit_cancellation_remains_cancelled_without_publication(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Before commit')
    source = source_file(tmp_path, 'docx')
    marker = tmp_path / 'before-commit'
    processes = publication_barrier(monkeypatch, marker, before=True)
    result = app.import_file(workspace, source, cancelled=marker.exists, timeout=30)
    assert marker.is_file(), result
    assert result['state'] == 'cancelled'
    assert 'publication_recovered' not in result
    assert len(processes) == 1 and processes[0].poll() is not None
    assert app.browse(workspace) == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0


def test_prelaunch_cancellation_never_starts_or_recovers_a_worker(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Before launch')
    source = source_file(tmp_path, 'docx')
    def forbidden(*args, **kwargs):
        raise AssertionError('A pre-cancelled import must not launch a process.')
    monkeypatch.setattr(worker.subprocess, 'Popen', forbidden)
    result = app.import_file(workspace, source, cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    assert 'publication_recovered' not in result
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
    assert 'publication_recovered' not in cancelled
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_other_import_before_child_launch_is_not_this_workers_commit(tmp_path, monkeypatch):
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
            # Another real import completes the same source/edition before our
            # child acquires the shared writer guard. Equal source identity
            # cannot grant this request the other transaction's ownership.
            published.append(app._import_file(workspace, source,
                work_id=work_id, edition_id=edition_id))
        return launch(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', intervening_import)
    result = app.import_file(workspace, source, work_id=work_id, edition_id=edition_id,
                            cancelled=marker.exists, timeout=30)
    assert published[0]['state'] == 'complete'
    assert marker.is_file() and len(processes) == 1
    assert result['state'] == 'cancelled', result
    assert 'publication_recovered' not in result
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
    with pytest.raises(ValueError, match='publication|Committed'):
        worker._reconcile_publication(app.root, result['request_id'], initial['attempt'])


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
    with pytest.raises(ValueError):
        worker._reconcile_publication(app.root, result['request_id'], initial['attempt'])


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
