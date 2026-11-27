"""Actual isolated imports interrupted on either side of the SQLite commit.

Only test-owned synchronization hooks are added to the trusted child bootstrap.
The real LibraryApp, parser, publisher, request files, SQLite transactions and
supervisor still run. No fake child receipt stands in for a publication.
"""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


def _source(path):
    if path.suffix == '.pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, 'Synthetic railway source after durable commit.')
        canvas.save()
    else:
        from docx import Document
        document = Document()
        document.add_paragraph('Synthetic railway source after durable commit.')
        document.save(path)
    return path


def _child_barrier(monkeypatch, marker, phase, *, change=None):
    """Hold a real child at a production boundary, without touching source files."""
    original = worker.subprocess.Popen
    script = '''
import sys
sys.path[:0] = sys.argv[1:]
import os, time
from pathlib import Path
from carr_library import application
from carr_library.ingest import worker
from carr_library.storage.revisions import Storage
marker = Path(MARKER)
phase = PHASE
change = CHANGE
original_publish = application._CatalogPublisher.publish_revision
original_stage = Storage.stage_revision
original_check = application._CatalogPublisher.check
original_import = application.LibraryApp._import_file

def wait_at_boundary():
    marker.write_text('ready', encoding='utf-8')
    if phase == 'crash_after_commit':
        os._exit(23)
    time.sleep(30)

def publish(self, *args, **kwargs):
    value = original_publish(self, *args, **kwargs)
    if phase in ('after_commit', 'crash_after_commit'):
        if change:
            paths = tuple((marker.parent / 'library' / 'import-requests').glob('*.json'))
            assert len(paths) == 1
            request = worker.load_request(marker.parent / 'library', paths[0].stem)
            intent = request.get('result')
            if not isinstance(intent, dict):
                raise AssertionError('fresh publication intent is missing')
            if change[0] == 'retained_source':
                source = next((marker.parent / 'library' / 'originals').glob(request['asset_sha256'] + '.*'))
                data = source.read_bytes()
                source.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            elif change[0] == 'request_title':
                request['metadata']['title'] = change[1]
            else:
                intent[change[0]] = change[1]
            worker._store(marker.parent / 'library', paths[0].stem, request)
        wait_at_boundary()
    return value

def stage(self, *args, **kwargs):
    value = original_stage(self, *args, **kwargs)
    if phase == 'before_commit':
        wait_at_boundary()
    return value

def check(self):
    value = original_check(self)
    if phase == 'replay' and not getattr(self, 'published_here', False):
        wait_at_boundary()
    return value

def import_and_pause(self, *args, **kwargs):
    value = original_import(self, *args, **kwargs)
    if phase == 'quarantined':
        assert value['state'] == 'quarantined'
        wait_at_boundary()
    return value

application._CatalogPublisher.publish_revision = publish
Storage.stage_revision = stage
application._CatalogPublisher.check = check
if phase == 'quarantined':
    # Explicit policy injection only: the real native PDF/parser checkpoint is
    # retained, but this test does not pretend to execute a scan/OCR engine.
    worker.RuntimeBoundIngest._eligible = lambda self, record: False
    application.LibraryApp._import_file = import_and_pause
raise SystemExit(worker.main())
'''.replace('MARKER', repr(str(marker))).replace('PHASE', repr(phase)).replace('CHANGE', repr(change))

    def launch(command, *args, **kwargs):
        # subprocess is shared by the real supervisor and Windows taskkill.
        # Only the trusted ingest bootstrap receives this test-owned barrier.
        if not isinstance(command, (list, tuple)) or '-c' not in command:
            return original(command, *args, **kwargs)
        index = command.index('-c') + 1
        if (index >= len(command) or not isinstance(command[index], str)
                or "run_module('carr_library.ingest.worker'" not in command[index]):
            return original(command, *args, **kwargs)
        command = list(command)
        command[index] = script
        return original(command, *args, **kwargs)

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)


@pytest.mark.parametrize('kind', ['script', 'inline'])
def test_child_barrier_preserves_unrelated_real_subprocesses(tmp_path, monkeypatch, kind):
    marker = tmp_path / 'must-not-reach-ingest-boundary'
    _child_barrier(monkeypatch, marker, 'after_commit')
    code = "print('unrelated child')"
    if kind == 'script':
        path = tmp_path / 'unrelated.py'
        path.write_text(code, encoding='utf-8')
        command = [sys.executable, '-I', '-B', str(path)]
    else:
        command = [sys.executable, '-I', '-B', '-c', code]
    result = worker.subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == 'unrelated child'
    assert not marker.exists()


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
@pytest.mark.parametrize('stop', ['cancel', 'timeout', 'crash'])
def test_fresh_committed_import_survives_lost_child_reply(tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Committed isolated import')
    collection = app.create_collection(workspace, 'Requested collection')
    source = _source(tmp_path / ('Оригінал Ω.' + kind))
    original_bytes = source.read_bytes()
    marker = tmp_path / 'committed'
    _child_barrier(monkeypatch, marker,
                   'crash_after_commit' if stop == 'crash' else 'after_commit')
    timed_out = []
    if stop == 'timeout':
        original_invoke, real_clock = worker._invoke, worker.time
        def expire_after_actual_commit(message, **options):
            # Keep the real supervisor and native child. Advance only this
            # module's deadline clock after the actual SQLite commit barrier;
            # cold PDF startup must not turn this into a pre-commit timeout.
            # The original real-wall-clock bound below remains unchanged.
            if 'data_dir' not in message:
                return original_invoke(message, **options)
            clock = {'reads': 0, 'advanced': False}
            def deadline_clock():
                now = real_clock.monotonic()
                clock['reads'] += 1
                if clock['reads'] > 1 and marker.exists():
                    clock['advanced'] = True
                return now + (options['timeout'] + 1 if clock['advanced'] else 0)
            with monkeypatch.context() as patch:
                patch.setattr(worker, 'time',
                              SimpleNamespace(monotonic=deadline_clock, sleep=real_clock.sleep))
                result = original_invoke(message, **options)
            timed_out.append((result['state'], clock['advanced'], marker.exists()))
            return result
        monkeypatch.setattr(worker, '_invoke', expire_after_actual_commit)
    started = monotonic()
    result = app.import_file(workspace, source, title='Exact original', authors=('Synthetic author',),
        collections=(collection,), timeout=15,
        cancelled=(lambda: marker.exists()) if stop == 'cancel' else (lambda: False))
    assert marker.exists(), result
    assert monotonic() - started < 12
    if stop == 'timeout':
        assert timed_out == [('timed_out', True, True)]
    assert result['state'] == 'complete', result
    assert result['isolated'] and result['verified'] is False
    persisted = worker.load_request(app.root, result['request_id'])
    assert persisted['state'] == 'complete' and persisted['result']['id'] == result['id']
    assert app.browse(workspace, collection=collection)[0].title == 'Exact original'
    hit, = app.search(workspace, 'railway', collection=collection)
    assert hit.revision == result['id'] and not hit.verified
    assert source.read_bytes() == original_bytes
    assert app.assets.resolve(persisted['asset_sha256']).read_bytes() == original_bytes
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['pdf', 'docx'])
def test_stopped_uncommitted_transaction_is_not_adopted(tmp_path, monkeypatch, kind):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Uncommitted isolated import')
    source = _source(tmp_path / ('source.' + kind))
    marker = tmp_path / 'staged'
    with monkeypatch.context() as patch:
        _child_barrier(patch, marker, 'before_commit')
        result = app.import_file(workspace, source, cancelled=marker.exists)
    assert marker.exists() and result['state'] == 'cancelled', result
    assert app.browse(workspace) == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    record = worker.load_request(app.root, result['request_id'])
    assert record['result']['state'] == 'cancelled'
    source.unlink()
    resumed = app.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_cancelled_replay_does_not_adopt_existing_publication_or_add_membership(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay remains cancellable')
    initial_collection = app.create_collection(workspace, 'Initial')
    requested = app.create_collection(workspace, 'Not yet committed')
    source = _source(tmp_path / 'source.pdf')
    metadata = dict(title='Stable source', work_id='stable-work', edition_id='stable-edition')
    first = app.import_file(workspace, source, collections=(initial_collection,), **metadata)
    assert first['state'] == 'complete'
    marker = tmp_path / 'replaying'
    _child_barrier(monkeypatch, marker, 'replay')
    replay = app.import_file(workspace, source, collections=(requested,),
                            cancelled=marker.exists, **metadata)
    assert marker.exists() and replay['state'] == 'cancelled', replay
    assert replay['request_id'] != first['request_id']
    assert app.browse(workspace, collection=requested) == ()
    assert app.search(workspace, 'railway', collection=initial_collection)[0].revision == first['id']


@pytest.mark.parametrize('field,value', [
    ('request_id', 'f' * 32), ('attempt', 'e' * 32), ('asset', '0' * 64),
    ('edition', 'foreign-edition'), ('workspace', 'foreign-workspace'),
    ('pages_sha256', '0' * 64), ('request_sha256', '0' * 64),
    ('retained_source', None), ('request_title', 'Changed during publication'),
])
def test_unbound_publication_intent_never_becomes_success(tmp_path, monkeypatch, field, value):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Publication binding')
    source = _source(tmp_path / 'source.docx')
    marker = tmp_path / 'committed'
    _child_barrier(monkeypatch, marker, 'after_commit', change=(field, value))
    result = app.import_file(workspace, source, cancelled=marker.exists)
    assert marker.exists(), result
    assert result['state'] == 'failed', result
    assert 'verif' in result['message'].lower()
    assert len(app.browse(workspace)) == 1  # Never delete an already committed source.


def test_prelaunch_cancellation_has_no_publication_to_recover(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('No native work requested')
    source = _source(tmp_path / 'source.docx')
    result = app.import_file(workspace, source, cancelled=lambda: True)
    assert result['state'] == 'cancelled'
    assert app.browse(workspace) == ()
    request = worker.load_request(app.root, result['request_id'])
    assert 'asset_sha256' not in request and request['result']['state'] == 'cancelled'


def test_quarantined_checkpoint_is_not_adopted_as_published_source(tmp_path, monkeypatch):
    import sqlite3

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Review remains necessary')
    source = _source(tmp_path / 'source.pdf')
    marker = tmp_path / 'quarantined'
    _child_barrier(monkeypatch, marker, 'quarantined')
    result = app.import_file(workspace, source, cancelled=marker.exists)
    assert marker.exists() and result['state'] == 'cancelled'
    request = worker.load_request(app.root, result['request_id'])
    with sqlite3.connect(app.root / 'imports.sqlite') as journal:
        assert journal.execute('SELECT state FROM job WHERE id=?', (request['job'],)).fetchone() == ('quarantined',)
        assert journal.execute('SELECT count(*) FROM checkpoint WHERE job=?', (request['job'],)).fetchone() == (1,)
    assert app.browse(workspace) == ()


@pytest.mark.parametrize('budget', ['MAX_RECOVERY_SECONDS', 'MAX_RECOVERY_TEXT'])
def test_uncertain_publication_remains_resumable_when_recovery_budget_is_exhausted(tmp_path, monkeypatch, budget):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded completion check')
    source = _source(tmp_path / 'source.docx')
    marker = tmp_path / 'committed'
    started = monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(worker, budget, 0)
        _child_barrier(patch, marker, 'after_commit')
        result = app.import_file(workspace, source, cancelled=marker.exists)
    assert marker.exists() and result['state'] == 'failed'
    assert 'could not verify' in result['message'] and monotonic() - started < 5
    assert len(app.browse(workspace)) == 1
    source.unlink()
    resumed = app.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize('phase', ['before_commit', 'after_commit'])
def test_pending_intent_backup_restores_original_identity_and_resumes(tmp_path, monkeypatch, phase):
    from carr_library.library_backup import create_backup, restore_backup

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Interrupted parent')
    source = _source(tmp_path / 'source.docx')
    original = source.read_bytes()
    marker = tmp_path / 'boundary'
    snapshot = []

    def cancel_at_boundary():
        if not marker.exists():
            return False
        path, = (app.root / 'import-requests').glob('*.json')
        snapshot.append(deepcopy(worker.load_request(app.root, path.stem)))
        return True

    with monkeypatch.context() as patch:
        _child_barrier(patch, marker, phase)
        result = app.import_file(workspace, source, cancelled=cancel_at_boundary)
    assert snapshot and snapshot[-1]['result']['state'] == 'publishing'
    request = snapshot[-1]
    # Reproduce only the persisted state lost when the supervisor itself exits
    # before its final receipt write. The actual child transaction already ran.
    worker._store(app.root, result['request_id'], request)
    digest = create_backup(app, tmp_path / 'backup')
    source.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    before = worker.load_request(restored.root, result['request_id'])
    assert before['state'] == 'running' and before['result']['state'] == 'publishing'
    resumed = restored.resume_import(workspace, result['request_id'])
    assert resumed['state'] == 'complete'
    assert restored.assets.resolve(request['asset_sha256']).read_bytes() == original
    assert restored.search(workspace, 'railway')[0].revision == resumed['id']
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1

