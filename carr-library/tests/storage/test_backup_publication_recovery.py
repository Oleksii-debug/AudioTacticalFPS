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
def fail_backup_cleanup(monkeypatch):
    """Keep failed cleanup directories alive so their notices can be checked."""
    original = library_backup.TemporaryDirectory.cleanup
    retained = []

    def fail(temporary):
        if Path(temporary.name).name.startswith('.carr-library-backup-'):
            retained.append(temporary)
            raise PermissionError('Synthetic post-publication directory cleanup failure')
        return original(temporary)

    monkeypatch.setattr(library_backup.TemporaryDirectory, 'cleanup', fail)
    yield retained
    for temporary in retained:
        original(temporary)


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
    assert verify_backup(retry, checksum)['version'] == 5


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


@pytest.mark.parametrize('document', ['project', 'recovery'])
def test_actual_exit_after_first_managed_document_commit_preserves_reopen_and_backup(
        produced, tmp_path, document):
    from carr_library.ui.audio_studio import StudioController
    app, workspace, page, bookmark = produced
    code = '''
import os,sys
from pathlib import Path
from carr_library.audio import managed
from carr_library.ui.audio_studio import StudioController
studio=StudioController(private_root=Path(sys.argv[1]))
studio.new('[pause 1]',title='Crash-safe saved project',input_kind='script')
kind=sys.argv[2]
if kind=='recovery':
    studio.save_in_library()
    studio.edit_script('[pause 2]')
name='project.json' if kind=='project' else 'project.json.recovery.json'
sync=managed._sync_directory
def stop(directory):
    sync(directory)
    if (Path(directory)/name).is_file():
        os._exit(75)
managed._sync_directory=stop
if kind=='project':
    studio.save_in_library()
else:
    studio.save_recovery()
'''
    import carr_library.audio.managed as managed_module
    source = Path(managed_module.__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(source), str(source.parent)))}
    child = subprocess.run([sys.executable, '-c', code, str(app.root), document],
                           env=env, capture_output=True, text=True, timeout=30)
    assert child.returncode == 75, (child.stdout, child.stderr)
    saved, = (app.root / 'audio-projects').glob('*/studio/project.json')
    committed = saved if document == 'project' else saved.with_name(saved.name + '.recovery.json')
    assert committed.stat().st_nlink == 1
    assert not tuple(saved.parent.glob('.studio-save-*'))
    raw = committed.read_bytes()
    reopened = StudioController(private_root=app.root).open(saved)
    assert not reopened.dirty and reopened.script == '[pause 1]'
    if document == 'recovery':
        reopened.recover()
        assert reopened.script == '[pause 2]'
    folder = tmp_path / 'post-crash-backup'
    checksum = create_backup(app, folder)
    assert verify_backup(folder, checksum)['version'] == 5
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    restored_saved = restored / saved.relative_to(app.root)
    restored_project = StudioController(private_root=restored).open(restored_saved)
    if document == 'recovery':
        restored_project.recover()
    assert restored_project.script == reopened.script
    assert (restored / committed.relative_to(app.root)).read_bytes() == raw
    assert ReadingDesk(LibraryApp(restored), workspace, 'edition').page(1).text == page.text
    reopened.edit_script('[pause 3]')
    reopened.save()
    assert StudioController(private_root=app.root).open(saved).script == '[pause 3]'
    assert (folder / committed.relative_to(app.root)).read_bytes() == raw


def test_unknown_managed_document_hardlink_remains_rejected(produced, tmp_path):
    from carr_library.audio.managed import ManagedProjects
    from carr_library.ui.audio_studio import StudioController
    app, *_ = produced
    studio = StudioController(private_root=app.root)
    studio.new('[pause 1]', input_kind='script')
    saved = studio.save_in_library()
    original = saved.read_bytes()
    unrelated = tmp_path / 'unrelated-document.json'
    os.link(saved, unrelated)
    with pytest.raises(ValueError, match='hard link'):
        ManagedProjects._state(saved)
    with pytest.raises(ValueError):
        StudioController(private_root=app.root).open(saved)
    with pytest.raises(ValueError, match='hard link'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert saved.read_bytes() == original == unrelated.read_bytes()
    assert unrelated.stat().st_nlink == 2


@pytest.mark.parametrize('reporter', ['callback', 'logging', 'broken-reporters'])
def test_completed_backup_survives_cleanup_and_diagnostic_failures(
        produced, tmp_path, monkeypatch, caplog, fail_backup_cleanup, reporter):
    app, workspace, page, bookmark = produced
    before = _files(app.root)
    notices = []
    callback = notices.append if reporter == 'callback' else None
    if reporter == 'broken-reporters':
        def broken_callback(message):
            notices.append(message)
            raise RuntimeError('Synthetic warning callback failure')

        def broken_logger(*args, **kwargs):
            raise RuntimeError('Synthetic logger failure')

        callback = broken_callback
        monkeypatch.setattr(library_backup._logger, 'warning', broken_logger)
    folder = tmp_path / 'completed'
    checksum = create_backup(app, folder, on_warning=callback)
    assert type(checksum) is str and checksum == sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert verify_backup(folder, checksum)['version'] == 5
    assert len(fail_backup_cleanup) == 1
    private_stage = Path(fail_backup_cleanup[0].name)
    assert private_stage.is_dir()
    message = notices[0] if notices else caplog.text
    assert str(private_stage) in message and 'backup is complete' in message
    assert 'does not need to be repeated' in message
    assert all((app.root / name).read_bytes() == raw for name, raw in before.items())
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    assert ReadingState(restored).bookmarks(workspace, 'edition') == (bookmark,)
    assert ReadingDesk(LibraryApp(restored), workspace, 'edition').page(1).text == page.text


def test_prepublication_error_is_not_replaced_by_cleanup_failure(
        produced, tmp_path, monkeypatch, fail_backup_cleanup):
    app, *_ = produced
    before = _files(app.root)
    notices = []

    def fail_snapshot(*args, **kwargs):
        raise ValueError('Synthetic primary snapshot failure')

    monkeypatch.setattr(library_backup.backup_sidecars, 'snapshot', fail_snapshot)
    folder = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='primary snapshot failure') as error:
        create_backup(app, folder, on_warning=notices.append)
    assert not folder.exists() and notices == []
    assert any(str(fail_backup_cleanup[0].name) in note for note in error.value.__notes__)
    assert all((app.root / name).read_bytes() == raw for name, raw in before.items())


def test_backup_cli_reports_completed_digest_and_cleanup_path(
        produced, tmp_path, capsys, fail_backup_cleanup):
    from carr_library.__main__ import main
    app, *_ = produced
    folder = tmp_path / 'cli-backup'
    code = main(['--data-dir', str(app.root), '--backup', str(folder)])
    assert code == 0
    output = capsys.readouterr()
    checksum = sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert checksum in output.out and 'Backup complete.' in output.out
    assert str(fail_backup_cleanup[0].name) in output.err
    assert 'backup is complete' in output.err
    assert verify_backup(folder, checksum)['version'] == 5
