"""Backup schema identity and legacy compatibility on actual SQLite files."""
from contextlib import closing
from hashlib import sha256
import json
import shutil
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup
from carr_library.storage.revisions import Storage
from carr_library.storage.schema_identity import (
    expected_schema_fingerprint, schema_fingerprint,
)


@pytest.fixture
def backup(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Schema compatibility')
    topic = app.create_topic(workspace, 'Evidence')
    app.add_text(workspace, 'Original title', 'Архів. A source-exact quotation.',
                 authors=('Original author',))
    hit, = app.search(workspace, 'Архів')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this separate note.')
    directory = tmp_path / 'backup'
    create_backup(app, directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    return app, workspace, topic, directory, manifest


def write_manifest(directory, manifest):
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (directory / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def bundle_bytes(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


def forbid_writable_storage(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail('incompatible backup reached writable Storage')
    monkeypatch.setattr('carr_library.library_backup.Storage', refused)


def test_new_backup_names_exact_schema_and_restores_research(backup, tmp_path):
    app, workspace, topic, directory, manifest = backup
    assert manifest['version'] == 4
    assert manifest['schema_version'] == 4
    assert set(manifest) == {'version', 'kind', 'schema_version',
                             'schema_fingerprint', 'database', 'assets', 'sidecars',
                             'generated_audio_included', 'pending'}
    assert manifest['sidecars'] == []
    assert manifest['generated_audio_included'] is False
    with closing(sqlite3.connect((directory / 'library.sqlite').as_uri() + '?mode=ro',
                                 uri=True)) as db:
        assert manifest['schema_fingerprint'] == schema_fingerprint(db)
    assert manifest['schema_fingerprint'] == expected_schema_fingerprint(4)
    original = bundle_bytes(directory)
    target = restore_backup(directory, tmp_path / 'restored', write_manifest(directory, manifest))
    restored = LibraryApp(target)
    assert restored.evidence(workspace, topic)[0] == app.evidence(workspace, topic)[0]
    assert bundle_bytes(directory) == original


def test_fingerprint_is_layout_identity_not_database_content(tmp_path):
    with closing(Storage(tmp_path / 'one.sqlite', schema_version=3)) as first:
        with closing(Storage(tmp_path / 'two.sqlite', schema_version=3)) as second:
            original = schema_fingerprint(first.db)
            assert original == schema_fingerprint(second.db) == expected_schema_fingerprint(3)
            with first.transaction():
                first.db.execute("INSERT INTO workspace VALUES('different-contents')")
                first.db.execute("INSERT INTO workspace_meta VALUES('different-contents','Unicode бібліотека','',0)")
            assert schema_fingerprint(first.db) == original
            first.db.execute('PRAGMA user_version=4')
            assert schema_fingerprint(first.db) != original
            first.db.execute('PRAGMA user_version=3')
            first.db.execute('CREATE TABLE unknown_extension(value TEXT)')
            assert schema_fingerprint(first.db) != original


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_backup_restores_exact_current_schema(backup, tmp_path, version):
    app, workspace, topic, directory, manifest = backup
    manifest['version'] = version
    manifest.pop('pending', None)
    shutil.rmtree(directory / 'pending', ignore_errors=True)
    del manifest['sidecars']
    del manifest['generated_audio_included']
    if version == 1:
        del manifest['schema_fingerprint']
    digest = write_manifest(directory, manifest)
    original = bundle_bytes(directory)
    before = app.evidence(workspace, topic)[0]
    restored = LibraryApp(restore_backup(directory, tmp_path / 'legacy-restored', digest))
    assert restored.evidence(workspace, topic)[0] == before
    assert restored.search(workspace, 'Архів')[0].title == 'Original title'
    assert bundle_bytes(directory) == original


@pytest.mark.parametrize('version', [1, 2, 3])
@pytest.mark.parametrize('sql', [
    # The competing unreleased layout used workspace_profile at user_version=3.
    'ALTER TABLE workspace_meta RENAME TO workspace_profile',
    'CREATE TABLE unknown_extension(value TEXT)',
    'DROP TRIGGER source_metadata_no_update',
    'PRAGMA user_version=5',
])
def test_different_layout_is_rejected_before_writable_open(
        backup, tmp_path, monkeypatch, version, sql):
    app, _, _, directory, manifest = backup
    source_before = app.database.read_bytes()
    database = directory / 'library.sqlite'
    with closing(sqlite3.connect(database)) as db:
        db.execute(sql)
        db.commit()
    manifest['version'] = version
    manifest.pop('pending', None)
    shutil.rmtree(directory / 'pending', ignore_errors=True)
    if version < 3:
        del manifest['sidecars']
        del manifest['generated_audio_included']
    elif version == 3:
        del manifest['generated_audio_included']
    if version == 1:
        del manifest['schema_fingerprint']
    # A valid outer checksum cannot turn an incompatible layout into our schema.
    manifest['database']['size_bytes'] = database.stat().st_size
    manifest['database']['sha256'] = sha256(database.read_bytes()).hexdigest()
    digest = write_manifest(directory, manifest)
    original = bundle_bytes(directory)
    forbid_writable_storage(monkeypatch)
    destination = tmp_path / 'rejected'
    with pytest.raises(RestoreError, match='schema'):
        restore_backup(directory, destination, digest)
    assert not destination.exists()
    assert bundle_bytes(directory) == original
    assert app.database.read_bytes() == source_before


@pytest.mark.parametrize('change', [
    'missing_fingerprint', 'wrong_fingerprint', 'nonstring_fingerprint',
    'unknown_version', 'boolean_version', 'unexpected_field', 'legacy_extra_field',
    'duplicate_asset', 'boolean_asset_size',
])
def test_manifest_fields_are_strict_before_writable_open(
        backup, tmp_path, monkeypatch, change):
    _, _, _, directory, manifest = backup
    if change == 'missing_fingerprint':
        del manifest['schema_fingerprint']
    elif change == 'wrong_fingerprint':
        manifest['schema_fingerprint'] = '0' * 64
    elif change == 'nonstring_fingerprint':
        manifest['schema_fingerprint'] = True
    elif change == 'unknown_version':
        manifest['version'] = 99
    elif change == 'boolean_version':
        manifest['version'] = True
    elif change == 'unexpected_field':
        manifest['allow_unknown_schema'] = True
    elif change == 'legacy_extra_field':
        manifest['version'] = 1
    elif change == 'duplicate_asset':
        manifest['assets'].append(dict(manifest['assets'][0]))
    else:
        manifest['assets'][0]['size_bytes'] = True
    digest = write_manifest(directory, manifest)
    original = bundle_bytes(directory)
    forbid_writable_storage(monkeypatch)
    destination = tmp_path / 'rejected'
    with pytest.raises(RestoreError):
        restore_backup(directory, destination, digest)
    assert not destination.exists()
    assert bundle_bytes(directory) == original


@pytest.mark.parametrize('duplicate', ['schema_fingerprint', 'database_size'])
def test_duplicate_json_keys_are_rejected(backup, tmp_path, monkeypatch, duplicate):
    _, _, _, directory, manifest = backup
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    if duplicate == 'schema_fingerprint':
        raw = '{"schema_fingerprint":' + json.dumps(manifest['schema_fingerprint']) + ',' + raw[1:]
    else:
        size = manifest['database']['size_bytes']
        raw = raw.replace('"database":{', '"database":{"size_bytes":' + str(size) + ',', 1)
    encoded = raw.encode('utf-8')
    (directory / 'manifest.json').write_bytes(encoded)
    original = bundle_bytes(directory)
    forbid_writable_storage(monkeypatch)
    destination = tmp_path / 'rejected'
    with pytest.raises(RestoreError, match='duplicate manifest key'):
        restore_backup(directory, destination, sha256(encoded).hexdigest())
    assert not destination.exists()
    assert bundle_bytes(directory) == original
