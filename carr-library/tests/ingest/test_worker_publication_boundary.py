"""Actual isolated LibraryApp publication races; no synthetic database or result."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
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
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, 'Committed railway source.')
        canvas.showPage()
        canvas.save()
    elif kind == 'epub':
        import runpy
        fixture = Path(__file__).resolve().parents[1] / 'e2e' / 'test_epub_import.py'
        runpy.run_path(str(fixture))['write_epub'](path)
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


def publication_diagnostics(monkeypatch):
    """Observe the actual verification branch without changing its verdict.

    Only the actual recovery frame is recorded, after child cleanup.
    Keep bounded exception metadata and return locations, never frame locals,
    source bytes, request contents or database rows.
    """
    events = []

    def observer(name, function):
        def observed(*args, **kwargs):
            previous = sys.gettrace()

            def trace(frame, event, value):
                if frame.f_code is not function.__code__:
                    return previous(frame, event, value) if previous is not None else None
                if event in ('exception', 'return') and len(events) < 32:
                    entry = dict(function=name, event=event, line=frame.f_lineno)
                    if event == 'exception':
                        error = value[1]
                        entry.update(error=type(error).__name__, message=str(error)[:240])
                        for attribute in ('errno', 'winerror', 'sqlite_errorcode', 'sqlite_errorname'):
                            detail = getattr(error, attribute, None)
                            if type(detail) in (int, str):
                                entry[attribute] = detail[:120] if isinstance(detail, str) else detail
                    else:
                        entry['returned'] = 'None' if value is None else type(value).__name__
                        if type(value) is dict and type(value.get('state')) is str:
                            entry['state'] = value['state'][:40]
                    events.append(entry)
                return trace

            try:
                sys.settrace(trace)
                return function(*args, **kwargs)
            finally:
                sys.settrace(previous)

        return observed

    for name in ('_recover_publication',):
        monkeypatch.setattr(worker, name, observer(name, getattr(worker, name)))
    return events


@pytest.mark.parametrize('kind', ('docx', 'md', 'txt', 'pdf', 'epub'))
@pytest.mark.parametrize('stop', ('cancel', 'timeout', 'crash'))
def test_durable_publication_survives_stopped_worker(tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Publication boundary')
    source = source_file(tmp_path, kind)
    original = source.read_bytes()
    marker = tmp_path / 'committed'
    processes = publication_barrier(monkeypatch, marker, crash=stop == 'crash')
    diagnostics = publication_diagnostics(monkeypatch)
    original_trace = sys.gettrace()
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
    assert result['state'] == 'complete', dict(result=result, publication_verification=diagnostics)
    assert sys.gettrace() is original_trace
    assert {entry['function'] for entry in diagnostics} == {'_recover_publication'}
    assert result['recovered_publication'] is True
    assert outcomes == [{'cancel': 'cancelled', 'timeout': 'timed_out', 'crash': 'failed'}[stop]]
    assert result['worker_state'] == {'cancel': 'cancelled', 'timeout': 'timed_out', 'crash': 'failed'}[stop]
    assert result['completed'] == (2 if kind == 'epub' else 1)
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
    assert 'recovered_publication' not in result
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
    assert 'recovered_publication' not in result
    assert app.browse(workspace) == ()


def test_preexisting_replay_is_not_new_completion_after_cancel(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay boundary')
    source = source_file(tmp_path, 'docx')
    existing = app.create_collection(workspace, 'Existing membership')
    requested = app.create_collection(workspace, 'Requested membership')
    work_id, edition_id = uuid4().hex, uuid4().hex
    first = app.import_file(workspace, source, work_id=work_id, edition_id=edition_id,
                            collections=(existing,))
    assert first['state'] == 'complete'
    marker = tmp_path / 'replay-before-commit'
    publication_barrier(monkeypatch, marker, before=True)
    cancelled = app.import_file(workspace, source, work_id=work_id, edition_id=edition_id,
                               collections=(requested,), cancelled=marker.exists, timeout=30)
    assert marker.is_file(), cancelled
    assert cancelled['state'] == 'cancelled'
    assert 'recovered_publication' not in cancelled
    assert app.browse(workspace, collection=requested) == ()
    assert len(app.browse(workspace, collection=existing)) == 1
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
            # A genuine independent publication after the supervisor's watermark,
            # before its actual child. No row/result is fabricated or retargeted.
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
        assert tuple(db.execute('SELECT language,edition,volume FROM catalog_edition').fetchone()) == (
            other['language'], other['edition'], other['volume'])
        assert tuple(row[0] for row in db.execute('SELECT name FROM catalog_author ORDER BY position')) == other['authors']
    if conflict == 'collections':
        assert app.browse(workspace, collection=collection) == ()


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


def stopped_publication_record(app, workspace, source, tmp_path, monkeypatch):
    """Keep the real child's pre-acknowledgement candidate for adversarial verification."""
    marker = tmp_path / 'committed-before-acknowledgement'
    publication_barrier(monkeypatch, marker, crash=True)
    captured = []
    recover = worker._recover_publication
    def observed(root, request_id, attempt, request_digest):
        record = deepcopy(worker.load_request(root, request_id))
        assert record['result']['state'] == 'publishing'
        captured.append((record, attempt, request_digest))
        return recover(root, request_id, attempt, request_digest)
    monkeypatch.setattr(worker, '_recover_publication', observed)
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['recovered_publication'] is True
    assert marker.exists() and len(captured) == 1
    record, attempt, request_digest = captured[0]
    monkeypatch.setattr(worker, '_recover_publication', recover)
    worker._store(app.root, result['request_id'], record)
    return result, record, attempt, request_digest


@pytest.mark.parametrize('change', ('scope', 'work', 'asset', 'configuration'))
def test_changed_request_identity_cannot_authorize_reconciliation(tmp_path, monkeypatch, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Request binding')
    source = source_file(tmp_path, 'docx')
    result, initial, attempt, request_digest = stopped_publication_record(
        app, workspace, source, tmp_path, monkeypatch)
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
    with pytest.raises(ValueError, match='publication|Committed'):
        worker._recover_publication(app.root, result['request_id'], attempt, request_digest)


def test_corrupted_retained_bytes_cannot_authorize_reconciliation(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original binding')
    source = source_file(tmp_path, 'docx')
    result, initial, attempt, request_digest = stopped_publication_record(
        app, workspace, source, tmp_path, monkeypatch)
    retained = app.assets.resolve(initial['asset_sha256'])
    raw = bytearray(retained.read_bytes())
    raw[-1] ^= 1
    retained.write_bytes(raw)
    assert worker._published_after_stop(app, initial, 0) is None
    with pytest.raises(ValueError):
        worker._recover_publication(app.root, result['request_id'], attempt, request_digest)


@pytest.mark.parametrize('binding', ('job', 'configuration'))
def test_pdf_reconciliation_requires_its_exact_saved_job_and_config(tmp_path, binding):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('PDF request binding')
    source = source_file(tmp_path, 'pdf')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete'
    request = worker.load_request(app.root, result['request_id'])
    if binding == 'job':
        request['job'] = 'f' * 64
    else:
        request['config']['dpi'] += 1
    worker._store(app.root, result['request_id'], request)
    assert worker._published_after_stop(app, request, 0) is None


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
