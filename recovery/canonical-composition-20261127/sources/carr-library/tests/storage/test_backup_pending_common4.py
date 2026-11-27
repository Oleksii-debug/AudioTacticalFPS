"""Common backup closure using the canonical interrupted-import producer."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.worker import load_request, prepare_request, record_progress
from carr_library.library_backup import create_backup, restore_backup, verify_backup, INCOMPLETE
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def partial(tmp_path):
    app = LibraryApp(tmp_path / 'Джерела čítanie')
    workspace = app.create_workspace('Pending sources')
    source = tmp_path / 'original.txt'
    source.write_text('First exact page.\fSecond railway page.\fThird exact page.', encoding='utf-8')
    metadata = dict(title='Pending source', authors=[], language=None, edition=None,
        volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(source.parent, source.name).manifest()
    request = prepare_request(app.root, workspace, source, metadata, Config(), expected_manifest=planned)
    calls = 0
    def stop():
        nonlocal calls
        calls += 1
        return calls >= 2
    result = app._import_file(workspace, source, **metadata, cancelled=stop,
        _request_id=request['request_id'], _expected_manifest=planned)
    assert result['state'] == 'cancelled' and result['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=result)
    return app, workspace, source, load_request(app.root, request['request_id']), result


def files(root):
    return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def resign(root, manifest):
    raw = (canonical(manifest) + '\n').encode('utf-8')
    (root / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_common4_partial_import_and_reader_state_restore_without_execution(tmp_path, monkeypatch):
    app, workspace, source, request, before = partial(tmp_path)
    app.add_text(workspace, 'Published source', 'Published source remains exact.', edition_id='published')
    page = ReadingDesk(app, workspace, 'published').page(1)
    ReadingState(app.root).remember(page.location)
    source.unlink()  # The retained original is the resume input.
    backup = tmp_path / 'backup'
    def forbid(*args, **kwargs):
        raise AssertionError('Backup/restore must not execute an importer or provider.')
    with monkeypatch.context() as passive:
        passive.setattr(subprocess, 'Popen', forbid)
        checksum = create_backup(app, backup)
        manifest = verify_backup(backup, checksum)
        assert manifest['version'] == 5 and manifest['pending']['version'] == 1
        assert any(e['filename'] == 'reading-state.json' for e in manifest['extensions']['files'])
        backup_before = files(backup)
        restored = LibraryApp(restore_backup(backup, tmp_path / 'Відновлена', checksum))
        assert files(backup) == backup_before
    assert (restored.root / 'imports.sqlite').read_bytes() == (backup / 'pending/imports.sqlite').read_bytes()
    after = load_request(restored.root, request['request_id'])
    assert after == dict(request, data_dir=str(restored.root),
                         source=str(restored.assets.resolve(request['asset_sha256'])))
    assert ReadingState(restored.root).resume(workspace, 'published') == page.location
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    final = restored.resume_import(workspace, request['request_id'])
    assert final['state'] == 'complete' and final['id'] == before['id']
    assert len(restored.search(workspace, 'railway')) == 1
    assert restored.resume_import(workspace, request['request_id'])['id'] == final['id']
    assert all(path.stat().st_nlink == 1 for path in restored.assets_dir.iterdir())


def test_planned_extended_format_keeps_exact_bytes_and_explicit_resume(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Planned documents')
    source = tmp_path / 'notes.md'
    source.write_text('# Source\nOriginal railway notation.', encoding='utf-8')
    planned = app.plan_import_batch(workspace, [source])
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    source.unlink()
    with monkeypatch.context() as passive:
        passive.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Restore executed a document worker'))
        restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    after = restored.import_batch(workspace, planned['batch_id'])
    assert after['state'] == planned['state']
    assert Path(after['rows'][0]['source']).suffix == '.md'
    assert after['rows'][0]['manifest'] == planned['rows'][0]['manifest']
    assert not restored.browse(workspace)
    final = restored.run_import_batch(workspace, planned['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1


@pytest.mark.parametrize('damage', ['missing', 'extra', 'checkpoint', 'traversal'])
def test_common4_pending_closure_rejects_damage_before_publication(tmp_path, damage):
    app, _, _, _, _ = partial(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    component = backup / 'pending'
    if damage == 'missing':
        (component / 'imports.sqlite').unlink()
    elif damage == 'extra':
        (component / 'unlisted.json').write_text('{}')
    elif damage == 'traversal':
        manifest['pending']['files'][0]['filename'] = '../library.sqlite'
    else:
        journal = component / 'imports.sqlite'
        with sqlite3.connect(journal) as db:
            payload = json.loads(db.execute('SELECT payload FROM checkpoint').fetchone()[0])
            payload['raw_text'] = 'Invented text with a matching checksum.'
            raw = canonical(payload)
            db.execute('UPDATE checkpoint SET payload=?, digest=?', (raw, sha256(raw.encode()).hexdigest()))
        entry = next(e for e in manifest['pending']['files'] if e['filename'] == 'imports.sqlite')
        entry.update(size_bytes=journal.stat().st_size, sha256=sha256(journal.read_bytes()).hexdigest())
    checksum = resign(backup, manifest)
    with pytest.raises((ValueError, FileNotFoundError)):
        restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()


def test_pending_bytes_count_towards_complete_backup_budget(tmp_path):
    app, _, _, _, _ = partial(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    budget = sum(p.stat().st_size for p in backup.rglob('*') if p.is_file())
    budget -= sum(e['size_bytes'] for e in manifest['pending']['files'])
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'too-small', max_bytes=budget)
    assert not (tmp_path / 'too-small').exists()


def test_pending_publication_failure_keeps_restore_incomplete(tmp_path, monkeypatch):
    app, _, _, _, _ = partial(tmp_path)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    target = tmp_path / 'restored'
    real_link = os.link
    def fail(source, destination, *args, **kwargs):
        if Path(destination) == target / 'imports.sqlite':
            raise OSError('Interrupted pending publication')
        return real_link(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError, match='Interrupted'):
        restore_backup(backup, target, checksum)
    assert (target / INCOMPLETE).is_file()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)


@pytest.mark.parametrize('version', [4, 5])
def test_exact_canonical_pending_shapes_are_read_without_rewriting(tmp_path, version):
    app, workspace, _, request, initial = partial(tmp_path)
    app.add_text(workspace, 'Published', 'Published historical reading source.', edition_id='published')
    page = ReadingDesk(app, workspace, 'published').page(1)
    ReadingState(app.root).remember(page.location)
    backup = tmp_path / 'legacy'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    extension = manifest.pop('extensions')
    reading, = extension['files']
    assert reading['filename'] == 'reading-state.json'
    (backup / 'extensions' / reading['filename']).rename(backup / reading['filename'])
    (backup / 'extensions').rmdir()
    assert manifest['state'] == [] and (backup / 'state').is_dir()
    manifest.update(version=version)
    if version == 5:
        manifest['reading_state'] = reading
    else:
        (backup / reading['filename']).unlink()
    checksum = resign(backup, manifest)
    original = files(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    assert files(backup) == original
    if version == 5:
        assert ReadingState(restored.root).resume(workspace, 'published') == page.location
    final = restored.resume_import(workspace, request['request_id'])
    assert final['state'] == 'complete' and final['id'] == initial['id']
