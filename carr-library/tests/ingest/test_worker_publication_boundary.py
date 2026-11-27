"""Actual isolated LibraryApp publication races; no synthetic database or result."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import time
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


class WorkerSubprocess:
    """Scope instrumentation to this supervisor, never the stdlib process module."""

    def __init__(self, original, popen):
        self._original = original
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(self._original, name)


class BarrierClock:
    """Arm the supervisor deadline before a committed child triggers expiry."""

    def __init__(self, marker):
        self.marker = marker
        self._armed = False
        self._expired = False
        self.sleep = time.sleep

    def monotonic(self):
        now = time.monotonic()
        if self._armed and self.marker.exists():
            self._expired = True
        self._armed = True
        return now + (100 if self._expired else 0)


def assert_reconciliation_rejected(app, request):
    try:
        recovered = worker._reconcile_publication(
            app.root, request['request_id'], request['attempt'],
            workspace=request['workspace'])
    except ValueError:
        return
    assert recovered is None


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

    monkeypatch.setattr(worker, 'subprocess', WorkerSubprocess(worker.subprocess, launch))
    return processes


@pytest.mark.parametrize('kind', ('docx', 'md', 'txt', 'pdf', 'epub'))
@pytest.mark.parametrize('stop', ('cancel', 'timeout', 'crash'))
def test_durable_publication_survives_stopped_worker(tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Publication boundary')
    source = source_file(tmp_path, kind)
    original = source.read_bytes()
    marker = tmp_path / 'committed'
    processes = publication_barrier(monkeypatch, marker, crash=stop == 'crash')
    if stop == 'timeout':
        monkeypatch.setattr(worker, 'time', BarrierClock(marker))
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
    monkeypatch.setattr(worker, 'subprocess', WorkerSubprocess(worker.subprocess, forbidden))
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
            # A genuine independent publication during the supervised attempt,
            # before its actual child. No row/result is fabricated or retargeted.
            competing.append(app._import_file(workspace, source, **other))
            assert competing[0]['state'] == 'complete'
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(worker, 'subprocess', WorkerSubprocess(worker.subprocess, launch))
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
    assert_reconciliation_rejected(app, initial)


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
    assert_reconciliation_rejected(app, initial)


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
    assert_reconciliation_rejected(app, request)
