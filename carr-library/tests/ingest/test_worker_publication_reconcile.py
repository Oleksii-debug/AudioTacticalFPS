"""Real isolated LibraryApp imports interrupted at their durable publish boundary."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


@pytest.fixture(params=['docx', 'pdf'])
def source(tmp_path, request):
    path = tmp_path / ('Синтетичне джерело.' + request.param)
    if request.param == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph('Original railway history. Точний оригінал.')
        document.save(path)
    else:
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(path))
        canvas.drawString(72, 700, 'Original railway history.')
        canvas.save()
    return path


def publication_barrier(monkeypatch, tmp_path, phase, *, action='wait'):
    """Modify only the trusted child's publication boundary; keep its real main."""
    marker = tmp_path / ('published-' + phase)
    popen = worker.subprocess.Popen
    children = []
    hook = (
        'import sys,time\nfrom pathlib import Path\n'
        'sys.path[:0]=sys.argv[1:]\n'
        'from carr_library.application import LibraryApp,_CatalogPublisher\n'
        'from carr_library.ingest import worker\n'
        f'marker=Path({str(marker)!r})\n'
        + ('publish=LibraryApp._import_file\n' if phase == 'after_import'
           else 'publish=_CatalogPublisher.publish_revision\n')
        + 'def barrier(self,*args,**kwargs):\n'
        + ('    result=publish(self,*args,**kwargs)\n' if phase in ('after', 'after_import') else '')
        + '    marker.write_text("ready",encoding="utf-8")\n'
        + ('    raise SystemExit(71)\n' if action == 'crash' else '    time.sleep(60)\n')
        + ('    return result\n' if phase in ('after', 'after_import') else '    return publish(self,*args,**kwargs)\n')
        + ('LibraryApp._import_file=barrier\n' if phase == 'after_import'
           else '_CatalogPublisher.publish_revision=barrier\n')
        + 'raise SystemExit(worker.main())\n')

    def launch(command, **kwargs):
        # _stop uses the same subprocess module for native Windows cleanup.
        # Pass only that exact command for a child this helper launched.
        if (len(command) == 5 and command[:2] == ['taskkill', '/PID']
                and command[3:] == ['/T', '/F']):
            assert command[2] in {str(child.pid) for child in children}
            return popen(command, **kwargs)
        assert command[1:6] == ['-I', '-B', '-X', 'utf8', '-c']
        child = popen([*command[:6], hook, *command[7:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    return marker, children


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_real_isolated_cancellation_distinguishes_committed_source(source, tmp_path, monkeypatch, phase):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Publication boundary')
    collection = app.create_collection(workspace, 'Chosen collection')
    original = source.read_bytes()
    marker, children = publication_barrier(monkeypatch, tmp_path, phase)

    result = app.import_file(workspace, source, title='Exact requested source',
        authors=('Literal Author',), collections=(collection,),
        cancelled=marker.exists, timeout=15)

    assert marker.exists() and children and all(c.poll() is not None for c in children)
    assert result['state'] == ('complete' if phase == 'after' else 'cancelled'), result
    assert worker.load_request(app.root, result['request_id'])['state'] == result['state']
    assert source.read_bytes() == original
    if phase == 'after':
        assert app.assets.resolve(sha256(original).hexdigest()).read_bytes() == original
        hit, = app.search(workspace, 'railway history', collection=collection)
        assert hit.revision == result['id'] and not hit.verified
        assert len(app.browse(workspace)) == 1
        # A completed request remains idempotent even when a later caller
        # cancels: no second worker or duplicate revision is necessary.
        monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('completed request relaunched'))
        replay = app.resume_import(workspace, result['request_id'], cancelled=lambda: True)
        assert replay['state'] == 'complete' and replay['id'] == result['id']
    else:
        assert app.browse(workspace) == ()


def test_prelaunch_cancel_does_not_promote_an_existing_replay(source, tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Replay boundary')
    collection = app.create_collection(workspace, 'Not approved for cancelled replay')
    metadata = dict(title='Stable source', work_id='stable-work', edition_id='stable-edition')
    first = app.import_file(workspace, source, **metadata)
    assert first['state'] == 'complete'

    stopped = app.import_file(workspace, source, collections=(collection,),
                              cancelled=lambda: True, **metadata)

    assert stopped['state'] == 'cancelled'
    assert len(app.browse(workspace)) == 1
    assert app.browse(workspace, collection=collection) == ()
    assert app.search(workspace, 'railway history')[0].revision == first['id']


@pytest.mark.parametrize('action', ['crash', 'timeout'])
def test_crash_or_timeout_after_real_commit_recovers_complete(source, tmp_path, monkeypatch, action):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Stopped after commit')
    marker, children = publication_barrier(monkeypatch, tmp_path, 'after',
                                         action='crash' if action == 'crash' else 'wait')
    started = time.monotonic()
    result = app.import_file(workspace, source, title='Published source', timeout=3)
    assert time.monotonic() - started < 12
    assert marker.exists() and all(child.poll() is not None for child in children)
    assert result['state'] == 'complete' and result['publication_reconciled'] is True, result
    assert app.search(workspace, 'railway history')[0].revision == result['id']


@pytest.mark.parametrize('damage', ['request', 'asset'])
def test_reconciliation_refuses_changed_request_or_original(source, tmp_path, monkeypatch, damage):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Request-bound recovery')
    marker, _ = publication_barrier(monkeypatch, tmp_path, 'after')
    stop = worker._stop

    def stop_then_damage(child):
        stop(child)
        request, = (app.root / 'import-requests').glob('*.json')
        record = json.loads(request.read_text(encoding='utf-8'))
        if damage == 'request':
            record['metadata']['title'] = 'Different request identity'
            request.write_text(json.dumps(record), encoding='utf-8')
        else:
            retained = app.assets.resolve(record['asset_sha256'])
            original = retained.read_bytes()
            retained.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    monkeypatch.setattr(worker, '_stop', stop_then_damage)
    result = app.import_file(workspace, source, cancelled=marker.exists, timeout=15)
    assert marker.exists()
    assert result['state'] == 'failed' and 'could not be verified' in result['message'], result
    assert not result.get('publication_reconciled')


def test_isolated_text_batch_keeps_exact_byte_attestation_after_commit(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Exact text batch')
    source = tmp_path / 'Оригінал.txt'
    original = b'\xef\xbb\xbfOriginal railway history.\r\nSecond line.\fExact second segment.'
    source.write_bytes(original)
    batch = app.plan_import_batch(workspace, (source,))
    marker, children = publication_barrier(monkeypatch, tmp_path, 'after')

    result = app.run_import_batch(workspace, batch['batch_id'], cancelled=marker.exists, timeout=15)

    assert marker.exists() and all(child.poll() is not None for child in children)
    row, = result['rows']
    assert row['state'] == 'complete', row
    assert row['result']['verified'] is True and row['result']['publication_reconciled'] is True
    hit, = app.search(workspace, 'railway history')
    assert hit.verified and hit.revision == row['result']['id']
    assert app.assets.resolve(sha256(original).hexdigest()).read_bytes() == original


def test_committed_replay_membership_is_reconciled_without_new_revision(source, tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Completed replay membership')
    collection = app.create_collection(workspace, 'New requested membership')
    metadata = dict(title='Stable source', work_id='stable-work', edition_id='stable-edition')
    first = app.import_file(workspace, source, **metadata)
    marker, children = publication_barrier(monkeypatch, tmp_path, 'after_import')

    result = app.import_file(workspace, source, collections=(collection,),
                             cancelled=marker.exists, timeout=15, **metadata)

    assert marker.exists() and all(child.poll() is not None for child in children)
    assert result['state'] == 'complete' and result['id'] == first['id'], result
    assert result['publication_reconciled'] is True
    assert len(app.browse(workspace, collection=collection)) == 1
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


def test_expired_snapshot_never_launches_child(tmp_path, monkeypatch):
    from carr_library.extract.page_ocr import Config
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Prelaunch deadline')
    read = app.read
    clock = [time.monotonic()]

    @contextmanager
    def expire_during_snapshot(*args, **kwargs):
        with read(*args, **kwargs) as db:
            yield db
        clock[0] += 1

    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(app, 'read', expire_during_snapshot)
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('expired request launched child'))
    result = worker.run_request(app.root, workspace, tmp_path / 'unused.docx', {},
                                Config(), timeout=0.1, _app=app)
    assert result['state'] == 'timed_out'


def test_padded_edition_metadata_matches_catalog_after_real_commit(tmp_path, monkeypatch):
    from docx import Document
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Literal edition metadata')
    source = tmp_path / 'Literal metadata.docx'
    document = Document()
    document.add_paragraph('Original railway history.')
    document.save(source)
    marker, children = publication_barrier(monkeypatch, tmp_path, 'after')
    result = app.import_file(workspace, source, language=' uk ', edition=' Edition one ',
                             volume=' Volume 2 ', cancelled=marker.exists, timeout=15)
    assert marker.exists() and all(child.poll() is not None for child in children)
    assert result['state'] == 'complete' and result['publication_reconciled'] is True, result
    with app.read(workspace) as db:
        row = db.execute('SELECT language,edition,volume FROM catalog_edition WHERE workspace=?',
                         (workspace,)).fetchone()
        assert tuple(row) == (' uk ', ' Edition one ', ' Volume 2 ')


def test_publication_barrier_passes_owned_windows_taskkill_to_original_popen(tmp_path, monkeypatch):
    calls = []
    child = SimpleNamespace(pid=24680)

    def original_popen(command, **kwargs):
        calls.append((command, kwargs))
        return child

    monkeypatch.setattr(worker.subprocess, 'Popen', original_popen)
    _, children = publication_barrier(monkeypatch, tmp_path, 'after')
    # This routing-only spy does not replace cleanup in the real-child cases.
    worker.subprocess.Popen(['python', '-I', '-B', '-X', 'utf8', '-c', 'pass'])
    assert children == [child]
    command = ['taskkill', '/PID', str(child.pid), '/T', '/F']
    options = dict(stdout=worker.subprocess.DEVNULL, stderr=worker.subprocess.DEVNULL,
                   creationflags=getattr(worker.subprocess, 'CREATE_NO_WINDOW', 0))

    assert worker.subprocess.Popen(command, **options) is child
    assert calls[-1][0] is command and calls[-1][1] == options
    assert children == [child]
    with pytest.raises(AssertionError):
        worker.subprocess.Popen(['taskkill', '/PID', '99999', '/T', '/F'])
    with pytest.raises(AssertionError):
        worker.subprocess.Popen(['unrelated-command'])
    assert len(calls) == 2
