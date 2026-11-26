"""Published V3 backup/restore acceptance through the real Library API."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import Library
from carr_library.ingest import workspace_bundle
from carr_library.research.saved_topic_query import SavedTopicQueries


@pytest.fixture
def source(tmp_path):
    app = Library(tmp_path / 'source')
    for workspace in ('alpha', 'beta'):
        app.create_workspace(workspace, 'Workspace description', workspace=workspace)
        app.create_topic(workspace, 'Research topic', topic='topic')
        state = app.import_text(workspace, workspace + ' exact original Café.\r\n',
                                 title='Title ' + workspace, authors=('Author ' + workspace,))
        services = app.bind_scope(workspace, 'topic')
        hit = services.search(workspace, 'exact')[0]
        saved = services.save_hit(workspace, hit.ref, hit.revision, hit.quote, hit.citation)
        with services.evidence_tx() as evidence:
            evidence.edit_note('topic', saved, workspace + ' preserved note')
            SavedTopicQueries(evidence).create('topic', 'query', 'Exact query', 'exact')
    app.archive_workspace('beta')
    return app


def bundle(app, tmp_path):
    path = tmp_path / 'bundle'
    digest = app.backup(path, include_all_workspaces=True)
    return path, digest


def rewrite_manifest(path, update):
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    update(manifest)
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (path / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_full_restore_preserves_all_workspaces_notes_queries_and_fresh_copy(source, tmp_path):
    path, digest = bundle(source, tmp_path)
    assert sha256((path / 'manifest.json').read_bytes()).hexdigest() == digest
    restored = Library.restore(path, tmp_path / 'restored', digest)
    assert [w.id for w in restored.workspaces()] == ['alpha']
    assert [w.id for w in restored.workspaces(include_archived=True)] == ['alpha', 'beta']
    assert restored.check()['originals_verified'] == 2
    services = restored.bind_scope('alpha', 'topic')
    hit = services.search('alpha', 'exact')[0]
    assert services.verified_copy('alpha', hit.ref, hit.revision, 'quote') == 'exact'
    with services.evidence_tx() as evidence:
        saved, = evidence.list_saved('topic')
        assert saved.note == 'alpha preserved note'
        assert SavedTopicQueries(evidence).run('topic', 'query')[0].evidence.evidence_id == saved.evidence_id
    exported = restored.export_topic('alpha', 'topic')
    assert 'alpha preserved note' in exported and 'Author alpha' in exported
    assert 'beta preserved note' not in exported
    with pytest.raises(ValueError, match='archived workspace'):
        restored.search('beta', 'exact')
    with source.storage() as before, restored.storage() as after:
        for table in ('workspace_profile', 'catalog_asset', 'catalog_edition', 'revision', 'page',
                      'metadata_snapshot', 'research_topic', 'research_evidence', 'research_saved_query'):
            assert [tuple(row) for row in before.db.execute('SELECT * FROM ' + table)] == [
                tuple(row) for row in after.db.execute('SELECT * FROM ' + table)]


def test_explicit_all_workspace_consent_and_no_overwrite(source, tmp_path):
    destination = tmp_path / 'bundle'
    with pytest.raises(ValueError, match='all-workspaces backup consent'):
        source.backup(destination)
    assert not destination.exists()
    path, digest = bundle(source, tmp_path)
    manifest_before = (path / 'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):
        source.backup(path, include_all_workspaces=True)
    assert (path / 'manifest.json').read_bytes() == manifest_before
    existing = tmp_path / 'existing'
    existing.mkdir()
    marker = existing / 'owned.txt'
    marker.write_text('Must survive', encoding='utf-8')
    with pytest.raises(FileExistsError):
        Library.restore(path, existing, digest)
    assert marker.read_text(encoding='utf-8') == 'Must survive'


@pytest.mark.parametrize('damage', ['digest', 'snapshot', 'asset', 'extra_asset', 'missing_asset', 'sidecar'])
def test_invalid_bundle_never_publishes_restore(source, tmp_path, damage):
    path, digest = bundle(source, tmp_path)
    if damage == 'digest':
        digest = '0'*64
    elif damage == 'snapshot':
        with (path / 'library.sqlite').open('r+b') as stream:
            stream.seek(100)
            value = stream.read(1)
            stream.seek(100)
            stream.write(bytes([value[0] ^ 1]))
    elif damage == 'asset':
        next((path / 'originals').iterdir()).write_text('Different bytes', encoding='utf-8')
    elif damage == 'extra_asset':
        (path / 'originals' / ('f'*64 + '.txt')).write_text('Extra', encoding='utf-8')
    elif damage == 'missing_asset':
        next((path / 'originals').iterdir()).unlink()
    else:
        (path / 'library.sqlite-wal').write_bytes(b'foreign sidecar')
    target = tmp_path / 'rejected'
    with pytest.raises((ValueError, OSError)):
        Library.restore(path, target, digest)
    assert not target.exists()
    assert source.check()['integrity'] == 'ok'


def test_rehashed_extra_schema_still_rejected(source, tmp_path):
    path, _ = bundle(source, tmp_path)
    snapshot = path / 'library.sqlite'
    with sqlite3.connect(snapshot) as db:
        db.execute('CREATE TABLE unexpected(value TEXT)')
    digest = rewrite_manifest(path, lambda m: m['snapshot'].update(
        sha256=sha256(snapshot.read_bytes()).hexdigest(), size_bytes=snapshot.stat().st_size))
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='schema mismatch'):
        Library.restore(path, target, digest)
    assert not target.exists()


@pytest.mark.parametrize('field', ['snapshot_path', 'asset_path', 'asset_size'])
def test_manifest_cannot_choose_paths_or_override_catalog(source, tmp_path, field):
    path, _ = bundle(source, tmp_path)
    def update(manifest):
        if field == 'snapshot_path':
            manifest['snapshot']['name'] = '../outside.sqlite'
        elif field == 'asset_path':
            manifest['assets'][0]['digest'] = '../outside'
        else:
            manifest['assets'][0]['size'] += 1
    digest = rewrite_manifest(path, update)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        Library.restore(path, target, digest)
    assert not target.exists()


def test_interrupted_restore_never_publishes_partial_library(source, tmp_path, monkeypatch):
    path, digest = bundle(source, tmp_path)
    target = tmp_path / 'interrupted'
    def fail_publication(src, dst):
        assert Path(src, 'library.sqlite').is_file()
        assert Path(src, 'originals').is_dir()
        raise OSError('synthetic publication interruption')
    with monkeypatch.context() as patch:
        patch.setattr(workspace_bundle, '_publish_directory', fail_publication)
        with pytest.raises(OSError, match='publication interruption'):
            Library.restore(path, target, digest)
    assert not target.exists()
    assert Library.restore(path, target, digest).check()['integrity'] == 'ok'


def test_atomic_restore_never_replaces_destination_created_during_validation(source, tmp_path, monkeypatch):
    path, digest = bundle(source, tmp_path)
    target = tmp_path / 'racing-destination'
    publish = workspace_bundle._publish_directory
    def competing_directory(src, dst):
        target.mkdir()
        return publish(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(workspace_bundle, '_publish_directory', competing_directory)
        with pytest.raises(FileExistsError):
            Library.restore(path, target, digest)
    # POSIX plain rename would silently replace this empty owned directory.
    assert target.is_dir() and list(target.iterdir()) == []


def test_backup_records_published_data_scope_and_excludes_unfinished_jobs(source, tmp_path):
    pending = source.import_text('alpha', 'Unpublished draft.\fSecond segment.',
                                 title='Pending', max_steps=1)
    assert pending['state'] == 'pending'
    path, digest = bundle(source, tmp_path)
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['pending_imports_included'] is False
    assert len(manifest['assets']) == 2
    restored = Library.restore(path, tmp_path / 'restored', digest)
    assert restored.import_jobs('alpha') == ()
    assert restored.search('alpha', 'Unpublished') == ()
    assert len(restored.search('alpha', 'exact')) == 1
    assert next(job for job in source.import_jobs('alpha') if job['id'] == pending['id'])['state'] == 'pending'


def test_failed_restore_marker_cannot_be_opened_as_an_empty_library(source, tmp_path, monkeypatch):
    """A failed restore must never masquerade as a fresh, usable empty library."""
    path, digest = bundle(source, tmp_path)
    target = tmp_path / 'marker-failure'
    write = workspace_bundle._write

    def fail_marker(filename, data):
        if Path(filename).name == workspace_bundle.PENDING:
            raise OSError('synthetic marker publication failure')
        return write(filename, data)

    with monkeypatch.context() as patch:
        patch.setattr(workspace_bundle, '_write', fail_marker)
        with pytest.raises(OSError, match='marker publication failure'):
            Library.restore(path, target, digest)
    # Either no final name was published, or it remains explicitly unusable.
    if target.exists():
        with pytest.raises(ValueError, match='restore is incomplete'):
            Library(target)
    assert source.check()['integrity'] == 'ok'
