"""Real SQLite, producer data, corruption and child-exit backup regressions."""
from hashlib import sha256
import json
import shutil
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_sidecars, library_backup
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def resign(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def manifest(folder):
    return json.loads((folder / 'manifest.json').read_bytes())


def files(folder):
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


@pytest.fixture
def reading(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    workspace = app.create_workspace('Джерела')
    app.add_text(workspace, 'Exact original', 'Незмінний текст. Exact source e\u0301.',
                 edition_id='edition', authors=('Автор',))
    page = ReadingDesk(app, workspace, 'edition').page(1)
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'Anchor-A')
    app.update_metadata(workspace, 'edition', title='A later catalog title',
                        authors=('Автор',), expected_metadata=app.edition_metadata(workspace, 'edition'))
    return app, workspace, page, bookmark


def test_v4_preserves_exact_historical_reader_state(reading, tmp_path):
    app, workspace, page, bookmark = reading
    source_bytes = (app.root / 'reading-state.json').read_bytes()
    app.archive_workspace(workspace)
    backup = tmp_path / 'Копія'
    checksum = app.backup(backup)
    value = manifest(backup)
    assert value['version'] == 5 and value['generated_audio_included'] is False
    assert value['sidecars'] == [dict(filename='reading-state.json', size_bytes=len(source_bytes),
                                     sha256=sha256(source_bytes).hexdigest())]
    before = files(backup)
    target = restore_backup(backup, tmp_path / 'Відновлена', checksum)
    assert ReadingState(target).resume(workspace, 'edition') == page.location
    assert ReadingState(target).bookmarks(workspace, 'edition') == (bookmark,)
    assert (target / 'reading-state.json').read_bytes() == source_bytes
    assert next(row for row in LibraryApp(target).workspaces(include_archived=True)
                if row.id == workspace).archived
    assert files(backup) == before


@pytest.mark.parametrize('shape', ['reading_state', 'sidecars'])
def test_exact_v3_reader_shapes_restore_without_rewriting(reading, tmp_path, shape):
    app, workspace, page, bookmark = reading
    folder = tmp_path / 'legacy'
    create_backup(app, folder)
    value = manifest(folder)
    if value.pop('pending') is not None:
        shutil.rmtree(folder / 'pending')
    value['version'] = 3
    del value['generated_audio_included']
    if shape == 'reading_state':
        value['reading_state'] = value.pop('sidecars')[0]
    digest = resign(folder, value)
    before = files(folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert ReadingState(target).resume(workspace, 'edition') == page.location
    assert ReadingState(target).bookmarks(workspace, 'edition') == (bookmark,)
    assert files(folder) == before


@pytest.mark.parametrize('shape', ['reading_state', 'state'])
def test_empty_v3_variants_have_unambiguous_namespaces(tmp_path, shape):
    app = LibraryApp(tmp_path / 'data')
    app.create_workspace('Empty reader state')
    folder = tmp_path / 'legacy'
    create_backup(app, folder)
    value = manifest(folder)
    if value.pop('pending') is not None:
        shutil.rmtree(folder / 'pending')
    value['version'] = 3
    del value['sidecars']
    if shape == 'reading_state':
        del value['generated_audio_included']
        value['reading_state'] = None
    else:
        value['state'] = []
        (folder / 'state').mkdir()
    digest = resign(folder, value)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert len(LibraryApp(target).workspaces()) == 1
    assert not (target / 'reading-state.json').exists()


@pytest.mark.parametrize('name', [
    'state/studio/jobs/123/job.json', 'state/assistant/123.json',
    'state/providers/credentials.json', 'state/studio/draft.json',
])
def test_unvalidated_jobs_and_settings_fail_instead_of_disappearing(reading, tmp_path, name):
    app, *_ = reading
    path = app.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"status":"running","secret":"synthetic-only"}')
    before = files(app.root)
    with pytest.raises(ValueError, match='validated backup adapter'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert all((app.root / name).read_bytes() == data for name, data in before.items()
               if not name.endswith(('-wal', '-shm')))


def test_unrelated_private_files_are_not_backup_inputs(reading, tmp_path):
    app, *_ = reading
    (app.root / 'credentials.json').write_text('synthetic private marker')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    assert not (folder / 'credentials.json').exists()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_missing_producer_validator_refuses_existing_data(reading, tmp_path, monkeypatch, operation):
    app, *_ = reading
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    root = app.root if operation == 'backup' else folder
    name = 'narration-projects/' + 'a' * 32 + '.json'
    path = root / name
    path.parent.mkdir()
    path.write_bytes(b'{}')
    real_import = backup_sidecars.import_module
    def missing(name, package):
        if name == '.reader.narration':
            raise ModuleNotFoundError('synthetic absent optional producer')
        return real_import(name, package)
    monkeypatch.setattr(backup_sidecars, 'import_module', missing)
    if operation == 'restore':
        value = manifest(folder)
        value['sidecars'].append(dict(filename=name, size_bytes=2, sha256=sha256(b'{}').hexdigest()))
        digest = resign(folder, value)
        monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened writable SQLite'))
    with pytest.raises(ValueError, match='installed data validator'):
        if operation == 'backup':
            create_backup(app, tmp_path / 'rejected')
        else:
            restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_sidecar_disk_full_does_not_publish_partial_destination(reading, tmp_path, monkeypatch, operation):
    import errno
    app, *_ = reading
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    before = files(folder)
    original = backup_sidecars._write
    def full(stage, name, raw):
        original(stage, name, raw)
        raise OSError(errno.ENOSPC, 'synthetic disk-full failure after staged sidecar write')
    monkeypatch.setattr(backup_sidecars, '_write', full)
    with pytest.raises(OSError) as error:
        if operation == 'backup':
            create_backup(app, tmp_path / 'rejected')
        else:
            restore_backup(folder, tmp_path / 'rejected', digest)
    assert error.value.errno == errno.ENOSPC
    assert not (tmp_path / 'rejected').exists()
    assert files(folder) == before


@pytest.mark.parametrize('change', ['mixed', 'extra', 'traversal', 'absolute', 'duplicate',
                                  'boolean-size', 'generated-number', 'reserved'])
def test_invalid_manifest_never_reaches_writable_storage(reading, tmp_path, monkeypatch, change):
    app, *_ = reading
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = manifest(folder)
    entry = value['sidecars'][0]
    if change == 'mixed':
        value.update(version=3, state=[])
    elif change == 'extra':
        value['allow_unsafe_state'] = True
    elif change in ('traversal', 'absolute', 'reserved'):
        entry['filename'] = {'traversal': '../reading-state.json', 'absolute': '/reading-state.json',
                             'reserved': 'state/CON/project.json'}[change]
    elif change == 'duplicate':
        value['sidecars'].append(dict(entry))
    elif change == 'boolean-size':
        entry['size_bytes'] = True
    else:
        value['generated_audio_included'] = 1
    digest = resign(folder, value)
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened writable SQLite'))
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


def test_same_size_valid_json_tamper_is_detected(reading, tmp_path):
    app, *_ = reading
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    path = folder / 'reading-state.json'
    raw = path.read_bytes()
    changed = raw.replace(b'Anchor-A', b'Anchor-B')
    assert len(changed) == len(raw) and changed != raw
    json.loads(changed)
    path.write_bytes(changed)
    with pytest.raises(ValueError, match='digest'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


def test_sidecar_change_during_snapshot_prevents_publication(reading, tmp_path, monkeypatch):
    app, *_ = reading
    original = backup_sidecars._write
    def changed(stage, name, raw):
        original(stage, name, raw)
        if name == 'reading-state.json':
            (app.root / name).write_bytes(raw.replace(b'Anchor-A', b'Anchor-B'))
    monkeypatch.setattr(backup_sidecars, '_write', changed)
    with pytest.raises(ValueError, match='changed during backup'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_real_process_exit_during_sidecar_publication(reading, tmp_path, operation):
    app, workspace, page, bookmark = reading
    folder = tmp_path / 'complete'
    digest = create_backup(app, folder)
    destination = tmp_path / 'interrupted'
    code = '''
import os,sys
from pathlib import Path
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup,restore_backup
root,source,target,digest,operation=sys.argv[1:]
link=os.link
def interrupt(src,dst,*args,**kwargs):
    if Path(dst)==Path(target)/'reading-state.json':os._exit(73)
    return link(src,dst,*args,**kwargs)
os.link=interrupt
if operation=='backup':create_backup(LibraryApp(root),target)
else:restore_backup(source,target,digest)
'''
    result = subprocess.run([sys.executable, '-c', code, str(app.root), str(folder), str(destination), digest, operation],
        env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)}, capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr
    if operation == 'backup':
        assert not (destination / 'manifest.json').exists()
    else:
        assert (destination / INCOMPLETE).exists()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(destination)
    with pytest.raises(FileExistsError):
        create_backup(app, destination)
    restored = restore_backup(folder, tmp_path / 'retry', digest)
    assert ReadingState(restored).bookmarks(workspace, 'edition') == (bookmark,)


@pytest.mark.parametrize('maximum', [True, 0, -1, 0.5, '4096'])
def test_invalid_total_budget_is_rejected_without_output(reading, tmp_path, maximum):
    app, *_ = reading
    with pytest.raises(ValueError, match='positive integer'):
        app.backup(tmp_path / 'rejected', max_bytes=maximum)
    assert not (tmp_path / 'rejected').exists()


def test_total_budget_includes_every_file_and_manifest(reading, tmp_path):
    app, *_ = reading
    folder = tmp_path / 'reference'
    create_backup(app, folder)
    total = sum(len(raw) for raw in files(folder).values())
    with pytest.raises(ValueError, match='byte budget'):
        app.backup(tmp_path / 'tiny', max_bytes=4096)
    with pytest.raises(ValueError, match='byte budget'):
        app.backup(tmp_path / 'one-byte-short', max_bytes=total - 1)
    assert not (tmp_path / 'tiny').exists() and not (tmp_path / 'one-byte-short').exists()
    exact = tmp_path / 'exact'
    app.backup(exact, max_bytes=total)
    assert sum(len(raw) for raw in files(exact).values()) == total


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_cancelled_operation_never_publishes_destination(reading, tmp_path, operation):
    app, *_ = reading
    folder = tmp_path / 'reference'
    digest = create_backup(app, folder)
    cancelled = lambda: True
    with pytest.raises(InterruptedError):
        if operation == 'backup':
            app.backup(tmp_path / 'rejected', cancelled=cancelled)
        else:
            restore_backup(folder, tmp_path / 'rejected', digest, cancelled=cancelled)
    assert not (tmp_path / 'rejected').exists()
