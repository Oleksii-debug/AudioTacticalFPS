"""Actual isolated LibraryApp imports stopped on both sides of SQLite commit.

Only a child-side barrier is injected around the real publisher. The supervisor,
parser, catalog transaction, original retention and recovery all execute normally.
"""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


def _source(tmp_path, kind):
    path = tmp_path / ('Оригінал Ω.' + kind)
    if kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, 'Committed railway source')
        canvas.save()
    else:
        from docx import Document
        document = Document()
        document.add_paragraph('Committed railway source — Україна')
        document.save(path)
    return path


def _barrier(monkeypatch, marker, phase, mode, *, corrupt=None):
    original = worker.subprocess.Popen
    code = '''
import sys, runpy, time, os
from pathlib import Path
sys.path[:0] = sys.argv[1:]
import carr_library.application as application
original = application._CatalogPublisher.publish_revision
def blocked(self, *args, **kwargs):
    if PHASE == 'after':
        original(self, *args, **kwargs)
    Path(MARKER).write_text('ready', encoding='utf-8')
    if MODE == 'crash':
        os._exit(21)
    while True:
        time.sleep(0.05)
application._CatalogPublisher.publish_revision = blocked
runpy.run_module('carr_library.ingest.worker', run_name='__main__')
'''.replace('PHASE', repr(phase)).replace('MODE', repr(mode)).replace('MARKER', repr(str(marker)))
    bootstrap = ("import sys,runpy; sys.path[:0]=sys.argv[1:]; "
                 "runpy.run_module('carr_library.ingest.worker',run_name='__main__')")

    def start(command, *args, **kwargs):
        # subprocess.run also calls this shared Popen on Windows for taskkill.
        # Only instrument the exact worker entrypoint; cleanup and unrelated
        # launches must execute through the original subprocess implementation.
        if not isinstance(command, (list, tuple)) or bootstrap not in command:
            return original(command, *args, **kwargs)
        command = list(command)
        assert command[0] == worker.sys.executable
        assert command[1:6] == ['-I', '-B', '-X', 'utf8', '-c']
        assert command[6] == bootstrap
        command[6] = code
        return original(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', start)
    # Advance only the supervisor's clock after the real commit/barrier. This
    # reaches its real timeout branch without relying on machine/parser speed.
    if mode == 'timeout':
        monkeypatch.setattr(worker, 'time', SimpleNamespace(sleep=time.sleep,
            monotonic=lambda: time.monotonic() + (31 if marker.exists() else 0)))
    changed = False

    def cancelled():
        nonlocal changed
        ready = marker.exists()
        if ready and corrupt is not None and not changed:
            corrupt()
            changed = True
        return ready and mode == 'cancel'
    return cancelled


def test_publication_barrier_delegates_real_unrelated_python_process(tmp_path, monkeypatch):
    marker = tmp_path / 'publication-barrier'
    _barrier(monkeypatch, marker, 'after', 'cancel')
    result = worker.subprocess.run(
        [worker.sys.executable, '-I', '-c', "print('unrelated process completed')"],
        capture_output=True, check=True, timeout=5)
    assert result.stdout.strip() == b'unrelated process completed'
    assert result.stderr == b'' and not marker.exists()


def test_publication_barrier_delegates_windows_taskkill_unchanged(tmp_path, monkeypatch):
    calls, result = [], object()

    def original(command, *args, **kwargs):
        calls.append((command, args, kwargs))
        return result

    monkeypatch.setattr(worker.subprocess, 'Popen', original)
    _barrier(monkeypatch, tmp_path / 'publication-barrier', 'after', 'cancel')
    command = ['taskkill', '/PID', '123456', '/T', '/F']
    options = dict(stdout=worker.subprocess.DEVNULL, stderr=worker.subprocess.DEVNULL)
    assert worker.subprocess.Popen(command, **options) is result
    assert calls == [(command, (), options)]
    assert calls[0][0] is command


def test_publication_barrier_still_rejects_nonisolated_worker_bootstrap(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *args, **kwargs: calls.append(args))
    _barrier(monkeypatch, tmp_path / 'publication-barrier', 'after', 'cancel')
    bootstrap = ("import sys,runpy; sys.path[:0]=sys.argv[1:]; "
                 "runpy.run_module('carr_library.ingest.worker',run_name='__main__')")
    command = [worker.sys.executable, '-E', '-B', '-X', 'utf8', '-c', bootstrap, str(tmp_path)]
    with pytest.raises(AssertionError):
        worker.subprocess.Popen(command)
    assert calls == []


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
@pytest.mark.parametrize('mode', ['cancel', 'timeout', 'crash'])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_actual_isolated_worker_reconciles_durable_publication(tmp_path, monkeypatch,
                                                              kind, mode, phase):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Interrupted import')
    collection = app.create_collection(workspace, 'Requested collection')
    source = _source(tmp_path, kind)
    original_bytes = source.read_bytes()
    marker = tmp_path / 'publication-barrier'
    with monkeypatch.context() as patch:
        cancelled = _barrier(patch, marker, phase, mode)
        result = app.import_file(workspace, source, title='Bound source',
            authors=('Synthetic author',), collections=(collection,),
            cancelled=cancelled, timeout=30)
    assert marker.exists(), 'The actual child did not reach its publication barrier.'
    assert result['isolated'] is True
    expected = 'complete' if phase == 'after' else {
        'cancel': 'cancelled', 'timeout': 'timed_out', 'crash': 'failed'}[mode]
    assert result['state'] == expected
    request = worker.load_request(app.root, result['request_id'])
    assert request['state'] == expected and request['result'] == result
    assert source.read_bytes() == original_bytes
    reopened = LibraryApp(app.root)
    if phase == 'after':
        assert result['recovered_after_termination'] is True
        book, = reopened.browse(workspace, collection=collection)
        assert book.title == 'Bound source'
        hit, = reopened.search(workspace, 'Committed railway', collection=collection)
        assert hit.revision == result['id'] and not hit.verified
        with reopened.read(workspace) as db:
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        # A saved, authenticated completion does not need the old external file.
        source.unlink()
        assert reopened.resume_import(workspace, result['request_id'])['id'] == result['id']
    else:
        assert reopened.browse(workspace) == ()
        source.unlink()
        resumed = reopened.resume_import(workspace, result['request_id'])
        assert resumed['state'] == 'complete'
        assert len(reopened.browse(workspace, collection=collection)) == 1


def test_preexisting_publication_does_not_complete_a_cancelled_collection_replay(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay')
    first = app.create_collection(workspace, 'First')
    additional = app.create_collection(workspace, 'Not yet committed')
    source = _source(tmp_path, 'pdf')
    metadata = dict(title='Replay source', authors=('Author',),
                    work_id='same-work', edition_id='same-edition')
    committed = app.import_file(workspace, source, collections=(first,), **metadata)
    assert committed['state'] == 'complete'
    replay = app.import_file(workspace, source, collections=(additional,),
                             cancelled=lambda: True, **metadata)
    assert replay['state'] == 'cancelled'
    assert 'recovered_after_termination' not in replay
    assert app.browse(workspace, collection=additional) == ()
    assert len(app.browse(workspace, collection=first)) == 1


@pytest.mark.parametrize('corruption', [
    'request', 'original', 'checkpoint', 'checkpoint_shape', 'checkpoint_provenance'])
def test_interrupted_commit_requires_intact_request_source_and_checkpoints(
        tmp_path, monkeypatch, corruption):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Integrity')
    source = _source(tmp_path, 'pdf')
    marker = tmp_path / 'publication-barrier'

    def corrupt():
        path, = (app.root / 'import-requests').glob('*.json')
        request = worker.load_request(app.root, path.stem)
        if corruption == 'request':
            request['metadata']['title'] = 'Foreign changed title'
            worker._store(app.root, path.stem, request)
        elif corruption == 'original':
            app.assets.resolve(request['asset_sha256']).write_bytes(b'changed retained bytes')
        else:
            # The child is paused inside its journal transaction, so edit a
            # checkpoint only after the supervisor has stopped/reaped it.
            pass

    original_stop = worker._stop

    def stopped(process):
        original_stop(process)
        if corruption.startswith('checkpoint'):
            with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
                if corruption == 'checkpoint':
                    db.execute('UPDATE checkpoint SET digest=?', ('0' * 64,))
                else:
                    if corruption == 'checkpoint_shape':
                        payload = '[]'
                    else:
                        item = json.loads(db.execute('SELECT payload FROM checkpoint').fetchone()[0])
                        item['provenance'] = []
                        payload = worker.canonical(item)
                    db.execute('UPDATE checkpoint SET payload=?,digest=?',
                               (payload, sha256(payload.encode('utf-8')).hexdigest()))
                db.commit()

    with monkeypatch.context() as patch:
        cancelled = _barrier(patch, marker, 'after', 'cancel', corrupt=corrupt)
        patch.setattr(worker, '_stop', stopped)
        result = app.import_file(workspace, source, cancelled=cancelled, timeout=30)
    assert marker.exists()
    assert result['state'] == 'failed'
    assert 'could not be verified' in result['message']
    assert 'recovered_after_termination' not in result
    # Failure reports uncertainty; it neither rolls back nor hides durable data.
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision JOIN sealed '
                          'ON sealed.revision=revision.id').fetchone()[0] == 1
