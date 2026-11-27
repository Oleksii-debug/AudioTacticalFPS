"""Real child processes prove commit ownership, independently of publication timing."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker
from carr_library.library_backup import restore_backup

# CI also runs these files outside the source checkout with importlib mode.
# Read the adjacent checked-in harness without adding source roots to sys.path.
_helpers = runpy.run_path(str(Path(__file__).with_name('test_worker_publication_boundary.py')))
capture_attempt, publication_barrier, source_file = (
    _helpers[name] for name in ('capture_attempt', 'publication_barrier', 'source_file'))


def test_other_language_publication_cannot_recover_a_failed_request(tmp_path, monkeypatch):
    # Independent release-review reproduction, retained without weakening it.
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Independent publication attribution')
    source = source_file(tmp_path, 'docx')
    common = dict(title='Same source', work_id=uuid4().hex, edition_id=uuid4().hex)
    invoke = worker._invoke
    actual_child = []
    def other_writer_then_child(message, **options):
        other = app._import_file(workspace, source, language='en', **common)
        assert other['state'] == 'complete'
        result = invoke(message, **options)
        actual_child.append(result)
        return result
    monkeypatch.setattr(worker, '_invoke', other_writer_then_child)
    result = app.import_file(workspace, source, language='uk', **common)
    assert actual_child[0]['state'] == 'failed'
    assert 'edition identity conflict' in actual_child[0]['message']
    with app.read(workspace) as db:
        assert db.execute('SELECT language FROM catalog_edition').fetchone()[0] == 'en'
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
    assert result['state'] == 'failed', result
    assert 'recovered_publication' not in result


def test_other_exact_publication_cannot_recover_a_cancelled_replay(tmp_path, monkeypatch):
    # Independent release-review reproduction, retained without weakening it.
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Concurrent exact replay')
    source = source_file(tmp_path, 'docx')
    marker = tmp_path / 'uncommitted-replay'
    metadata = dict(title='Same source', work_id=uuid4().hex, edition_id=uuid4().hex, language='uk')
    publication_barrier(monkeypatch, marker, before=True)
    invoke = worker._invoke
    actual_child = []
    def other_writer_then_child(message, **options):
        other = app._import_file(workspace, source, **metadata)
        assert other['state'] == 'complete'
        result = invoke(message, **options)
        actual_child.append(result)
        return result
    monkeypatch.setattr(worker, '_invoke', other_writer_then_child)
    result = app.import_file(workspace, source, cancelled=marker.exists, timeout=30, **metadata)
    assert marker.is_file()
    assert actual_child[0]['state'] == 'cancelled'
    assert result['state'] == 'cancelled', result
    assert 'recovered_publication' not in result
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0


def test_fresh_attempt_of_same_request_cannot_borrow_prior_receipt(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Fresh attempt')
    launches = capture_attempt(monkeypatch)
    source = source_file(tmp_path, 'docx')
    first = app.import_file(workspace, source)
    assert first['state'] == 'complete'
    initial, first_attempt = launches[0]
    assert worker._published_after_stop(app, initial, first_attempt)['state'] == 'complete'
    # Reproduce losing the parent-side result after the actual child committed.
    worker.record_progress(app.root, first['request_id'], state='running', result=None)
    marker = tmp_path / 'replay'
    publication_barrier(monkeypatch, marker, before=True)
    result = app.resume_import(workspace, first['request_id'], cancelled=marker.exists, timeout=30)
    assert marker.is_file() and result['state'] == 'cancelled'
    assert 'recovered_publication' not in result
    resumed, second_attempt = launches[1]
    assert first_attempt != second_attempt
    assert worker._published_after_stop(app, resumed, second_attempt) is None
    with app.read(workspace) as db:
        row, = db.execute('SELECT request_id,attempt,revision FROM import_publication').fetchall()
        assert tuple(row) == (first['request_id'], first_attempt, first['id'])


def test_native_exit_after_receipt_and_revision_staging_rolls_back_everything(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Uncommitted receipt')
    marker = tmp_path / 'staged'
    popen = worker.subprocess.Popen
    def launch(command, *args, **kwargs):
        if isinstance(command, list) and '-c' in command:
            index = command.index('-c') + 1
            anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"
            if anchor in command[index]:
                body = '\n'.join([
                    'def stop_after_stage(*args, **kwargs):',
                    '    result = original(*args, **kwargs)',
                    '    db = args[2]',
                    '    assert db.in_transaction',
                    "    assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1",
                    "    assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1",
                    '    Path(' + repr(str(marker)) + ").write_text('uncommitted receipt and source')",
                    '    os._exit(23)',
                ])
                hook = ('import os; from pathlib import Path; '
                    'from carr_library.ingest import worker as product; '
                    'original=product.record_publication_candidate; exec(' + repr(body) + '); '
                    'product.record_publication_candidate=stop_after_stage; ')
                command = list(command)
                command[index] = command[index].replace(anchor, hook + anchor)
        return popen(command, *args, **kwargs)
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    result = app.import_file(workspace, source_file(tmp_path, 'docx'))
    assert marker.is_file() and result['state'] == 'failed'
    assert 'recovered_publication' not in result
    reopened = LibraryApp(app.root)
    with reopened.read(workspace) as db:
        for table in ('import_publication', 'revision', 'sealed', 'page', 'catalog_edition', 'catalog_work'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


@pytest.mark.parametrize('operation', ('update', 'delete', 'replace', 'replay'))
def test_receipt_is_immutable_and_cannot_be_added_to_a_sealed_revision(tmp_path, operation):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Immutable receipt')
    result = app.import_file(workspace, source_file(tmp_path, 'docx'))
    with app.read(workspace) as db:
        before = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    with pytest.raises(sqlite3.IntegrityError):
        with app.write(workspace) as db:
            if operation == 'update':
                db.execute('UPDATE import_publication SET attempt=?', (uuid4().hex,))
            elif operation == 'delete':
                db.execute('DELETE FROM import_publication')
            else:
                values = list(before)
                if operation == 'replay':
                    values[1], values[2] = uuid4().hex, uuid4().hex
                db.execute(('INSERT OR REPLACE' if operation == 'replace' else 'INSERT') +
                    ' INTO import_publication VALUES(?,?,?,?)', values)
    with app.read(workspace) as db:
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == before
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    assert result['state'] == 'complete'


def test_receipt_and_source_identity_survive_restore_without_reusing_the_attempt(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Portable receipt')
    source = source_file(tmp_path, 'docx')
    launches = capture_attempt(monkeypatch)
    result = app.import_file(workspace, source, language='uk', authors=('Автор',))
    initial, attempt = launches[0]
    with app.read(workspace) as db:
        before = tuple(db.execute('SELECT * FROM import_publication').fetchone())
        assert db.execute('SELECT language FROM catalog_edition').fetchone()[0] == 'uk'
    source_digest = worker.load_request(app.root, result['request_id'])['asset_sha256']
    assert all(str(app.root) not in value and str(source) not in value for value in before)
    assert before[:3] == (result['id'], result['request_id'], attempt)
    worker.record_progress(app.root, result['request_id'], state='running', result=None)
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    restored_initial = deepcopy(initial)
    request = worker.load_request(restored.root, result['request_id'])
    restored_initial.update(data_dir=str(restored.root), source=request['source'])
    with restored.read(workspace) as db:
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == before
    assert restored.assets.resolve(source_digest).read_bytes() == app.assets.resolve(source_digest).read_bytes()
    # Canonical receipt digests bind the original launch paths. The database
    # row and source survive unchanged, but a relocated request cannot reuse
    # its old attempt/candidate as a fresh publication authority.
    assert 'attempt' not in request and 'publication_candidate' not in request
    assert worker._published_after_stop(restored, restored_initial, attempt) is None
    marker = tmp_path / 'restored-replay'
    publication_barrier(monkeypatch, marker, before=True)
    resumed = restored.resume_import(workspace, result['request_id'], cancelled=marker.exists, timeout=30)
    assert marker.exists() and resumed['state'] == 'cancelled'
    assert launches[-1][1] != attempt and 'recovered_publication' not in resumed


def test_superseded_txt_recovery_does_not_claim_verified_completion(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Historical completion')
    launches = capture_attempt(monkeypatch)
    batch = app.plan_import_batch(workspace, [source_file(tmp_path, 'txt')])
    result = app.run_import_batch(workspace, batch['batch_id'])['rows'][0]['result']
    initial, attempt = launches[0]
    assert result['state'] == 'complete'
    app.update_metadata(workspace, initial['metadata']['edition_id'], title='Updated metadata')
    recovered = worker._published_after_stop(app, initial, attempt)
    assert recovered['state'] == 'superseded' and recovered['verified'] is False


@pytest.mark.parametrize('stop', ('cancel', 'crash'))
@pytest.mark.parametrize('other_language', ('uk', 'en'))
def test_after_child_stop_other_publisher_cannot_complete_request(tmp_path, monkeypatch, stop, other_language):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('After stopped worker publication attribution')
    source = source_file(tmp_path, 'docx')
    metadata = dict(title='Same source', work_id=uuid4().hex, edition_id=uuid4().hex)
    marker = tmp_path / 'before-commit'
    if stop == 'cancel':
        publication_barrier(monkeypatch, marker, before=True)
    else:
        popen = worker.subprocess.Popen
        def crash_before_commit(command, *args, **kwargs):
            if isinstance(command, list) and '-c' in command:
                index = command.index('-c') + 1
                anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"
                if anchor in command[index]:
                    body = '\n'.join([
                        'def crash(*args, **kwargs):',
                        '    result = original(*args, **kwargs)',
                        '    Path(' + repr(str(marker)) + ').write_text("uncommitted", encoding="utf-8")',
                        '    os._exit(17)',
                    ])
                    hook = ('import os; from pathlib import Path; from carr_library import application as product; '
                            'original=product.register_batch; exec(' + repr(body) + '); product.register_batch=crash; ')
                    command = list(command)
                    command[index] = command[index].replace(anchor, hook + anchor)
            return popen(command, *args, **kwargs)
        monkeypatch.setattr(worker.subprocess, 'Popen', crash_before_commit)
    invoke = worker._invoke
    actual_child = []
    def stopped_child_then_other_writer(message, **options):
        stopped = invoke(message, **options)
        actual_child.append(stopped)
        assert marker.is_file(), stopped
        assert stopped['state'] == ('cancelled' if stop == 'cancel' else 'failed'), stopped
        with app.read(workspace) as db:
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        # This independent real publisher acquires the released writer lock;
        # its commit precedes the original parent's reconciliation read.
        other = app._import_file(workspace, source, language=other_language, **metadata)
        assert other['state'] == 'complete', other
        return stopped
    monkeypatch.setattr(worker, '_invoke', stopped_child_then_other_writer)
    result = app.import_file(workspace, source, language='uk',
                             cancelled=lambda: stop == 'cancel' and marker.exists(), timeout=30, **metadata)
    assert len(actual_child) == 1
    with app.read(workspace) as db:
        assert db.execute('SELECT language FROM catalog_edition').fetchone()[0] == other_language
    assert result['state'] == actual_child[0]['state'], result
    assert 'recovered_publication' not in result, result
