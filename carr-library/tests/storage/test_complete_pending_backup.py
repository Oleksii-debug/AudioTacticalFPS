"""Enclosing manifest and publication integration for resumable import backups."""
from contextlib import closing
from uuid import uuid4
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import prepare_request, record_progress, load_request
from carr_library.ingest.pipeline import canonical
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE


def test_complete_backup_survives_lost_original_and_resumes_identical_import(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Resumable source')
    original = tmp_path / 'original.txt'
    original.write_text('First original page.\fSecond original page.\fThird original page.', encoding='utf-8')
    metadata = dict(title='Unfinished book', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 2
    initial = app._import_file(workspace, original, **metadata, cancelled=cancelled,
                              _request_id=request['request_id'], _expected_manifest=planned)
    assert initial['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    digest = create_backup(app, tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert completed['id'] == initial['id'] and completed['edition'] == initial['edition']
    assert restored.search(workspace, 'Second')[0].quote == 'Second'


def test_complete_backup_preserves_prepared_request_before_first_worker_launch(tmp_path):
    """A queued request may have no journal/checkpoint or retained original yet."""
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Queued source')
    original = tmp_path / 'queued.txt'
    original.write_text('Queued original survives relocation.', encoding='utf-8')
    metadata = dict(title='Queued book', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    digest = create_backup(app, tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    assert rebound['expected_manifest'] == planned
    from pathlib import Path
    retained = Path(rebound['source'])
    assert retained.is_relative_to(restored.root)
    assert retained.read_bytes() == b'Queued original survives relocation.'
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert completed['edition'] == metadata['edition_id']
    assert restored.search(workspace, 'relocation')[0].quote == 'relocation'


def _manifest(folder, value=None):
    path = folder / 'manifest.json'
    if value is None:
        return json.loads(path.read_bytes())
    raw = (canonical(value) + '\n').encode('utf-8')
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def _metadata():
    return dict(title='Queued source', authors=[], language=None, edition=None,
                volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)


def _planned(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Queued source')
    source = tmp_path / 'queued.txt'
    source.write_text('Railway original survives relocation.', encoding='utf-8')
    batch = app.plan_import_batch(workspace, [source])
    return app, workspace, source, batch


def test_empty_library_uses_exact_v5_and_counts_only_delivered_files(tmp_path):
    from carr_library.library_backup import _read_manifest, declared_backup_files
    app = LibraryApp(tmp_path / 'empty')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder, max_files=2)
    manifest = _read_manifest(folder, digest)
    assert manifest['version'] == 5
    assert set(manifest) == {'version', 'kind', 'schema_version', 'schema_fingerprint',
                             'database', 'assets', 'sidecars', 'provider_restore_mode', 'pending'}
    assert manifest['provider_restore_mode'] == 'LOCAL_ONLY'
    assert manifest['pending'] == dict(version=1, data_dir=str(app.root),
                                      files=[], assets=[], sources=[])
    assert {path.name for path in folder.iterdir()} == {
        'library.sqlite', 'originals', 'pending', 'manifest.json'}
    assert set(declared_backup_files(manifest)) == {'library.sqlite'}
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (restored / 'imports.sqlite').exists()
    assert not (restored / 'import-requests').exists()


def test_unpinned_prepared_request_restores_verified_hash_before_explicit_resume(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Prepared before first child')
    original = tmp_path / 'queued.txt'
    raw = b'Original queued railway evidence.'
    original.write_bytes(raw)
    metadata = _metadata()
    request = prepare_request(app.root, workspace, original, metadata, Config())
    assert 'asset_sha256' not in request and 'expected_manifest' not in request
    request_path = app.root / 'import-requests' / (request['request_id'] + '.json')
    before = request_path.read_bytes()
    assert not (app.root / 'imports.sqlite').exists()
    assert list(app.assets_dir.iterdir()) == []
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert request_path.read_bytes() == before
    original.unlink()

    bad = LibraryApp(restore_backup(folder, tmp_path / 'damaged-restore', digest))
    rebound = load_request(bad.root, request['request_id'])
    assert rebound['asset_sha256'] == sha256(raw).hexdigest()
    assert rebound['metadata'] == request['metadata']
    assert rebound['config'] == request['config'] and rebound['state'] == request['state']
    retained = Path(rebound['source'])
    assert retained.parent == bad.assets_dir and retained.read_bytes() == raw
    changed = b'A different source must never replace the saved original.'
    retained.write_bytes(changed)
    result = bad.resume_import(workspace, request['request_id'])
    assert result['state'] == 'failed'
    with bad.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 0
    assert not any(path.name.startswith(sha256(changed).hexdigest()) for path in bad.assets_dir.iterdir())

    good = LibraryApp(restore_backup(folder, tmp_path / 'intact-restore', digest))
    with good.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 0
    completed = good.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert completed['edition'] == metadata['edition_id']
    assert good.search(workspace, 'railway')[0].quote == 'railway'


@pytest.mark.parametrize('version', [3, 4])
def test_actual_legacy_sidecar_shapes_remain_restorable(tmp_path, version):
    from carr_library.storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.progress import ProgressSettings, ProgressSettingsStore
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Legacy')
    result = app.add_text(workspace, 'Saved source', 'Exact historical railway source.')
    location = ReadingDesk(app, workspace, result['edition']).page(1).location
    ReadingState(app.root).remember(location)
    settings = ProgressSettings(mode='beep', volume=35, interval_seconds=7)
    if version == 4:
        ProgressSettingsStore(app.root).save(settings)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = _manifest(folder)
    manifest['version'] = version
    # These are genuine old sidecar layouts over their released V4 catalog,
    # not a current database with only the enclosing version number changed.
    database = folder / 'library.sqlite'
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
        for name in ('no_update', 'no_delete', 'no_replace'):
            db.execute('DROP TRIGGER import_publication_' + name)
        db.execute('DROP TABLE import_publication')
        db.execute('PRAGMA user_version=4')
        db.commit()
        assert schema_fingerprint(db) == expected_schema_fingerprint(4)
    manifest['schema_version'] = 4
    manifest['schema_fingerprint'] = expected_schema_fingerprint(4)
    manifest['database']['size_bytes'] = database.stat().st_size
    manifest['database']['sha256'] = sha256(database.read_bytes()).hexdigest()
    del manifest['pending']
    shutil.rmtree(folder / 'pending')
    if version == 3:
        del manifest['provider_restore_mode']
    digest = _manifest(folder, manifest)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    assert ReadingState(restored.root).resume(workspace, result['edition']) == location
    if version == 4:
        assert ProgressSettingsStore(restored.root).load() == settings
    assert not (restored.root / 'imports.sqlite').exists()


def test_partially_completed_batch_keeps_completed_journal_and_queued_input(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Mixed batch')
    sources = [tmp_path / 'first.txt', tmp_path / 'second.txt']
    for path, text in zip(sources, ('First railway original.', 'Second railway original.')):
        path.write_text(text, encoding='utf-8')
    plan = app.plan_import_batch(workspace, sources)
    stop = Event()

    def completed_one(record):
        if record['counts']['complete'] == 1:
            stop.set()

    initial = app.run_import_batch(workspace, plan['batch_id'],
                                  cancelled=stop.is_set, progress=completed_one)
    assert initial['counts']['complete'] == 1 and initial['counts']['queued'] == 1
    first = next(row for row in initial['rows'] if row['state'] == 'complete')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    with sqlite3.connect(folder / 'pending' / 'imports.sqlite') as journal:
        assert journal.execute('SELECT id,state FROM job').fetchall() == [(first['result']['id'], 'complete')]
    assert len(_manifest(folder)['pending']['assets']) == 2
    for path in sources:
        path.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    before = restored.import_batch(workspace, plan['batch_id'])
    assert before['counts'] == initial['counts']
    final = restored.run_import_batch(workspace, plan['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 2
    finished = next(row for row in final['rows'] if row['row_id'] == first['row_id'])
    assert finished['result']['id'] == first['result']['id']
    second = tmp_path / 'second-backup'
    second_digest = create_backup(restored, second)
    again = LibraryApp(restore_backup(second, tmp_path / 'again', second_digest))
    replayed = again.run_import_batch(workspace, plan['batch_id'])
    assert [row['result']['id'] for row in replayed['rows']] == [row['result']['id'] for row in final['rows']]
    with again.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 2


@pytest.mark.parametrize(('suffix', 'text'), [
    ('.HTML', '<html><body><p>Railway original source.</p></body></html>'),
    ('.MD', '# Railway original source\n\nExact retained text.'),
    ('.EML', 'From: archive@example.test\nTo: reader@example.test\nSubject: Railway\n'
             'Content-Type: text/plain; charset=utf-8\n\nRailway original source.'),
])
def test_extended_queued_source_survives_loss_and_real_import(tmp_path, suffix, text):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Extended source')
    source = tmp_path / ('original' + suffix)
    source.write_text(text, encoding='utf-8')
    plan = app.plan_import_batch(workspace, [source])
    assert plan['counts']['queued'] == 1
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    assert _manifest(folder)['pending']['assets'][0]['filename'].endswith(suffix.lower())
    source.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 0
    final = restored.run_import_batch(workspace, plan['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1
    row = final['rows'][0]
    assert row['edition_id'] == plan['rows'][0]['edition_id']
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM page WHERE verified=1').fetchone()[0] == 0
    second = tmp_path / 'second-backup'
    second_digest = create_backup(restored, second)
    again = LibraryApp(restore_backup(second, tmp_path / 'again', second_digest))
    assert again.resume_import(workspace, row['request_id'])['id'] == row['result']['id']


@pytest.mark.parametrize('damage', ['missing', 'extra', 'hardlink', 'retarget', 'foreign',
                                    'missing-component', 'boolean-version', 'remote-mode'])
def test_resigned_pending_backup_still_requires_exact_files_and_identities(tmp_path, damage):
    app, _, _, _ = _planned(tmp_path)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = _manifest(folder)
    component = folder / 'pending'
    entry = next(row for row in manifest['pending']['files'] if row['filename'].startswith('import-batches/'))
    path = component / entry['filename']
    if damage == 'missing':
        path.unlink()
    elif damage == 'extra':
        (component / 'unlisted').write_bytes(b'Unexpected')
    elif damage == 'hardlink':
        os.link(path, tmp_path / 'extra-link')
    elif damage == 'retarget':
        manifest['pending']['sources'][0]['source'] = str(tmp_path / 'unselected.txt')
    elif damage == 'foreign':
        batch = json.loads(path.read_bytes())
        batch['workspace'] = 'foreign-workspace'
        raw = (canonical(batch) + '\n').encode('utf-8')
        path.write_bytes(raw)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    elif damage == 'missing-component':
        del manifest['pending']
    elif damage == 'boolean-version':
        manifest['pending']['version'] = True
    else:
        manifest['provider_restore_mode'] = 'APPROVED_REMOTE'
    digest = _manifest(folder, manifest)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, target, digest)
    assert not target.exists()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_pending_directory_publication_interruption_never_claims_completion(tmp_path, monkeypatch, operation):
    app, _, _, _ = _planned(tmp_path)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = tmp_path / 'interrupted'
    original = Path.rename
    selected = 'pending' if operation == 'backup' else 'import-batches'

    def interrupted(source, destination):
        if Path(destination) == target / selected:
            raise OSError('Synthetic pending publication interruption')
        return original(source, destination)

    monkeypatch.setattr(Path, 'rename', interrupted)
    with pytest.raises(OSError, match='Synthetic'):
        if operation == 'backup':
            create_backup(app, target)
        else:
            restore_backup(folder, target, digest)
    if operation == 'backup':
        assert not (target / 'manifest.json').exists()
        with pytest.raises(ValueError, match='final manifest'):
            restore_backup(target, tmp_path / 'rejected', digest)
    else:
        assert (target / INCOMPLETE).exists()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(target)


def test_rebased_prepared_request_cannot_exceed_reader_limit(tmp_path):
    from carr_library.ingest.worker import MAX_REQUEST, _request_record
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Long metadata')
    source = tmp_path / 'q.txt'
    source.write_bytes(b'Exact original.')
    metadata = _metadata()
    metadata.update(title='T' * 4000, authors=['A' * 2000] * 29, volume='')
    identity = uuid4().hex
    planned = snapshot(source.parent, source.name).manifest()
    record = _request_record(app.root, workspace, source, metadata, Config(), identity, planned)
    padding = MAX_REQUEST - 8 - len((canonical(record) + '\n').encode('utf-8'))
    assert 0 < padding <= 4000
    metadata['volume'] = 'V' * padding
    prepare_request(app.root, workspace, source, metadata, Config(),
                    request_id=identity, expected_manifest=planned)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = tmp_path / ('Відновлення_' + 'я' * 30)
    with pytest.raises(ValueError, match='Rebased.*byte budget'):
        restore_backup(folder, target, digest)
    assert not target.exists()


def test_rebased_batch_cannot_exceed_reader_limit(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, _, _, plan = _planned(tmp_path)
    record = app.root / 'import-batches' / (plan['batch_id'] + '.json')
    monkeypatch.setattr(pending_backup, 'MAX_BATCH', record.stat().st_size + 8)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = tmp_path / ('Відновлення_' + 'я' * 30)
    with pytest.raises(ValueError, match='Rebased.*byte budget'):
        restore_backup(folder, target, digest)
    assert not target.exists()


@pytest.mark.parametrize(('name', 'message'), [
    ('MAX_BACKUP_BYTES', 'byte limit'), ('MAX_BACKUP_FILES', 'file limit'),
])
def test_reader_counts_pending_inside_the_existing_aggregate_ceiling(tmp_path, monkeypatch, name, message):
    from carr_library import library_backup
    app, _, _, _ = _planned(tmp_path)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    files = [path for path in folder.rglob('*') if path.is_file()]
    ordinary = [path for path in files if not path.is_relative_to(folder / 'pending')]
    measure = (lambda paths: sum(path.stat().st_size for path in paths)
               if name == 'MAX_BACKUP_BYTES' else len(paths))
    ceiling = measure(files) - 1
    assert measure(ordinary) <= ceiling
    monkeypatch.setattr(library_backup, name, ceiling)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match=message):
        restore_backup(folder, target, digest)
    assert not target.exists()
