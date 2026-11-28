"""Legacy request defaults preserve raw records without reusing old OCR runtimes."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import source_snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.pipeline import canonical
from carr_library.library_backup import create_backup, restore_backup


_barriers = runpy.run_path(str(Path(__file__).with_name('test_import_commit_supervisor.py')))
_child_barrier = _barriers['_child_barrier']


# Only recognition text is deterministic. The real isolated worker still decodes
# the image, binds its pixels/checkpoint, quarantines it and requires exact review.
# This is a source/configuration test, not an OCR-accuracy qualification.
_RASTER_CHILD_SETUP = r'''
from carr_library.extract import page_ocr
def recognize_review_candidate(result, image, config, deadline, **options):
    assert image.size == (320, 48)
    raw = 'Legacy railway text.\n'
    result['raw_text'] = raw
    result['status'] = 'review_required'
    result['warnings'].append('ocr_not_verified')
    result['provenance'].update(method='ocr_candidate', ocr_text=raw)
    return result
page_ocr._recognize_image = recognize_review_candidate
'''


def legacy_config():
    value = asdict(Config())
    assert value.pop('automatic_languages') is False
    assert set(value) == {'languages', 'dpi', 'timeout', 'max_pixels', 'orientation',
                          'model_policy', 'model_profile'}
    return value


def physical_source(path):
    if path.suffix == '.pdf':
        return _barriers['_source'](path)
    from PIL import Image
    with Image.new('RGB', (32, 24), 'white') as image:
        image.save(path)
    return path


def prepared(app, workspace, source):
    metadata = dict(title='Legacy prepared source', authors=[], language=None,
        edition=None, volume=None, collections=[], work_id='legacy-work',
        edition_id='legacy-edition')
    request = worker.prepare_request(app.root, workspace, source, metadata, Config(),
        expected_manifest=source_snapshot(source.parent, source.name).manifest())
    request['config'] = legacy_config()
    worker._store(app.root, request['request_id'], request)
    return request


def track_workers(monkeypatch):
    original, children = worker.subprocess.Popen, []
    def launch(command, *args, **kwargs):
        process = original(command, *args, **kwargs)
        if isinstance(command, (list, tuple)) and '-c' in command:
            index = command.index('-c') + 1
            if index < len(command) and "run_module('carr_library.ingest.worker'" in command[index]:
                children.append(process)
        return process
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    return children


@pytest.mark.parametrize('retention', ['selected', 'retained', 'restored'])
def test_actual_legacy_txt_batch_resumes_without_rewriting_saved_config(
        tmp_path, monkeypatch, retention):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Legacy queued source')
    source = tmp_path / 'original.txt'
    original = b'Legacy railway text remains exact.\n'
    source.write_bytes(original)
    batch = app.plan_import_batch(workspace, [source])
    batch['config'] = legacy_config()
    store = ImportBatchStore(app)
    store._store(batch)
    row, = batch['rows']
    request = worker.prepare_request(app.root, workspace, row['source'],
        store._metadata(row, batch), Config(), request_id=row['request_id'],
        expected_manifest=row['manifest'])
    request['config'] = legacy_config()
    worker._store(app.root, request['request_id'], request)
    if retention != 'selected':
        asset = source_snapshot(source.parent, source.name)
        app.assets.retain_snapshot(asset)
        worker.record_progress(app.root, request['request_id'], asset_sha256=asset.digest)
        source.unlink()
    if retention == 'restored':
        backup = tmp_path / 'backup'
        checksum = create_backup(app, backup)
        app = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
        store = ImportBatchStore(app)
    batch = app.import_batch(workspace, batch['batch_id'])
    row, = batch['rows']
    request = worker.load_request(app.root, request['request_id'])
    before = worker._path(app.root, request['request_id']).read_bytes()
    repeated = worker.prepare_request(app.root, workspace, row['source'],
        store._metadata(row, batch), Config(**batch['config']),
        request_id=row['request_id'], expected_manifest=row['manifest'])
    assert repeated == request
    assert worker._path(app.root, request['request_id']).read_bytes() == before
    assert batch['config'] == repeated['config'] == legacy_config()
    children = track_workers(monkeypatch)
    final = app.run_import_batch(workspace, batch['batch_id'], timeout=30)
    assert final['state'] == 'complete' and final['counts']['complete'] == 1, final
    assert len(children) == 1 and children[0].poll() is not None
    completed, = final['rows']
    assert completed['result']['state'] == 'complete' and completed['result']['isolated'] is True
    record = worker.load_request(app.root, request['request_id'])
    assert record['config'] == final['config'] == legacy_config()
    assert app.assets.resolve(record['asset_sha256']).read_bytes() == original
    assert (source.read_bytes() == original) if retention == 'selected' else not source.exists()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT raw FROM page').fetchone()[0] == original.decode('utf-8')


@pytest.mark.parametrize('kind', ['pdf', 'png'])
def test_old_prepared_physical_request_recovers_its_fresh_current_commit(
        tmp_path, monkeypatch, kind):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Legacy prepared physical source')
    source = physical_source(tmp_path / ('original.' + kind))
    if kind == 'png':
        from PIL import Image, ImageDraw
        with Image.new('RGB', (320, 48), 'white') as image:
            ImageDraw.Draw(image).text((4, 4), 'Legacy railway text.', fill='black')
            image.save(source)
    original = source.read_bytes()
    request = prepared(app, workspace, source)
    request_digest = worker._request_digest(request)
    assert request.get('job') is None and not (app.root / 'imports.sqlite').exists()
    marker = tmp_path / 'committed'
    if kind == 'png':
        # A raster has no native text. First retain a real current-runtime
        # candidate, then explicitly review that displayed checkpoint. The
        # direct no-job first-commit path remains covered by the native-text PDF.
        with monkeypatch.context() as candidate:
            _child_barrier(candidate, marker, 'recognizer_only',
                           child_setup=_RASTER_CHILD_SETUP)
            candidate_children = track_workers(candidate)
            initial = app.resume_import(workspace, request['request_id'], timeout=30)
        assert initial['state'] == 'quarantined' and initial['isolated'] is True, initial
        assert not marker.exists() and len(candidate_children) == 1
        assert candidate_children[0].poll() is not None
        retained = worker.load_request(app.root, request['request_id'])
        assert retained['config'] == legacy_config()
        assert worker._request_digest(retained) == request_digest
        with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
            state, configuration = db.execute(
                'SELECT state,config FROM job WHERE id=?', (retained['job'],)).fetchone()
            assert state == 'quarantined'
            assert json.loads(configuration)['config'] == asdict(Config())
        with app.read(workspace) as db:
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
        pending = app.pending_review(workspace, request['request_id'])
        assert pending is not None and 'Pillow/' in pending['renderer']
        assert pending['raw_text'] == 'Legacy railway text.\n'
        with pytest.raises(ValueError, match='changed'):
            app.confirm_import_review(workspace, request['request_id'],
                'Explicit fixture reviewer', confirmed=True, expected_checksum='0' * 64)
        app.confirm_import_review(workspace, request['request_id'],
            'Explicit fixture reviewer', confirmed=True, expected_checksum=pending['checksum'])
        assert app.browse(workspace) == ()
    _child_barrier(monkeypatch, marker, 'after_commit')
    children = track_workers(monkeypatch)
    result = app.resume_import(workspace, request['request_id'],
                               cancelled=marker.exists, timeout=30)
    assert marker.exists() and len(children) == 1 and children[0].poll() is not None, result
    assert result['state'] == 'complete' and result['recovered_publication'] is True, result
    assert result['worker_state'] == 'cancelled' and result['isolated'] is True
    assert result['verified'] is False
    saved = worker.load_request(app.root, request['request_id'])
    assert saved['config'] == legacy_config() and worker._request_digest(saved) == request_digest
    with app.read(workspace) as db:
        revision, configuration = db.execute('SELECT id,config FROM revision').fetchone()
        settings = json.loads(configuration)
        assert settings['config'] == asdict(Config())
        assert settings['runtime_identity']['config'] == asdict(Config())
        assert revision == result['id'] == saved['job']
        receipt = db.execute('SELECT request_id,attempt,sha256 FROM import_publication WHERE revision=?',
                             (revision,)).fetchone()
        assert tuple(receipt[:2]) == (request['request_id'], saved['attempt'])
        assert worker._publication_state(db, saved, revision)['sha256'] == receipt[2]
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        if kind == 'png':
            page, = db.execute('SELECT raw FROM page WHERE revision=?', (revision,))
            assert page[0] == pending['raw_text']
            review, = db.execute(
                'SELECT raw_sha256,render_sha256,renderer,reviewer FROM source_page_review '
                'WHERE revision=?', (revision,))
            assert tuple(review) == (sha256(pending['raw_text'].encode('utf-8')).hexdigest(),
                                     pending['render_sha256'], pending['renderer'],
                                     'Explicit fixture reviewer')
    assert source.read_bytes() == original
    assert app.assets.resolve(saved['asset_sha256']).read_bytes() == original


def test_empty_legacy_png_remains_failed_without_publication(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Empty raster remains refused')
    source = physical_source(tmp_path / 'blank.png')
    original = source.read_bytes()
    request = prepared(app, workspace, source)
    children = track_workers(monkeypatch)
    result = app.resume_import(workspace, request['request_id'], timeout=30)
    assert result['state'] == 'failed' and result['error'] == 'empty_source', result
    assert len(children) == 1 and children[0].poll() is not None
    assert result['isolated'] is True and not result.get('recovered_publication')
    saved = worker.load_request(app.root, request['request_id'])
    assert saved['config'] == legacy_config()
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
        checkpoint, = db.execute('SELECT payload FROM checkpoint WHERE job=?', (saved['job'],))
        payload = json.loads(checkpoint[0])
        assert payload['status'] == 'blank' and payload['raw_text'] == ''
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM source_page_review').fetchone()[0] == 0
    assert source.read_bytes() == original
    assert app.assets.resolve(saved['asset_sha256']).read_bytes() == original


@pytest.mark.parametrize('kind', ['pdf', 'png'])
def test_existing_legacy_physical_checkpoint_keeps_runtime_refusal(
        tmp_path, monkeypatch, kind):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Old physical checkpoint')
    source = physical_source(tmp_path / ('original.' + kind))
    original = source.read_bytes()
    request = prepared(app, workspace, source)
    marker = tmp_path / 'checkpoint'
    with monkeypatch.context() as hold:
        _child_barrier(hold, marker, 'quarantined')
        first_children = track_workers(hold)
        initial = app.resume_import(workspace, request['request_id'],
                                    cancelled=marker.exists, timeout=30)
    assert marker.exists() and initial['state'] == 'cancelled', initial
    assert len(first_children) == 1 and first_children[0].poll() is not None
    request = worker.load_request(app.root, request['request_id'])
    journal = app.root / 'imports.sqlite'
    with closing(sqlite3.connect(journal)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        row = dict(db.execute('SELECT * FROM job WHERE id=?', (request['job'],)).fetchone())
        checkpoint = dict(db.execute('SELECT * FROM checkpoint WHERE job=?', (row['id'],)).fetchone())
        assert row['state'] == 'quarantined' and checkpoint['ordinal'] == 1
        settings = json.loads(row['config'])
        assert settings['config'] == asdict(Config())
        # Model a genuine historical artifact: exact seven-field settings and a
        # different adapter fingerprint. No installed/runtime file is modified.
        settings['config'] = legacy_config()
        settings['runtime_identity']['config'] = legacy_config()
        assert settings['runtime_identity']['source_sha256']['page_ocr.py'] != '0' * 64
        settings['runtime_identity']['source_sha256']['page_ocr.py'] = '0' * 64
        old_id = row['id']
        row['config'] = canonical(settings)
        row['id'] = sha256(canonical([workspace, row['edition'], row['asset'], row['config']]).encode()).hexdigest()
        payload = json.loads(checkpoint['payload'])
        payload['provenance']['config'] = legacy_config()
        checkpoint.update(job=row['id'], payload=canonical(payload))
        checkpoint['digest'] = sha256(checkpoint['payload'].encode()).hexdigest()
        columns = ('id', 'workspace', 'edition', 'asset', 'config', 'count', 'expected_active', 'state', 'error')
        db.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?)', tuple(row[key] for key in columns))
        db.execute('INSERT INTO checkpoint VALUES(?,?,?,?)',
                   tuple(checkpoint[key] for key in ('job', 'ordinal', 'payload', 'digest')))
        db.execute('DELETE FROM checkpoint WHERE job=?', (old_id,))
        db.execute('DELETE FROM job WHERE id=?', (old_id,))
        db.commit()
        before_job = tuple(db.execute('SELECT * FROM job').fetchone())
        before_checkpoint = tuple(db.execute('SELECT * FROM checkpoint').fetchone())
    request['job'] = row['id']
    worker._store(app.root, request['request_id'], request)
    source.unlink()
    children = track_workers(monkeypatch)
    result = app.resume_import(workspace, request['request_id'], timeout=30)
    assert len(children) == 1 and children[0].poll() is not None
    assert result['state'] == 'failed' and 'Extraction runtime changed' in result['message'], result
    assert not result.get('recovered_publication')
    with closing(sqlite3.connect(journal)) as db:
        assert tuple(db.execute('SELECT * FROM job').fetchone()) == before_job
        assert tuple(db.execute('SELECT * FROM checkpoint').fetchone()) == before_checkpoint
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    assert worker.load_request(app.root, request['request_id'])['config'] == legacy_config()
    assert app.assets.resolve(request['asset_sha256']).read_bytes() == original and not source.exists()


@pytest.mark.parametrize('damage', [
    'enabled', 'numeric-false', 'missing-field', 'unknown-field', 'timeout',
    'metadata', 'workspace', 'source', 'manifest',
])
def test_legacy_default_equivalence_does_not_relax_other_prepared_identity(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact saved identity')
    source = tmp_path / 'original.txt'
    source.write_text('Exact legacy selected source.', encoding='utf-8')
    original = prepared(app, workspace, source)
    changed = deepcopy(original)
    if damage == 'enabled':
        changed['config']['automatic_languages'] = True
    elif damage == 'numeric-false':
        changed['config']['automatic_languages'] = 0
    elif damage == 'missing-field':
        changed['config'].pop('orientation')
    elif damage == 'unknown-field':
        changed['config']['unknown_policy'] = False
    elif damage == 'timeout':
        changed['config']['timeout'] += 1
    elif damage == 'metadata':
        changed['metadata']['title'] = 'Different title'
    elif damage == 'workspace':
        changed['workspace'] = 'another-workspace'
    elif damage == 'source':
        changed['source'] = str(tmp_path / 'another.txt')
    else:
        changed['expected_manifest']['sha256'] = '0' * 64
    worker._store(app.root, original['request_id'], changed)
    before = worker._path(app.root, original['request_id']).read_bytes()
    with pytest.raises(ValueError, match='planned identity'):
        worker.prepare_request(app.root, workspace, source, original['metadata'], Config(),
            request_id=original['request_id'], expected_manifest=original['expected_manifest'])
    assert worker._path(app.root, original['request_id']).read_bytes() == before
    assert not (app.root / 'imports.sqlite').exists()


@pytest.mark.parametrize('restore_before_run', [False, True])
def test_unprepared_legacy_batch_keeps_wire_through_lost_reply_and_full_backup(
        tmp_path, monkeypatch, restore_before_run):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Unprepared historical batch')
    source = tmp_path / 'original.txt'
    original = b'Unprepared legacy railway source.\n'
    source.write_bytes(original)
    batch = app.plan_import_batch(workspace, [source])
    batch['config'] = legacy_config()
    store = ImportBatchStore(app)
    store._store(batch)
    row, = batch['rows']
    with pytest.raises(FileNotFoundError):
        worker.load_request(app.root, row['request_id'])
    if restore_before_run:
        backup = tmp_path / 'before-run'
        checksum = create_backup(app, backup)
        source.unlink()
        app = LibraryApp(restore_backup(backup, tmp_path / 'restored-before-run', checksum))
        store = ImportBatchStore(app)
        batch = app.import_batch(workspace, batch['batch_id'])
        row, = batch['rows']
        with pytest.raises(FileNotFoundError):
            worker.load_request(app.root, row['request_id'])
    original_store, lost = ImportBatchStore._store, []
    def lose_completed_batch_write(self, record, **options):
        if (self.root == app.root and record['batch_id'] == batch['batch_id']
                and record['rows'][0]['state'] == 'complete' and not lost):
            completed = worker.load_request(app.root, row['request_id'])
            assert completed['state'] == 'complete' and completed['config'] == legacy_config()
            lost.append(completed['result']['id'])
            raise OSError('Synthetic loss before completed batch publication')
        return original_store(self, record, **options)
    with monkeypatch.context() as interrupted:
        children = track_workers(interrupted)
        interrupted.setattr(ImportBatchStore, '_store', lose_completed_batch_write)
        with pytest.raises(OSError, match='before completed batch publication'):
            app.run_import_batch(workspace, batch['batch_id'], timeout=30)
    assert len(children) == 1 and children[0].poll() is not None and len(lost) == 1
    durable_batch = store._path(batch['batch_id']).read_bytes()
    persisted = json.loads(durable_batch)
    assert persisted['config'] == legacy_config() and persisted['rows'][0]['state'] == 'running'
    view = app.import_batch(workspace, batch['batch_id'])
    assert view['state'] == 'complete' and view['rows'][0]['result']['id'] == lost[0]
    assert store._path(batch['batch_id']).read_bytes() == durable_batch
    request = worker.load_request(app.root, row['request_id'])
    assert request['config'] == view['config'] == legacy_config()
    if source.exists():
        assert source.read_bytes() == original
        source.unlink()
    def forbid(*args, **kwargs):
        pytest.fail('Passive backup/reconciliation repeated an already committed import child.')
    with monkeypatch.context() as passive:
        passive.setattr(worker.subprocess, 'Popen', forbid)
        backup = tmp_path / 'after-lost-reply'
        checksum = create_backup(app, backup)
        restored = LibraryApp(restore_backup(backup, tmp_path / 'restored-after-commit', checksum))
        final = restored.run_import_batch(workspace, batch['batch_id'], timeout=30)
    assert final['state'] == 'complete' and final['rows'][0]['result']['id'] == lost[0]
    after = worker.load_request(restored.root, row['request_id'])
    assert after['config'] == final['config'] == legacy_config()
    assert after['source'] == final['rows'][0]['source']
    assert restored.assets.resolve(after['asset_sha256']).read_bytes() == original
    assert not source.exists()
    with restored.read(workspace) as db:
        assert db.execute('SELECT id FROM revision').fetchall()[0][0] == lost[0]
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1


@pytest.mark.parametrize('damage', ['enabled', 'numeric-false', 'missing-field', 'unknown-field', 'timeout'])
def test_initial_saved_config_wire_must_match_typed_options_before_request_creation(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bound initial request options')
    source = tmp_path / 'original.txt'
    source.write_text('Strict initial request options.', encoding='utf-8')
    config = legacy_config()
    if damage == 'enabled':
        config['automatic_languages'] = True
    elif damage == 'numeric-false':
        config['automatic_languages'] = 0
    elif damage == 'missing-field':
        config.pop('orientation')
    elif damage == 'unknown-field':
        config['unknown_policy'] = False
    else:
        config['timeout'] += 1
    metadata = dict(title='Selected source', authors=[], language=None,
        edition=None, volume=None, collections=[], work_id='work', edition_id='edition')
    with pytest.raises(ValueError, match='configuration differs'):
        worker.prepare_request(app.root, workspace, source, metadata, Config(),
            request_id='a' * 32, request_config=config,
            expected_manifest=source_snapshot(source.parent, source.name).manifest())
    assert not tuple((app.root / 'import-requests').glob('*.json'))
    assert not (app.root / 'imports.sqlite').exists()
