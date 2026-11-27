"""Independent full-path regressions for recovery schema and terminal imports."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.restore import RestoreError
from carr_library.ingest.worker import load_request, prepare_request, record_progress
from carr_library.library_backup import create_backup, restore_backup
from carr_library.providers import ProviderManager, ProviderProfile


def _reseal_recovery(backup):
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for record in manifest['recovery']['files']:
        raw = (backup / 'recovery' / record['path']).read_bytes()
        record.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = json.dumps(manifest, sort_keys=True).encode('utf-8')
    manifest_path.write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('alteration', ['missing_primary_key', 'sqlite_like_table'])
def test_resealed_auxiliary_schema_cannot_publish_a_broken_restored_database(tmp_path, alteration):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Source schema review')
    app.add_text(workspace, 'Original', 'Exact retained original text.')
    manager = ProviderManager(app.root / 'provider-settings')
    profile = ProviderProfile('example', 'Example', 'llm', 'https://example.com/v1', 'selected-model')
    manager.save(profile)
    backup = tmp_path / 'backup'
    create_backup(app, backup)

    with sqlite3.connect(backup / 'recovery/provider-settings/providers.sqlite') as db:
        if alteration == 'missing_primary_key':
            rows = db.execute('SELECT * FROM profiles').fetchall()
            db.execute('DROP TABLE profiles')
            # The column names and all existing row values still match. The
            # missing PK breaks the actual owner's next ON CONFLICT upsert.
            db.execute('CREATE TABLE profiles(id TEXT,document TEXT,spent_cents INTEGER,credential_ref TEXT)')
            db.executemany('INSERT INTO profiles VALUES(?,?,?,?)', rows)
        else:
            # LIKE 'sqlite_%' treats '_' as a wildcard. This ordinary table
            # is not a reserved SQLite internal object and must be rejected.
            db.execute('CREATE TABLE sqliteXunexpected(secret TEXT)')

    destination = tmp_path / 'must-not-be-published'
    with pytest.raises(RestoreError, match='schema'):
        restore_backup(backup, destination, _reseal_recovery(backup))
    assert not destination.exists()
    manager.save(profile)
    assert manager.profiles() == (profile,)


@pytest.mark.parametrize('record_terminal_request', [False, True])
def test_actual_superseded_import_and_receipt_survive_full_backup(tmp_path, record_terminal_request):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Historical imports')
    source = tmp_path / 'source.txt'
    original_bytes = b'Exact historical source.\r\n'
    source.write_bytes(original_bytes)
    metadata = dict(title='Original title', work_id='stable-work', edition_id='stable-edition')
    initial = app.import_file(workspace, source, **metadata)
    historical = app.search(workspace, 'historical')[0]
    active_revision = app.update_metadata(workspace, metadata['edition_id'], title='Current title')
    replay = app.import_file(workspace, source, **dict(metadata, title='Current title'))
    assert initial['state'] == 'complete' and replay['state'] == 'superseded'
    assert replay['id'] == initial['id'] and active_revision != initial['id']

    request = prepare_request(app.root, workspace, source,
        dict(title='Current title', authors=[], language=None, edition=None, volume=None,
             collections=[], work_id=metadata['work_id'], edition_id=metadata['edition_id']), Config())
    if record_terminal_request:
        # The real worker saves the returned state/result through this same
        # function. Retain both the actual journal and its terminal receipt.
        record_progress(app.root, request['request_id'], state=replay['state'], result=replay,
                        asset_sha256=sha256(original_bytes).hexdigest(), job=replay['id'])

    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    with sqlite3.connect(restored.root / 'imports.sqlite') as db:
        assert db.execute('SELECT state FROM job WHERE id=?', (initial['id'],)).fetchone() == ('superseded',)
    current = restored.search(workspace, 'historical')[0]
    assert current.revision == active_revision and current.title == 'Current title'
    assert restored.assets.resolve(sha256(original_bytes).hexdigest()).read_bytes() == original_bytes
    with restored.read(workspace) as db:
        assert db.execute('SELECT raw FROM page WHERE revision=?', (historical.revision,)).fetchone()[0] == original_bytes.decode('utf-8')
    recovered_request = load_request(restored.root, request['request_id'])
    assert recovered_request['state'] == ('superseded' if record_terminal_request else 'pending')
    assert recovered_request['data_dir'] == str(restored.root)


def test_cross_platform_batch_paths_are_rejected_before_any_restored_root_is_published(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Queued batch')
    source = tmp_path / 'source.txt'
    source.write_bytes(b'Exact queued source.\r\n')
    batches = ImportBatchStore(app)
    batch = batches.plan(workspace, [source])
    assert batch['state'] == 'queued' and len(batch['rows']) == 1
    backup = tmp_path / 'backup'
    create_backup(app, backup)

    # A backup made on the other supported path syntax must not publish an
    # apparently complete queue that the native batch loader cannot open.
    other_root = '/home/example/library' if os.name == 'nt' else r'C:\Users\Example\Library'
    other_source = '/home/example/source.txt' if os.name == 'nt' else r'C:\Books\source.txt'
    assert not Path(other_source).is_absolute()
    path = backup / 'recovery/import-batches' / (batch['batch_id'] + '.json')
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved['data_dir'] = other_root
    saved['rows'][0]['source'] = other_source
    path.write_text(json.dumps(saved), encoding='utf-8')
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['recovery']['source_root'] = other_root
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    destination = tmp_path / 'must-not-be-published'
    with pytest.raises(RestoreError):
        restore_backup(backup, destination, _reseal_recovery(backup))
    assert not destination.exists()
    assert batches.get(workspace, batch['batch_id']) == batch
    assert source.read_bytes() == b'Exact queued source.\r\n'
