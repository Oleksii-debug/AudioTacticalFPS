"""Actual LibraryApp/isolated-child/SQLite publication boundary regressions.

Only the child's before/after-COMMIT barrier is injected. Extraction, catalog
publication, retained originals, supervisor termination and reconciliation are
the product implementations, with a separate SQLite reader at the barrier.
"""
from copy import deepcopy
import sqlite3
import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.ingest import worker
from carr_library.ingest.writer_guard import exclusive_writer


def source_file(tmp_path, kind):
    source = tmp_path / ('Original railway source.' + kind)
    if kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Original railway publication boundary.')
        canvas.showPage()
        canvas.save()
    elif kind == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph('Original railway publication boundary.')
        document.save(source)
    else:
        source.write_bytes(b'Original railway publication boundary.\r\n')
    return source


def barrier(monkeypatch, tmp_path, *, phase, stop):
    marker = tmp_path / 'publication-barrier'
    processes = []
    launch = worker.subprocess.Popen
    # Fixed test bootstrap imports the real installed/current source worker.
    # Its only substitution is a bounded pause at the actual commit seam.
    bootstrap = f'''
import os, runpy, sys, time
from pathlib import Path
sys.path[:0] = sys.argv[1:]
from carr_library.ingest import worker
marker = Path({str(marker)!r})
original = worker.{'prepare_publication' if phase == 'before' else 'confirm_publication'}
def seam(*args, **kwargs):
    if {phase!r} == 'before':
        original(*args, **kwargs)
    marker.write_text(str(os.getpid()), encoding='ascii')
    if {stop!r} == 'crash':
        os._exit(37)
    time.sleep(20)
    raise RuntimeError('The publication barrier was not stopped within its budget')
worker.{'prepare_publication' if phase == 'before' else 'confirm_publication'} = seam
runpy.run_module('carr_library.ingest.worker', run_name='__main__')
'''

    def wrapped(arguments, **options):
        arguments = list(arguments)
        # Preserve Windows taskkill and any unrelated subprocess unchanged.
        if arguments[1:6] != ['-I', '-B', '-X', 'utf8', '-c']:
            return launch(arguments, **options)
        arguments[6] = bootstrap
        process = launch(arguments, **options)
        processes.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', wrapped)
    if stop == 'timeout':
        clock = time.monotonic
        monkeypatch.setattr(worker, 'time', SimpleNamespace(
            monotonic=lambda: clock() + (30 if marker.exists() else 0), sleep=time.sleep))
    return marker, processes


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
@pytest.mark.parametrize('stop', ['cancelled', 'timed_out', 'crash'])
def test_supervisor_recovers_exact_commit_before_stdout(tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Committed import')
    collection = app.create_collection(workspace, 'Requested collection')
    source = source_file(tmp_path, kind)
    marker, processes = barrier(monkeypatch, tmp_path, phase='after',
        stop={'cancelled': 'cancel', 'timed_out': 'timeout', 'crash': 'crash'}[stop])
    observed = []

    def cancelled():
        if marker.exists():
            with sqlite3.connect(app.database) as reader:
                observed.append(reader.execute('SELECT count(*) FROM sealed').fetchone()[0])
            return stop == 'cancelled'
        return False

    result = app.import_file(workspace, source, collections=(collection,),
                             cancelled=cancelled, timeout=10)
    assert marker.exists() and processes and all(p.poll() is not None for p in processes)
    assert result['state'] == 'complete' and result['reconciled'] is True
    assert result['worker_terminal_state'] == ('failed' if stop == 'crash' else stop)
    if stop != 'crash':
        assert observed and set(observed) == {1}  # Another connection saw COMMIT before stop.
    assert result['worker_message']
    request = worker.load_request(app.root, result['request_id'])
    assert request['publication']['committed'] is False  # Child never wrote an acknowledgment.
    assert request['state'] == 'complete' and request['result'] == result
    assert request['job'] == result['id'] == request['publication']['revision']
    assert len(app.browse(workspace, collection=collection)) == 1
    with sqlite3.connect(app.database) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    # The exact completed request recovers without launching another parser,
    # even if the original selected file has since disappeared.
    source.unlink()
    resumed = LibraryApp(app.root).resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == result['id']
    assert resumed['worker_terminal_state'] == result['worker_terminal_state']
    assert resumed['worker_message'] == result['worker_message']
    assert len(processes) == 1


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
@pytest.mark.parametrize('stop', ['cancel', 'timeout', 'crash'])
def test_precommit_stop_never_promotes_prepared_intent(tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Uncommitted import')
    source = source_file(tmp_path, kind)
    marker, processes = barrier(monkeypatch, tmp_path, phase='before', stop=stop)
    observed = []

    def cancelled():
        if marker.exists():
            with sqlite3.connect(app.database) as reader:
                observed.append(reader.execute('SELECT count(*) FROM sealed').fetchone()[0])
            return stop == 'cancel'
        return False

    result = app.import_file(workspace, source, cancelled=cancelled, timeout=10)
    assert marker.exists() and all(p.poll() is not None for p in processes)
    assert result['state'] == {'cancel': 'cancelled', 'timeout': 'timed_out', 'crash': 'failed'}[stop]
    assert app.browse(workspace) == ()
    if stop != 'crash':
        assert observed and set(observed) == {0}
    request = worker.load_request(app.root, result['request_id'])
    assert request['publication']['committed'] is False
    assert request['state'] == result['state']


@pytest.mark.parametrize('new_membership', [False, True])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_existing_revision_is_not_itself_a_commit_for_replay(tmp_path, monkeypatch,
                                                           new_membership, phase):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay')
    first = app.create_collection(workspace, 'Already registered')
    second = app.create_collection(workspace, 'New registration')
    source = source_file(tmp_path, 'pdf')
    metadata = dict(title='Original title', work_id='stable-work', edition_id='stable-edition')
    original = app.import_file(workspace, source, collections=(first,), **metadata)
    marker, processes = barrier(monkeypatch, tmp_path, phase=phase, stop='cancel')
    result = app.import_file(workspace, source, collections=(first, second) if new_membership else (first,),
                             cancelled=marker.exists, timeout=10, **metadata)
    committed_change = phase == 'after' and new_membership
    assert result['state'] == ('complete' if committed_change else 'cancelled')
    if committed_change:
        assert result['id'] == original['id'] and result['worker_terminal_state'] == 'cancelled'
    assert bool(app.browse(workspace, collection=second)) == committed_change
    assert len(app.browse(workspace, collection=first)) == 1
    assert processes and all(p.poll() is not None for p in processes)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['txt', 'pdf'])
def test_inline_fresh_commit_is_not_relabelled_by_late_cancellation(tmp_path, monkeypatch, kind):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Fresh in-process commit')
    source = source_file(tmp_path, kind)
    publish = _CatalogPublisher.publish_revision
    committed = []

    def after_commit(publisher, *args, **kwargs):
        publish(publisher, *args, **kwargs)
        with sqlite3.connect(app.database) as reader:
            assert reader.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1
        committed.append(True)

    monkeypatch.setattr(_CatalogPublisher, 'publish_revision', after_commit)
    result = app._import_file(workspace, source, cancelled=lambda: bool(committed))
    assert committed and result['state'] == 'complete'
    assert len(LibraryApp(app.root).browse(workspace)) == 1


@pytest.fixture(scope='module')
def completed_request(tmp_path_factory):
    directory = tmp_path_factory.mktemp('bound-publication')
    app = LibraryApp(directory / 'library')
    workspace = app.create_workspace('Identity checks')
    collection = app.create_collection(workspace, 'Authorized membership')
    result = app.import_file(workspace, source_file(directory, 'pdf'), collections=(collection,))
    assert result['state'] == 'complete'
    return app, worker.load_request(app.root, result['request_id'])


@pytest.mark.parametrize('mutation', ['request', 'job', 'attempt', 'version', 'book',
    'config', 'pages', 'asset', 'memberships', 'result'])
def test_reconciliation_rejects_wrong_request_bound_identity(completed_request, mutation):
    app, original = completed_request
    request = deepcopy(original)
    intent = request['publication']
    if mutation == 'request':
        request['request_id'] = 'f' * 32
    elif mutation == 'job':
        request['job'] = 'e' * 64
    elif mutation == 'attempt':
        intent['attempt_id'] = 'f' * 32
    elif mutation == 'version':
        intent['version'] = True
    elif mutation == 'book':
        request['metadata']['edition_id'] = 'another-book'
    elif mutation == 'config':
        request['config']['dpi'] = 200
    elif mutation == 'pages':
        intent['pages_sha256'] = 'f' * 64
    elif mutation == 'asset':
        request['asset_sha256'] = 'f' * 64
    elif mutation == 'memberships':
        intent['before']['missing_memberships'] = ['unrequested-collection']
    else:
        intent['result']['edition'] = 'another-book'
    with exclusive_writer(app.root), pytest.raises(ValueError):
        app._reconcile_import_publication(request)


def test_stale_completion_is_not_reactivated_after_metadata_edit(completed_request):
    app, request = completed_request
    workspace = request['workspace']
    old_revision = request['job']
    current = app.update_metadata(workspace, request['metadata']['edition_id'], title='New metadata')
    assert current != old_revision
    with exclusive_writer(app.root), pytest.raises(ValueError, match='active revision'):
        app._reconcile_import_publication(request)
    with app.read(workspace) as db:
        assert db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                          (workspace, request['metadata']['edition_id'])).fetchone()[0] == current


def test_receipt_schema_validator_is_pure_and_accepts_legacy_records(completed_request):
    _, original = completed_request
    assert worker.validate_publication_document(deepcopy(original)) == original['publication']
    legacy = deepcopy(original)
    legacy.pop('attempt_id')
    legacy.pop('publication')
    assert worker.validate_publication_document(legacy) is None
    for key, value in [('committed', 1), ('unexpected', 'extra')]:
        changed = deepcopy(original)
        changed['publication'][key] = value
        with pytest.raises(ValueError):
            worker.validate_publication_document(changed)
