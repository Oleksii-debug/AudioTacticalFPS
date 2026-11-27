"""Real pending worker/journal recovery, plus untrusted-backup failure cases."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.worker import load_request, prepare_request, record_progress
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.pending_backup import snapshot_pending, restore_pending, validate_pending
from carr_library.storage.revisions import Storage


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'source-library')


def _source(tmp_path, name='original.txt', text='Original first page.\fOriginal second page.\fOriginal third page.'):
    source = tmp_path / name
    source.write_text(text, encoding='utf-8')
    return source


def _snapshot(app, tmp_path, name='snapshot'):
    stage = tmp_path / name
    stage.mkdir()
    with exclusive_writer(app.root), closing(Storage(app.database)) as storage:
        storage.backup(stage / 'library.sqlite')
        metadata = snapshot_pending(app, stage / 'pending')
    return stage, metadata


def _restore(backup, metadata, tmp_path, name='restored'):
    stage = tmp_path / (name + '-stage')
    stage.mkdir()
    shutil.copyfile(backup / 'library.sqlite', stage / 'library.sqlite')
    target = tmp_path / name
    result = restore_pending(backup / 'pending', stage, target, metadata)
    stage.rename(target)
    return LibraryApp(target), result


def _metadata():
    return dict(title='Pending quotation', authors=['Original author'], language=None,
                edition=None, volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)


def _partial(app, tmp_path):
    workspace = app.create_workspace('Pending library')
    source = _source(tmp_path)
    metadata = _metadata()
    planned = snapshot(source.parent, source.name).manifest()
    request = prepare_request(app.root, workspace, source, metadata, Config(), expected_manifest=planned)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 2
    result = app._import_file(workspace, source, **metadata, cancelled=cancelled,
                              _request_id=request['request_id'], _expected_manifest=planned)
    assert result['state'] == 'cancelled' and result['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=result)
    return workspace, source, load_request(app.root, request['request_id']), result


def _rewrite_json(path, update):
    value = json.loads(path.read_bytes())
    update(value)
    path.write_bytes((canonical(value) + '\n').encode('utf-8'))


def _refresh_file(metadata, component, name):
    raw = (component / name).read_bytes()
    entry = next(item for item in metadata['files'] if item['filename'] == name)
    entry.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))


def test_actual_partial_import_restores_original_and_resumes_same_job(app, tmp_path):
    workspace, source, request, initial = _partial(app, tmp_path)
    source.unlink()
    orphan = b'Unreferenced bytes must not enter the backup.'
    app.assets.retain(orphan, 'text/plain', sha256(orphan).hexdigest())
    backup, metadata = _snapshot(app, tmp_path)
    assert len(metadata['assets']) == 1
    assert metadata['assets'][0]['sha256'] == request['asset_sha256']
    before_journal = (backup / 'pending' / 'imports.sqlite').read_bytes()
    restored, report = _restore(backup, metadata, tmp_path)
    assert report['paths'] == ['imports.sqlite', 'import-requests']
    assert (restored.root / 'imports.sqlite').read_bytes() == before_journal
    after = load_request(restored.root, request['request_id'])
    expected = dict(request, data_dir=str(restored.root), source=str(restored.assets.resolve(request['asset_sha256'])))
    assert after == expected
    final = restored.resume_import(workspace, request['request_id'])
    assert final['state'] == 'complete' and final['verified'] is True
    assert final['id'] == initial['id'] and final['edition'] == initial['edition']
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == 3
    assert len(restored.search(workspace, 'second')) == 1
    assert restored.resume_import(workspace, request['request_id'])['id'] == final['id']


def test_lost_parent_reply_backup_preserves_commit_but_discards_attempt_receipt(app, tmp_path):
    from carr_library.ingest.worker import run_request, _store

    workspace = app.create_workspace('Committed child')
    source = _source(tmp_path)
    result = run_request(app.root, workspace, source, _metadata(), Config())
    assert result['state'] == 'complete'
    request = load_request(app.root, result['request_id'])
    assert request['publication_candidate']['revision'] == result['id']
    request['state'] = 'running'
    request.pop('result')
    _store(app.root, result['request_id'], request)
    backup, metadata = _snapshot(app, tmp_path)
    source.unlink()
    restored, _ = _restore(backup, metadata, tmp_path)
    rebound = load_request(restored.root, result['request_id'])
    assert 'attempt' not in rebound and 'publication_candidate' not in rebound
    assert rebound['state'] == 'running'
    completed = restored.resume_import(workspace, result['request_id'])
    assert completed['state'] == 'complete' and completed['id'] == result['id']
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == 3


@pytest.mark.parametrize('field', ('attempt', 'revision', 'sha256', 'completed', 'manifest'))
def test_malformed_publication_candidate_cannot_enter_backup(app, tmp_path, field):
    from carr_library.ingest.worker import run_request, _store

    workspace = app.create_workspace('Candidate validation')
    source = _source(tmp_path)
    result = run_request(app.root, workspace, source, _metadata(), Config())
    assert result['state'] == 'complete'
    request = load_request(app.root, result['request_id'])
    if field == 'manifest':
        request['publication_candidate'][field]['sha256'] = '0' * 64
    elif field == 'completed':
        request['publication_candidate'][field] = True
    elif field == 'attempt':
        request['publication_candidate'][field] = uuid4().hex
    else:
        request['publication_candidate'][field] = 'not-a-hash'
    _store(app.root, result['request_id'], request)
    with pytest.raises(ValueError, match='publication candidate|foreign source'):
        _snapshot(app, tmp_path)


def test_planned_batch_without_requests_captures_external_bytes_and_duplicates(app, tmp_path):
    workspace = app.create_workspace('Planned library')
    first = _source(tmp_path, 'a.txt', 'First retained planned source.')
    duplicate = _source(tmp_path, 'b.txt', first.read_text(encoding='utf-8'))
    second = _source(tmp_path, 'c.txt', 'Second retained planned source.')
    collection = app.create_collection(workspace, 'Planned sources')
    plan = app.plan_import_batch(workspace, [first, duplicate, second], collections=[collection])
    assert plan['counts']['queued'] == 2 and plan['counts']['duplicate'] == 1
    backup, metadata = _snapshot(app, tmp_path)
    assert len(metadata['assets']) == 2 and len(metadata['sources']) == 3
    for path in (first, duplicate, second):
        path.unlink()
    restored, _ = _restore(backup, metadata, tmp_path)
    rebound = restored.import_batch(workspace, plan['batch_id'])
    assert rebound['batch_id'] == plan['batch_id']
    for before, after in zip(plan['rows'], rebound['rows']):
        for key in before:
            if key not in ('source', 'duplicate_of'):
                assert after[key] == before[key]
        assert Path(after['source']).parent == restored.assets_dir
    completed = restored.run_import_batch(workspace, plan['batch_id'])
    assert completed['state'] == 'complete' and completed['counts']['complete'] == 2
    assert completed['counts']['duplicate'] == 1
    assert len(restored.browse(workspace, collection=collection)) == 2
    # A restored duplicate can be backed up again without losing its identity.
    _snapshot(restored, tmp_path, 'second-snapshot')


def test_prepared_request_and_running_batch_remain_bound_after_restore(app, tmp_path, monkeypatch):
    workspace = app.create_workspace('Prepared request')
    source = _source(tmp_path)
    planned = app.plan_import_batch(workspace, [source])
    class Crash(BaseException):
        pass
    def stop(*args, **kwargs):
        raise Crash()
    monkeypatch.setattr(app, 'resume_import', stop)
    with pytest.raises(Crash):
        app.run_import_batch(workspace, planned['batch_id'])
    backup, metadata = _snapshot(app, tmp_path)
    source.unlink()
    restored, _ = _restore(backup, metadata, tmp_path)
    final = restored.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete'
    row = final['rows'][0]
    request = load_request(restored.root, row['request_id'])
    assert row['source'] == request['source']
    assert row['work_id'] == planned['rows'][0]['work_id']
    assert row['edition_id'] == planned['rows'][0]['edition_id']
    assert request['expected_manifest'] == row['manifest']


@pytest.mark.parametrize('change', ['missing', 'changed', 'symlink', 'hardlink'])
def test_missing_or_replaced_planned_source_never_makes_complete_backup(app, tmp_path, change):
    workspace = app.create_workspace('Fail closed')
    source = _source(tmp_path)
    app.plan_import_batch(workspace, [source])
    if change == 'changed':
        source.write_text('Different source bytes.', encoding='utf-8')
    else:
        original = tmp_path / 'unselected.txt'
        source.rename(original)
        if change == 'symlink':
            source.symlink_to(original)
        elif change == 'hardlink':
            os.link(original, source)
    with pytest.raises(ValueError, match='missing|changed|links|regular'):
        _snapshot(app, tmp_path)


def test_empty_pending_component_and_no_unreferenced_retained_bytes(app, tmp_path):
    app.create_workspace('Empty queue')
    raw = b'An abandoned, unreferenced original.'
    app.assets.retain(raw, 'text/plain', sha256(raw).hexdigest())
    backup, metadata = _snapshot(app, tmp_path)
    assert metadata['files'] == metadata['assets'] == metadata['sources'] == []
    restored, report = _restore(backup, metadata, tmp_path)
    assert report == {'assets': [], 'paths': []}
    assert list(restored.assets_dir.iterdir()) == []


@pytest.mark.parametrize('kind', ['schema', 'checkpoint_hash', 'checkpoint_locator', 'checkpoint_text', 'job_identity', 'workspace', 'fake_complete'])
def test_journal_tampering_rejected_before_restore_publication(app, tmp_path, kind):
    workspace, source, request, result = _partial(app, tmp_path)
    backup, metadata = _snapshot(app, tmp_path)
    journal = backup / 'pending' / 'imports.sqlite'
    with sqlite3.connect(journal) as db:
        if kind == 'schema':
            db.execute('CREATE TRIGGER altered AFTER INSERT ON job BEGIN DELETE FROM checkpoint; END')
        elif kind == 'checkpoint_hash':
            db.execute("UPDATE checkpoint SET digest=?", ('0' * 64,))
        elif kind in ('checkpoint_locator', 'checkpoint_text'):
            raw = db.execute('SELECT payload FROM checkpoint').fetchone()[0]
            payload = json.loads(raw)
            if kind == 'checkpoint_locator':
                payload['physical_index'] = 1
            else:
                payload['raw_text'] = 'An invented quotation with a recomputed checkpoint checksum.'
            raw = canonical(payload)
            db.execute('UPDATE checkpoint SET payload=?,digest=?', (raw, sha256(raw.encode()).hexdigest()))
        elif kind == 'job_identity':
            db.execute('UPDATE job SET edition=?', ('unrelated-edition',))
        elif kind == 'workspace':
            db.execute('UPDATE job SET workspace=?', ('foreign-workspace',))
        else:
            db.execute("UPDATE job SET state='complete'")
    # Even recomputing a component checksum cannot bypass semantic validation.
    _refresh_file(metadata, backup / 'pending', 'imports.sqlite')
    with pytest.raises(ValueError):
        _restore(backup, metadata, tmp_path)
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('kind', ['hash', 'size', 'traversal', 'extra', 'symlink', 'foreign_request', 'retarget'])
def test_component_tampering_and_source_retargeting_rejected(app, tmp_path, kind):
    _, _, request, _ = _partial(app, tmp_path)
    backup, metadata = _snapshot(app, tmp_path)
    component = backup / 'pending'
    asset_name = 'originals/' + metadata['assets'][0]['filename']
    request_name = 'import-requests/' + request['request_id'] + '.json'
    if kind == 'hash':
        path = component / asset_name
        raw = path.read_bytes()
        path.write_bytes(b'X' + raw[1:])
    elif kind == 'size':
        (component / asset_name).write_bytes(b'changed')
    elif kind == 'traversal':
        metadata['files'][0]['filename'] = '../library.sqlite'
    elif kind == 'extra':
        (component / 'unlisted').write_text('not allowed')
    elif kind == 'symlink':
        path = component / asset_name
        original = tmp_path / 'external-copy'
        path.rename(original)
        path.symlink_to(original)
    elif kind == 'foreign_request':
        _rewrite_json(component / request_name, lambda value: value.update(workspace='foreign-workspace'))
        _refresh_file(metadata, component, request_name)
    else:
        metadata['sources'][0]['source'] = str(tmp_path / 'unselected.txt')
    with pytest.raises(ValueError):
        _restore(backup, metadata, tmp_path)
    assert not (tmp_path / 'restored').exists()


def test_backup_never_follows_symlinked_journal_ancestor(app, tmp_path):
    _partial(app, tmp_path)
    directory = app.root / 'import-requests'
    moved = tmp_path / 'outside-requests'
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match='links'):
        _snapshot(app, tmp_path)


def test_cancelled_snapshot_and_restore_are_explicit(app, tmp_path):
    _partial(app, tmp_path)
    with pytest.raises(InterruptedError):
        snapshot_pending(app, tmp_path / 'cancelled', cancelled=lambda: True)
    assert not (tmp_path / 'cancelled').exists()
    backup, metadata = _snapshot(app, tmp_path)
    stage = tmp_path / 'restore-stage'
    stage.mkdir()
    shutil.copyfile(backup / 'library.sqlite', stage / 'library.sqlite')
    with pytest.raises(InterruptedError):
        restore_pending(backup / 'pending', stage, tmp_path / 'restored', metadata, cancelled=lambda: True)
    assert list(stage.iterdir()) == [stage / 'library.sqlite']


def test_noncanonical_and_duplicate_json_are_not_accepted(app, tmp_path):
    _, _, request, _ = _partial(app, tmp_path)
    path = app.root / 'import-requests' / (request['request_id'] + '.json')
    raw = path.read_text(encoding='utf-8')
    path.write_text(raw.replace('{', '{"version":1,', 1), encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        _snapshot(app, tmp_path)


def test_manifest_limits_and_exact_source_bindings(app, tmp_path):
    _partial(app, tmp_path)
    _, metadata = _snapshot(app, tmp_path)
    bad = deepcopy(metadata)
    bad['sources'].append(dict(bad['sources'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        validate_pending(bad)
    bad = deepcopy(metadata)
    bad['files'][0]['size_bytes'] = 9 * 1024**3
    with pytest.raises(ValueError, match='budget'):
        validate_pending(bad)


def test_journal_online_backup_includes_committed_wal(app, tmp_path):
    _partial(app, tmp_path)
    with sqlite3.connect(app.root / 'imports.sqlite') as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute("UPDATE job SET error='committed_wal_evidence'")
        writer.commit()
        assert (app.root / 'imports.sqlite-wal').stat().st_size > 0
        backup, metadata = _snapshot(app, tmp_path)
    assert not any(item['filename'].endswith(('-wal', '-shm')) for item in metadata['files'])
    restored, _ = _restore(backup, metadata, tmp_path)
    with sqlite3.connect(restored.root / 'imports.sqlite') as journal:
        assert journal.execute('SELECT error FROM job').fetchone()[0] == 'committed_wal_evidence'


@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Actual local OCR engine required')
def test_actual_reviewed_ocr_checkpoint_retains_attestation_and_resumes(app, tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    original = tmp_path / 'scanned.pdf'
    image = Image.new('RGB', (1200, 300), 'white')
    ImageDraw.Draw(image).text((30, 70), 'Railway backup source',
                               font=ImageFont.load_default(size=72), fill='black')
    canvas = Canvas(str(original))
    canvas.drawImage(ImageReader(image), 50, 500, 500, 125)
    canvas.save()
    workspace = app.create_workspace('Reviewed pending original')
    pending = app.import_file(workspace, original)
    assert pending['state'] == 'quarantined'
    request_id = pending['request_id']
    review = app.pending_review(workspace, request_id)
    assert 'Railway' in review['raw_text']
    app.confirm_import_review(workspace, request_id, 'Synthetic test reviewer', confirmed=True,
                              expected_checksum=review['checksum'])
    before = (app.root / 'import-requests' / 'reviews' / (request_id + '.json')).read_bytes()
    backup, metadata = _snapshot(app, tmp_path)
    original.unlink()
    restored, _ = _restore(backup, metadata, tmp_path)
    assert (restored.root / 'import-requests' / 'reviews' / (request_id + '.json')).read_bytes() == before
    assert restored.pending_review(workspace, request_id)['checksum'] == review['checksum']
    final = restored.resume_import(workspace, request_id)
    assert final['state'] == 'complete' and final['id'] == pending['id']
    hit, = restored.search(workspace, 'Railway')
    assert hit.verified


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_orphaned_pending_sqlite_state_cannot_be_omitted(app, tmp_path, suffix):
    (app.root / ('imports.sqlite' + suffix)).write_bytes(b'orphaned SQLite state')
    with pytest.raises(ValueError, match='orphaned'):
        _snapshot(app, tmp_path)


def test_transient_directory_entries_are_bounded_before_snapshot(app, tmp_path, monkeypatch):
    from carr_library import pending_backup
    directory = app.root / 'import-requests'
    directory.mkdir()
    for index in range(4):
        (directory / f'{index:032x}.tmp').write_bytes(b'incomplete')
    monkeypatch.setattr(pending_backup, 'MAX_FILES', 2)
    with pytest.raises(ValueError, match='directory entries'):
        _snapshot(app, tmp_path)


def test_active_request_guard_refuses_snapshot_without_waiting(app, tmp_path):
    from carr_library.ingest.worker import request_lock
    _, _, request, _ = _partial(app, tmp_path)
    with request_lock(app.root, request['request_id']):
        with pytest.raises(ValueError, match='running'):
            _snapshot(app, tmp_path)


def test_batch_writer_cannot_replace_a_record_during_snapshot(app, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from carr_library import pending_backup
    workspace = app.create_workspace('Guarded queue')
    source = _source(tmp_path)
    batch = app.plan_import_batch(workspace, [source])
    write = pending_backup._write
    checks = []

    def protected(path, raw):
        if Path(path).parent.name == 'import-batches':
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(app.set_import_batch_row, workspace, batch['batch_id'],
                                     batch['rows'][0]['row_id'], included=False)
                with pytest.raises(ValueError, match='already running'):
                    future.result(timeout=5)
            checks.append(Path(path).name)
        return write(path, raw)

    monkeypatch.setattr(pending_backup, '_write', protected)
    _snapshot(app, tmp_path)
    assert checks == [batch['batch_id'] + '.json']
    assert app.import_batch(workspace, batch['batch_id'])['rows'][0]['state'] == 'queued'


def test_completed_request_cannot_retarget_another_sealed_original(app, tmp_path):
    workspace = app.create_workspace('Sealed sources')
    results = []
    for number in (1, 2):
        source = tmp_path / f'source{number}.html'
        source.write_text(f'<html><body><p>Distinct original {number}.</p></body></html>', encoding='utf-8')
        result = app.import_file(workspace, source)
        assert result['state'] == 'complete'
        results.append(result)
    first = load_request(app.root, results[0]['request_id'])
    second = load_request(app.root, results[1]['request_id'])
    backup, metadata = _snapshot(app, tmp_path)
    component = backup / 'pending'
    name = 'import-requests/' + first['request_id'] + '.json'
    _rewrite_json(component / name, lambda value: value.update(asset_sha256=second['asset_sha256']))
    _refresh_file(metadata, component, name)
    binding = next(value for value in metadata['sources'] if value['filename'] == name)
    binding['asset_sha256'] = second['asset_sha256']
    with pytest.raises(ValueError, match='sealed publication'):
        _restore(backup, metadata, tmp_path)
    assert not (tmp_path / 'restored').exists()
