"""Final byte verification for typed sidecars, including Windows metadata behavior."""
import os
from pathlib import Path
import stat

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup
from carr_library.progress import FILENAME, ProgressPreferences, save_preferences


def preferences_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic backup byte identity')
    save_preferences(app.root, ProgressPreferences(volume=0.12))
    return app, app.root / FILENAME


def test_equal_metadata_content_change_cannot_publish_a_complete_backup(tmp_path, monkeypatch):
    app, source = preferences_library(tmp_path)
    original, before = source.read_bytes(), source.stat()
    replacement = original.replace(b'"volume": 0.12', b'"volume": 0.34')
    assert replacement != original and len(replacement) == len(original)
    # POSIX ctime cannot be restored. Preserve every other real file attribute
    # while modelling Windows creation-time semantics for this portable test.
    monkeypatch.setattr(library_backup, '_identity', lambda info:
        (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
         info.st_size, info.st_mtime_ns, 0))
    modified = False

    def interleaved_writer():
        nonlocal modified
        stages = list(tmp_path.glob('.carr-library-backup-*'))
        copied = [stage / 'sidecars' / FILENAME for stage in stages]
        if not modified and any(path.is_file() and path.stat().st_size == len(original)
                                for path in copied):
            modified = True
            source.write_bytes(replacement)
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        return False

    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='content changed'):
        library_backup.create_backup(app, destination, cancelled=interleaved_writer)
    assert modified and source.read_bytes() == replacement
    assert not destination.exists()


def test_final_byte_verification_obeys_cancellation_before_publication(tmp_path, monkeypatch):
    app, source = preferences_library(tmp_path)
    original = source.read_bytes()
    original_copy = library_backup._copy_sidecar
    revalidating = False

    def observed_copy(root, relative, target, identity, cancelled):
        nonlocal revalidating
        if root == app.root and relative == FILENAME and target is None:
            revalidating = True
        return original_copy(root, relative, target, identity, cancelled)

    monkeypatch.setattr(library_backup, '_copy_sidecar', observed_copy)
    destination = tmp_path / 'cancelled-backup'
    with pytest.raises(InterruptedError, match='cancelled'):
        library_backup.create_backup(app, destination, cancelled=lambda: revalidating)
    assert revalidating and source.read_bytes() == original
    assert not destination.exists()


def test_unchanged_preferences_roundtrip_and_read_only_verify(tmp_path):
    app, source = preferences_library(tmp_path)
    before = source.read_bytes()
    destination = tmp_path / 'backup'
    digest = library_backup.create_backup(app, destination)
    # Use the public verifier and restore with the independently retained hash.
    inventory = {p.relative_to(destination).as_posix(): p.read_bytes()
                 for p in destination.rglob('*') if p.is_file()}
    library_backup.verify_backup(destination, digest)
    assert {p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob('*') if p.is_file()} == inventory
    restored = library_backup.restore_backup(destination, tmp_path / 'restored', digest)
    assert (restored / FILENAME).read_bytes() == before
    assert source.read_bytes() == before


# Registered namespaces only: private executable/credential configuration is
# deliberately outside this inventory and must not be inspected or collected.
_OPTIONAL_ROOTS = (
    'state', 'audio-projects', 'state/assistant', 'state/translation',
    'state/transcription', 'transcription-jobs', 'state/operations',
    'state/studio/recordings', 'state/historical-backups',
    'narration-imports', 'narration-projects', 'import-requests',
    'import-batches', 'imports.sqlite',
)


@pytest.mark.parametrize('name', _OPTIONAL_ROOTS)
@pytest.mark.parametrize('failure', [PermissionError, NotADirectoryError])
def test_uninspectable_registered_root_cannot_publish_complete_backup(tmp_path, monkeypatch, name, failure):
    from carr_library.ingest.restore import RestoreError

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Original')
    target = app.root / name
    if name == 'imports.sqlite':
        target.write_bytes(b'An unreadable existing journal must never be omitted.')
        original = target.read_bytes()
    else:
        target.mkdir(parents=True, exist_ok=True)
        original = None
    database = app.database.read_bytes()
    lstat, lexists = Path.lstat, os.path.lexists

    def denied(path, *args, **kwargs):
        if path == target:
            raise failure('registered producer lookup unavailable')
        return lstat(path, *args, **kwargs)

    def suppressed(path):
        # Model lexists suppression on every Python/OS without depending on
        # administrative permissions or the test runner's effective account.
        return False if Path(path) == target else lexists(path)

    destination = tmp_path / 'backup'
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', denied)
        patch.setattr(os.path, 'lexists', suppressed)
        with pytest.raises(RestoreError, match='could not be inspected') as error:
            library_backup.create_backup(app, destination)
        assert isinstance(error.value.__cause__, failure)
    assert not destination.exists() and app.database.read_bytes() == database
    if original is None:
        assert target.is_dir()
    else:
        assert target.read_bytes() == original


def test_absent_and_empty_registered_roots_still_roundtrip(tmp_path):
    from carr_library.backup_files import optional_present

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original')
    for name in _OPTIONAL_ROOTS:
        assert not optional_present(app.root / name)
    absent = tmp_path / 'absent-backup'
    digest = library_backup.create_backup(app, absent)
    library_backup.verify_backup(absent, digest)
    for name in _OPTIONAL_ROOTS:
        if name != 'imports.sqlite':
            (app.root / name).mkdir(parents=True, exist_ok=True)
    empty = tmp_path / 'empty-backup'
    digest = library_backup.create_backup(app, empty)
    library_backup.verify_backup(empty, digest)
    restored = library_backup.restore_backup(empty, tmp_path / 'restored-empty', digest)
    assert [item.id for item in LibraryApp(restored).workspaces()] == [workspace]


def test_optional_root_disappearance_after_presence_stays_a_failure(tmp_path, monkeypatch):
    folder = tmp_path / 'library' / 'audio-projects'
    folder.mkdir(parents=True)
    lstat, calls = Path.lstat, []

    def disappeared(path, *args, **kwargs):
        if path == folder:
            calls.append(path)
            if len(calls) > 1:
                raise FileNotFoundError('observed root disappeared during validation')
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', disappeared)
    with pytest.raises(FileNotFoundError, match='observed root disappeared'):
        library_backup._projects(folder.parent)
    assert len(calls) == 2


def test_optional_missing_child_cannot_hide_a_regular_file_parent(tmp_path):
    from carr_library.backup_files import optional_present

    parent = tmp_path / 'ordinary-file'
    parent.write_bytes(b'Keep this original.')
    with pytest.raises(ValueError):
        optional_present(parent / 'child')
    assert parent.read_bytes() == b'Keep this original.'


def test_windows_path_not_found_mapping_cannot_hide_a_file_parent(tmp_path, monkeypatch):
    from carr_library.backup_files import optional_present

    parent = tmp_path / 'ordinary-file'
    parent.write_bytes(b'Keep this original.')
    child, lstat = parent / 'child', Path.lstat

    def mapped(path, *args, **kwargs):
        if path == child:
            raise FileNotFoundError('Windows PATH_NOT_FOUND beneath a file')
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', mapped)
    with pytest.raises(ValueError, match='regular local'):
        optional_present(child)
    assert parent.read_bytes() == b'Keep this original.'


def test_dangling_optional_link_stays_present_for_existing_path_guard(tmp_path, monkeypatch):
    from carr_library.backup_files import optional_present, regular

    path, lstat = tmp_path / 'dangling', Path.lstat
    link = os.stat_result((stat.S_IFLNK | 0o777, 1, 1, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(Path, 'lstat', lambda item, *args, **kwargs:
                        link if item == path else lstat(item, *args, **kwargs))
    assert optional_present(path)
    with pytest.raises(ValueError, match='regular local'):
        regular(path)


def test_optional_root_fix_never_collects_private_engine_configuration(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Original')
    private = app.root / 'state' / 'audio' / 'engines.json'
    private.parent.mkdir(parents=True)
    original = b'Private executable configuration stays outside portable typed state.'
    private.write_bytes(original)
    lstat = Path.lstat

    def forbidden(path, *args, **kwargs):
        if path == private:
            raise AssertionError('The backup must not inspect local engine configuration.')
        return lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', forbidden)
        digest = library_backup.create_backup(app, tmp_path / 'backup')
        library_backup.verify_backup(tmp_path / 'backup', digest)
    assert private.read_bytes() == original


@pytest.mark.parametrize('restoring', [False, True])
def test_unreadable_staged_pending_tree_never_publishes_completion(tmp_path, monkeypatch, restoring):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending source')
    original = tmp_path / 'queued.txt'
    original.write_text('Original retained pending source.', encoding='utf-8')
    app.plan_import_batch(workspace, [original])
    database = app.database.read_bytes()
    folder = tmp_path / 'backup'
    digest = library_backup.create_backup(app, folder) if restoring else None
    destination = tmp_path / 'restored' if restoring else folder
    publish, scandir = library_backup.publish_tree, os.scandir
    blocked, attempts = [], []

    def publishing(source, target, **options):
        blocked.append(Path(source))
        return publish(source, target, **options)

    def denied(path):
        if not isinstance(path, int) and Path(path) in blocked:
            attempts.append(Path(path))
            raise PermissionError('staged pending inventory unreadable')
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(library_backup, 'publish_tree', publishing)
        patch.setattr(os, 'scandir', denied)
        with pytest.raises(PermissionError, match='pending inventory unreadable'):
            if restoring:
                library_backup.restore_backup(folder, destination, digest)
            else:
                library_backup.create_backup(app, destination)
    assert attempts and app.database.read_bytes() == database
    assert original.read_text(encoding='utf-8') == 'Original retained pending source.'
    if restoring:
        assert (destination / library_backup.INCOMPLETE).is_file()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(destination)
        library_backup.verify_backup(folder, digest)
    else:
        assert not (destination / library_backup.MANIFEST).exists()
