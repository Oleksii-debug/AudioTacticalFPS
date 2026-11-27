"""Real process-exit recovery with the existing library, reader and assistant.

Only canonical TXT and reading-state producers are needed. The completion
manifest must be verifiable even when private staging cleanup never runs.
"""
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import library_backup
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def _files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def produced(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    workspace = app.create_workspace('Exact source')
    app.add_text(workspace, 'Original title', 'Незмінне джерело. Exact source e\u0301.',
                 edition_id='edition')
    page = ReadingDesk(app, workspace, 'edition').page(1)
    reading = ReadingState(app.root)
    reading.remember(page.location)
    bookmark = reading.add_bookmark(page.location, 'Anchor-A')
    app.update_metadata(workspace, 'edition', title='Later catalog title',
                        expected_metadata=app.edition_metadata(workspace, 'edition'))
    return app, workspace, page, bookmark


def test_actual_exit_at_completion_manifest_leaves_a_verifiable_backup(
        produced, tmp_path):
    from carr_library.assistant.backup_receipt import capture_backup_receipt, verify_backup_receipt
    app, workspace, page, bookmark = produced
    target = tmp_path / 'preserved'
    code = '''
import os,sys
from pathlib import Path
from carr_library.application import LibraryApp
from carr_library import library_backup
target=Path(sys.argv[2])
sync=library_backup._sync_directory
def stop(directory):
    sync(directory)
    if Path(directory)==target and (target/'manifest.json').is_file():
        os._exit(74)
library_backup._sync_directory=stop
library_backup.create_backup(LibraryApp(sys.argv[1]),target)
'''
    import carr_library.library_backup as backup_module
    source = Path(backup_module.__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(source), str(source.parent)))}
    child = subprocess.run([sys.executable, '-c', code, str(app.root), str(target)],
                           env=env, capture_output=True, text=True, timeout=30)
    assert child.returncode == 74, (child.stdout, child.stderr)
    before = _files(target)
    mtimes = {name: (target / name).stat().st_mtime_ns for name in before}
    checksum = sha256(before['manifest.json']).hexdigest()
    manifest = verify_backup(target, checksum)
    payload = ['library.sqlite', 'manifest.json',
               *(f"originals/{entry['filename']}" for entry in manifest['assets']),
               *(entry['filename'] for entry in manifest['sidecars'])]
    assert all((target / name).stat().st_nlink == 1 for name in payload)
    receipt = capture_backup_receipt(target, checksum, max_bytes=8 * 1024**2, max_files=1000)
    assert verify_backup_receipt(target, receipt, max_bytes=8 * 1024**2, max_files=1000) == receipt
    with pytest.raises(FileExistsError):
        create_backup(app, target)
    restored = restore_backup(target, tmp_path / 'restored', checksum)
    assert LibraryApp(restored).workspaces()[0].id == workspace
    assert ReadingState(restored).resume(workspace, 'edition') == page.location
    assert ReadingState(restored).bookmarks(workspace, 'edition') == (bookmark,)
    assert (restored / 'reading-state.json').read_bytes() == (app.root / 'reading-state.json').read_bytes()
    assert _files(target) == before
    assert {name: (target / name).stat().st_mtime_ns for name in before} == mtimes


def test_failed_staging_link_retirement_cannot_publish_completion(produced, tmp_path, monkeypatch):
    app, workspace, page, bookmark = produced
    original_state = (app.root / 'reading-state.json').read_bytes()
    destination = tmp_path / 'interrupted'
    real_unlink = Path.unlink

    def fail_private_retirement(path, *args, **kwargs):
        if path.name == 'reading-state.json' and path.parent.name.startswith('.carr-library-backup-'):
            raise OSError('Injected failure retiring a private staging link')
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_private_retirement)
        with pytest.raises(OSError, match='private staging link'):
            create_backup(app, destination)
    assert destination.is_dir() and not (destination / 'manifest.json').exists()
    with pytest.raises(FileExistsError):
        create_backup(app, destination)
    assert (app.root / 'reading-state.json').read_bytes() == original_state
    assert ReadingState(app.root).bookmarks(workspace, 'edition') == (bookmark,)
    assert ReadingDesk(app, workspace, 'edition').page(1).text == page.text
    retry = tmp_path / 'retry'
    checksum = create_backup(app, retry)
    assert verify_backup(retry, checksum)['version'] == 4


@pytest.mark.parametrize('failure', ['partial_write', 'competing_file'])
def test_completion_manifest_write_is_checked_and_never_replaces_another_file(
        produced, tmp_path, monkeypatch, failure):
    app, *_ = produced
    destination = tmp_path / 'incomplete'
    sync = library_backup._sync_file
    written = None

    def interrupted(path, raw):
        nonlocal written
        if path == destination / 'manifest.json':
            written = raw[:len(raw) // 2] if failure == 'partial_write' else b'Unrelated file.'
            sync(path, written)
            if failure == 'partial_write':
                raise OSError('Injected incomplete manifest write')
        return sync(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(library_backup, '_sync_file', interrupted)
        with pytest.raises(OSError):
            create_backup(app, destination)
    assert written is not None
    assert (destination / 'manifest.json').read_bytes() == written
    with pytest.raises(ValueError, match='manifest'):
        verify_backup(destination, sha256(written).hexdigest())
    with pytest.raises(ValueError, match='manifest'):
        restore_backup(destination, tmp_path / 'rejected', sha256(written).hexdigest())
    assert not (tmp_path / 'rejected').exists()
    with pytest.raises(FileExistsError):
        create_backup(app, destination)
    assert (destination / 'manifest.json').read_bytes() == written
