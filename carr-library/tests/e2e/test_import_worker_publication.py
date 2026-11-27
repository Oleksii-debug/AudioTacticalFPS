"""Actual child-process races around the catalog's durable SQLite commit.

Only scheduling barriers are injected into the trusted child bootstrap. The
application, catalog, source retention, extraction, publication and supervisor
are the real production implementations, with synthetic local documents.
"""
from pathlib import Path
from contextlib import closing
import shutil
import sqlite3
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker


def _source(root, kind):
    path = root / ('Джерело.' + kind)
    if kind == 'txt':
        path.write_bytes('Railway history.\r\nУкраїнський текст.\fSecond source segment.'.encode('utf-8'))
    elif kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, 'Railway history from the original source.')
        canvas.save()
    else:
        from docx import Document
        document = Document()
        document.add_paragraph('Railway history. Український текст.')
        document.save(path)
    return path


def _metadata(collections=()):
    return dict(title='Exact source', authors=['Synthetic author'], language='uk',
                edition='First edition', volume=None, collections=list(collections),
                work_id='source-work', edition_id='source-edition')


def _barrier(monkeypatch, marker, *, boundary, exit_after=False):
    """Pause the real child before/after commit, without replacing its result."""
    popen = subprocess.Popen
    code = f'''
import os, time
from pathlib import Path
from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.ingest import worker as _worker
def _pause():
    Path({str(marker)!r}).write_text('observed', encoding='utf-8')
    if {exit_after!r}:
        os._exit(7)
    time.sleep(20)
'''
    if boundary == 'before':
        code += '''
_candidate = _worker.record_publication_candidate
def _before(*args, **kwargs):
    _candidate(*args, **kwargs)
    _pause()
_worker.record_publication_candidate = _before
'''
    elif boundary == 'after':
        code += '''
_publish = _CatalogPublisher.publish_revision
def _after(self, *args, **kwargs):
    _publish(self, *args, **kwargs)
    _pause()
_CatalogPublisher.publish_revision = _after
'''
    else:
        assert boundary == 'replay'
        code += '''
_import = LibraryApp._import_file
def _replay(self, *args, **kwargs):
    _pause()
    return _import(self, *args, **kwargs)
LibraryApp._import_file = _replay
'''

    def launch(command, **options):
        # subprocess.run also reaches this global Popen on Windows for taskkill.
        # Only inject into the exact trusted worker bootstrap, never cleanup or
        # another native subprocess used by the real producer.
        bootstrap = ("import sys,runpy; sys.path[:0]=sys.argv[1:]; "
                     "runpy.run_module('carr_library.ingest.worker',run_name='__main__')")
        prefix = [sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap]
        if not isinstance(command, (list, tuple)) or list(command[:7]) != prefix:
            return popen(command, **options)
        command = list(command)
        command[6] = ('import sys,runpy\nsys.path[:0]=sys.argv[1:]\n' + code
                      + "\nrunpy.run_module('carr_library.ingest.worker',run_name='__main__')")
        return popen(command, **options)

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)


@pytest.mark.parametrize('kind', ('txt', 'pdf', 'docx'))
@pytest.mark.parametrize('termination', ('cancel', 'crash', 'timeout'))
def test_stopped_child_reports_verified_fresh_commit(tmp_path, monkeypatch, kind, termination):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Publication')
    collection = app.create_collection(workspace, 'Published collection')
    source = _source(tmp_path, kind)
    original = source.read_bytes()
    marker = tmp_path / 'after-commit'
    _barrier(monkeypatch, marker, boundary='after', exit_after=termination == 'crash')
    result = worker.run_request(app.root, workspace, source, _metadata((collection,)), Config(),
        cancelled=lambda: termination == 'cancel' and marker.exists(),
        timeout=3.0 if termination == 'timeout' else 15.0)
    assert marker.exists(), result
    assert result['state'] == 'complete', result
    assert result['publication_recovered'] is True
    assert result['verified'] is (kind == 'txt')
    record = worker.load_request(app.root, result['request_id'])
    assert record['state'] == 'complete' and record['result'] == result
    assert app.assets.resolve(result['asset']).read_bytes() == original
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1
        assert db.execute('SELECT revision FROM active').fetchone()[0] == result['id']
        assert db.execute('SELECT count(*) FROM catalog_membership').fetchone()[0] == 1
        assert tuple(db.execute('SELECT revision,request_id,attempt,sha256 '
                                'FROM import_publication').fetchone()) == (
            result['id'], record['request_id'], record['attempt'],
            record['publication_candidate']['sha256'])
    assert app.browse(workspace, collection=collection)[0].title == 'Exact source'
    # Recovery uses the durable publication, even though the extraction journal
    # may still show pending because the child stopped before its own commit.
    assert source.read_bytes() == original
    assert worker.run_request(app.root, workspace, source, {}, Config(),
                              request_id=result['request_id']) == result


@pytest.mark.parametrize('kind', ('pdf', 'docx'))
@pytest.mark.parametrize('termination', ('cancel', 'crash', 'timeout'))
def test_stopped_child_before_commit_never_reports_completion(tmp_path, monkeypatch, kind, termination):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Rollback')
    source = _source(tmp_path, kind)
    marker = tmp_path / 'before-commit'
    _barrier(monkeypatch, marker, boundary='before', exit_after=termination == 'crash')
    result = worker.run_request(app.root, workspace, source, _metadata(), Config(),
        cancelled=lambda: termination == 'cancel' and marker.exists(),
        timeout=3.0 if termination == 'timeout' else 15.0)
    assert marker.exists(), result
    assert result['state'] == {'cancel': 'cancelled', 'crash': 'failed', 'timeout': 'timed_out'}[termination]
    assert app.browse(workspace) == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM catalog_work').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
    record = worker.load_request(app.root, result['request_id'])
    assert record['publication_candidate'] is not None
    assert record['state'] == result['state']


@pytest.mark.parametrize('kind', ('txt', 'pdf', 'docx'))
@pytest.mark.parametrize('competitor_kind', ('direct', 'borrowed-request', 'second-request', 'forged-sidecar'))
def test_competing_commit_after_kill_does_not_complete_stopped_request(
        tmp_path, monkeypatch, kind, competitor_kind):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Competing publication after rollback')
    source = _source(tmp_path, kind)
    planned = worker.prepare_request(app.root, workspace, source, _metadata(), Config())
    marker = tmp_path / 'before-commit'
    real_popen, real_stop = worker.subprocess.Popen, worker._stop
    _barrier(monkeypatch, marker, boundary='before')
    competing = []

    def stop_then_publish(process):
        real_stop(process)
        if marker.exists() and not competing:
            # The cancelled child has released the writer lock. Complete a
            # different import before its parent can reconcile the hint.
            competing.append(None)
            with monkeypatch.context() as clean_launch:
                clean_launch.setattr(worker.subprocess, 'Popen', real_popen)
                if competitor_kind in ('second-request', 'forged-sidecar'):
                    competing[0] = worker.run_request(app.root, workspace, source,
                        _metadata(), Config(), timeout=15.0)
                else:
                    options = ({'_request_id': planned['request_id']}
                               if competitor_kind == 'borrowed-request' else {})
                    competing[0] = app._import_file(workspace, source, config=Config(),
                                                   **_metadata(), **options)
            if competitor_kind == 'forged-sidecar':
                stopped = worker.load_request(app.root, planned['request_id'])
                other = worker.load_request(app.root, competing[0]['request_id'])
                # A copied completion hint cannot transfer the ownership of a
                # committed database receipt, even with the current attempt ID.
                stopped['publication_candidate'] = dict(other['publication_candidate'],
                                                       attempt=stopped['attempt'])
                worker._store(app.root, planned['request_id'], stopped)

    monkeypatch.setattr(worker, '_stop', stop_then_publish)
    result = worker.run_request(app.root, workspace, source, _metadata(), Config(),
                                cancelled=marker.exists, timeout=15.0,
                                request_id=planned['request_id'])
    assert marker.exists() and competing[0]['state'] == 'complete', competing
    assert result['state'] == 'cancelled' and 'publication_recovered' not in result, result
    request = worker.load_request(app.root, result['request_id'])
    # The source ID is deliberately unchanged; ownership comes from the
    # transaction receipt, never a second revision for an identical import.
    assert request['publication_candidate']['revision'] == competing[0]['id']
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        receipt = db.execute('SELECT request_id,attempt FROM import_publication').fetchone()
        if competitor_kind in ('second-request', 'forged-sidecar'):
            other = worker.load_request(app.root, competing[0]['request_id'])
            assert tuple(receipt) == (other['request_id'], other['attempt'])
            assert receipt['request_id'] != result['request_id']
        else:
            assert receipt is None


@pytest.mark.parametrize('kind', ('txt', 'pdf', 'docx'))
@pytest.mark.parametrize('publisher', ('direct', 'request'))
def test_cancelled_replay_cannot_claim_preexisting_publication(tmp_path, monkeypatch, kind, publisher):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Replay')
    source = _source(tmp_path, kind)
    first = (worker.run_request(app.root, workspace, source, _metadata(), Config())
             if publisher == 'request' else app._import_file(workspace, source, **_metadata()))
    assert first['state'] == 'complete'
    with app.read(workspace) as db:
        before = tuple(tuple(row) for row in db.execute('SELECT * FROM import_publication'))
    requested = app.create_collection(workspace, 'Uncommitted membership')
    marker = tmp_path / 'before-replay'
    _barrier(monkeypatch, marker, boundary='replay')
    result = worker.run_request(app.root, workspace, source, _metadata((requested,)), Config(),
                                cancelled=marker.exists, timeout=15.0)
    assert marker.exists() and result['state'] == 'cancelled', result
    assert app.browse(workspace, collection=requested) == ()
    assert worker.load_request(app.root, result['request_id'])['publication_candidate'] is None
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_membership').fetchone()[0] == 0
        assert tuple(tuple(row) for row in db.execute('SELECT * FROM import_publication')) == before


def test_replay_preserves_original_receipt_and_sql_replacement_is_rejected(tmp_path):
    from carr_library.storage.revisions import Storage

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Immutable publication ownership')
    source = _source(tmp_path, 'txt')
    first = worker.run_request(app.root, workspace, source, _metadata(), Config())
    assert first['state'] == 'complete'
    with app.read(workspace) as db:
        before = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    replay = worker.run_request(app.root, workspace, source, _metadata(), Config())
    assert replay['state'] == 'complete' and replay['id'] == first['id']
    assert replay['request_id'] != first['request_id']
    with closing(Storage(app.database)) as storage:
        for sql, values in (
                ('UPDATE import_publication SET attempt=?', ('f' * 32,)),
                ('DELETE FROM import_publication', ()),
                ('INSERT OR REPLACE INTO import_publication VALUES(?,?,?,?)', before),
                ('INSERT INTO import_publication VALUES(?,?,?,?)',
                 (None, 'e' * 32, 'f' * 32, 'a' * 64)),
                ('INSERT INTO import_publication VALUES(?,?,?,?)',
                 ('f' * 64, 'e' * 32, 'f' * 32, 'a' * 64))):
            with pytest.raises(sqlite3.IntegrityError), storage.transaction():
                storage.db.execute(sql, values)
        assert tuple(storage.db.execute('SELECT * FROM import_publication').fetchone()) == before
        assert storage.db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
        storage.check()


@pytest.mark.parametrize('tampering', ('candidate', 'source', 'workspace', 'metadata', 'config', 'attempt'))
def test_recovery_rechecks_candidate_source_and_authorization(tmp_path, monkeypatch, tampering):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Recheck')
    source = _source(tmp_path, 'pdf')
    marker = tmp_path / 'after-commit'
    _barrier(monkeypatch, marker, boundary='after')
    changed = False

    def stop():
        nonlocal changed
        if not marker.exists():
            return False
        if not changed:
            request_path, = (app.root / 'import-requests').glob('*.json')
            request_id = request_path.stem
            record = worker.load_request(app.root, request_id)
            if tampering == 'candidate':
                record['publication_candidate']['sha256'] = '0' * 64
                worker._store(app.root, request_id, record)
            elif tampering == 'source':
                app.assets.resolve(record['asset_sha256']).write_bytes(b'Changed original bytes')
            elif tampering in ('metadata', 'config', 'attempt'):
                if tampering == 'metadata':
                    record['metadata']['title'] = 'Changed requested title'
                elif tampering == 'config':
                    record['config']['dpi'] = 72
                else:
                    record['attempt'] = 'f' * 32
                worker._store(app.root, request_id, record)
            else:
                # Use a separate connection: the child is paused after the
                # publication transaction, so this is a real authorization change.
                with sqlite3.connect(app.database) as db:
                    db.execute('UPDATE workspace_meta SET archived=1 WHERE workspace=?', (workspace,))
            changed = True
        return True

    result = worker.run_request(app.root, workspace, source, _metadata(), Config(),
                                cancelled=stop, timeout=15.0)
    assert changed and result['state'] == 'failed', result
    assert 'could not verify' in result['message']
    assert not result.get('publication_recovered')


def test_resumed_attempt_cannot_reuse_old_candidate_when_cancelled_before_launch(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Attempt identity')
    source = _source(tmp_path, 'pdf')
    first = worker.run_request(app.root, workspace, source, _metadata(), Config())
    record = worker.load_request(app.root, first['request_id'])
    assert record['publication_candidate'] is not None
    old_attempt = record['attempt']
    record['state'] = 'running'
    record.pop('result')
    worker._store(app.root, first['request_id'], record)
    result = worker.run_request(app.root, workspace, source, {}, Config(),
        request_id=first['request_id'], cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    current = worker.load_request(app.root, first['request_id'])
    assert current['attempt'] != old_attempt and current['publication_candidate'] is None
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_docx_publication_keeps_existing_logical_record_capacity(tmp_path, monkeypatch):
    from docx import Document

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Long logical document')
    source = tmp_path / 'many-paragraphs.docx'
    document = Document()
    for index in range(1001):
        document.add_paragraph(f'Railway source paragraph {index + 1}.')
    document.save(source)
    marker = tmp_path / 'after-commit'
    _barrier(monkeypatch, marker, boundary='after')
    result = worker.run_request(app.root, workspace, source, _metadata(), Config(),
                                cancelled=marker.exists, timeout=15.0)
    assert marker.exists() and result['state'] == 'complete', result
    assert result['completed'] == 1001 and result['publication_recovered'] is True
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == 1001


def test_recovered_commit_with_pending_extraction_journal_survives_backup(tmp_path, monkeypatch):
    from carr_library.ingest.writer_guard import exclusive_writer
    from carr_library.pending_backup import snapshot_pending, restore_pending
    from carr_library.storage.revisions import Storage

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Recovered publication')
    source = _source(tmp_path, 'pdf')
    marker = tmp_path / 'after-commit'
    _barrier(monkeypatch, marker, boundary='after')
    result = worker.run_request(app.root, workspace, source, _metadata(), Config(),
                                cancelled=marker.exists, timeout=15.0)
    assert result['state'] == 'complete' and result['publication_recovered'] is True
    with app.read(workspace) as db:
        receipt = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        assert db.execute('SELECT state FROM job WHERE id=?', (result['id'],)).fetchone()[0] == 'pending'
    backup = tmp_path / 'backup'
    backup.mkdir()
    with exclusive_writer(app.root), closing(Storage(app.database)) as storage:
        storage.backup(backup / 'library.sqlite')
        pending = snapshot_pending(app, backup / 'pending')
    source.unlink()
    stage, destination = tmp_path / 'restore-stage', tmp_path / 'restored'
    stage.mkdir()
    shutil.copyfile(backup / 'library.sqlite', stage / 'library.sqlite')
    restore_pending(backup / 'pending', stage, destination, pending)
    stage.rename(destination)
    restored = LibraryApp(destination)
    record = worker.load_request(restored.root, result['request_id'])
    assert 'attempt' not in record and 'publication_candidate' not in record
    assert restored.resume_import(workspace, result['request_id']) == result
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt
    assert restored.search(workspace, 'Railway')[0].quote == 'Railway'


@pytest.mark.parametrize('attempt', (None, '', True, 'not-an-attempt', 'b' * 32))
def test_real_child_rejects_missing_or_foreign_persisted_attempt(tmp_path, attempt):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Trusted stdin identity')
    source = _source(tmp_path, 'txt')
    planned = worker.prepare_request(app.root, workspace, source, _metadata(), Config())
    worker.record_progress(app.root, planned['request_id'], state='running',
                           attempt='a' * 32, publication_candidate=None)
    message = dict(data_dir=str(app.root), request_id=planned['request_id'], timeout=15.0)
    if attempt is not None:
        message['attempt'] = attempt
    result = worker._invoke(message, timeout=15.0)
    assert result['state'] == 'failed', result
    assert 'attempt' in result['message'] or 'request schema' in result['message']
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
    assert not (app.root / 'imports.sqlite').exists()
    assert worker.load_request(app.root, planned['request_id'])['publication_candidate'] is None


def test_termination_error_never_reconciles_even_an_owned_commit(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Confirmed child termination')
    source = _source(tmp_path, 'txt')
    marker = tmp_path / 'after-commit'
    _barrier(monkeypatch, marker, boundary='after')
    real_stop = worker._stop

    def unconfirmed(process):
        real_stop(process)  # Clean up the real fixture child before raising.
        raise subprocess.TimeoutExpired('fixture could not confirm termination', 1)

    def forbidden(*args):
        pytest.fail('An unconfirmed child termination reached publication reconciliation.')

    monkeypatch.setattr(worker, '_stop', unconfirmed)
    monkeypatch.setattr(worker, '_reconcile_publication', forbidden)
    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_request(app.root, workspace, source, _metadata(), Config(),
                           cancelled=marker.exists, timeout=15.0)
    assert marker.exists()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1


def test_logical_publication_digest_survives_full_checked_restore(tmp_path):
    from carr_library.library_backup import create_backup, restore_backup

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Stable logical request')
    source = _source(tmp_path, 'txt')
    result = worker.run_request(app.root, workspace, source, _metadata(), Config())
    assert result['state'] == 'complete'
    record = worker.load_request(app.root, result['request_id'])
    with app.read(workspace) as db:
        before = worker._publication_state(db, record, result['id'])
        receipt = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    source.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'Відновлена бібліотека', digest))
    rebound = worker.load_request(restored.root, result['request_id'])
    assert rebound['source'] != record['source'] and rebound['data_dir'] != record['data_dir']
    assert 'attempt' not in rebound and 'publication_candidate' not in rebound
    with restored.read(workspace) as db:
        assert worker._publication_state(db, rebound, result['id']) == before
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt
    # Mutable progress is neither part of the original input nor a completion
    # authority. Full source and catalog attestation above still matches.
    changed_progress = dict(rebound, state='running', result=None,
                            attempt='c' * 32, publication_candidate=None)
    assert worker._request_identity(changed_progress) == worker._request_identity(rebound)
    changed_input = dict(rebound, config=dict(rebound['config'], dpi=72))
    with restored.read(workspace) as db:
        assert worker._publication_state(db, changed_input, result['id'])['sha256'] != before['sha256']
    assert restored.resume_import(workspace, result['request_id']) == result
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize(('suffix', 'raw'), [
    ('html', b'<!doctype html><html><body><p>Railway original HTML.</p></body></html>'),
    ('md', b'# Railway original Markdown\n\nSeparate source paragraph.\n'),
    ('eml', b'From: author@example.invalid\r\nTo: reader@example.invalid\r\n'
            b'Subject: Railway original message\r\nMIME-Version: 1.0\r\n'
            b'Content-Type: text/plain; charset=utf-8\r\n\r\nRailway original email.\r\n'),
])
def test_extended_document_commit_uses_same_owned_receipt(tmp_path, monkeypatch, suffix, raw):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Retained document formats')
    source = tmp_path / ('Джерело.' + suffix)
    source.write_bytes(raw)
    marker = tmp_path / 'after-commit'
    _barrier(monkeypatch, marker, boundary='after')
    result = worker.run_request(app.root, workspace, source, _metadata(), Config(),
                                cancelled=marker.exists, timeout=15.0)
    assert marker.exists() and result['state'] == 'complete', result
    assert result['publication_recovered'] is True and result['verified'] is False
    assert app.assets.resolve(result['asset']).read_bytes() == raw
    record = worker.load_request(app.root, result['request_id'])
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        receipt = db.execute('SELECT request_id,attempt FROM import_publication').fetchone()
        assert tuple(receipt) == (result['request_id'], record['attempt'])
    assert app.search(workspace, 'Railway')
    assert source.read_bytes() == raw
