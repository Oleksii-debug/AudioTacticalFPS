"""An unreadable optional producer must not become a complete empty backup."""
import os
from pathlib import Path
import stat

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_files import optional_present, regular
from carr_library.library_backup import create_backup, restore_backup, verify_backup


ROOTS = (
    'audio-projects', 'state/assistant', 'state/translation', 'transcription-jobs',
    'state/operations', 'state/studio/recordings', 'state/speech', 'state/ui',
    'state/historical-backups', 'narration-imports', 'narration-projects',
    'import-requests', 'import-batches',
)


@pytest.mark.parametrize('name', ROOTS)
@pytest.mark.parametrize('failure', [PermissionError, NotADirectoryError])
def test_complete_backup_refuses_denied_optional_producer_lookup(tmp_path, monkeypatch, name, failure):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Original')
    target = app.root / name
    target.mkdir(parents=True, exist_ok=True)
    database = app.database.read_bytes()
    lstat, lexists = Path.lstat, os.path.lexists

    def denied(path, *args, **kwargs):
        if path == target:
            raise failure('typed producer lookup unavailable')
        return lstat(path, *args, **kwargs)

    def suppressed(path):
        # Reproduce the historical error suppression independently of Python
        # version/platform; the current producer must use its real lstat probe.
        return False if Path(path) == target else lexists(path)

    backup = tmp_path / 'backup'
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', denied)
        patch.setattr(os.path, 'lexists', suppressed)
        with pytest.raises(failure, match='typed producer lookup unavailable'):
            create_backup(app, backup)
    assert not backup.exists() and app.database.read_bytes() == database
    assert target.is_dir()


def test_genuine_absent_and_empty_optional_roots_remain_valid_roundtrip(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Original')
    for name in ROOTS:
        assert not optional_present(app.root / name)
    original = create_backup(app, tmp_path / 'without-state')
    verify_backup(tmp_path / 'without-state', original)
    for name in ROOTS:
        (app.root / name).mkdir(parents=True, exist_ok=True)
    # Empty namespaces are not credentials, executable profiles or extra data.
    digest = create_backup(app, tmp_path / 'empty-state')
    verify_backup(tmp_path / 'empty-state', digest)
    restore_backup(tmp_path / 'empty-state', tmp_path / 'restored', digest)
    assert (tmp_path / 'restored' / 'library.sqlite').is_file()


def test_a_root_disappearing_after_presence_cannot_be_silently_omitted(tmp_path, monkeypatch):
    from carr_library.library_backup import _projects

    root = tmp_path / 'library'
    folder = root / 'audio-projects'
    folder.mkdir(parents=True)
    lstat = Path.lstat
    seen = []
    def disappears(path, *args, **kwargs):
        if path == folder:
            seen.append(True)
            if len(seen) > 1:
                raise FileNotFoundError('root disappeared after presence')
        return lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', disappears)
    with pytest.raises(FileNotFoundError, match='disappeared'):
        _projects(root)
    assert len(seen) == 2


def test_non_directory_ancestor_is_an_error_not_absence(tmp_path):
    (tmp_path / 'file').write_bytes(b'preserved')
    with pytest.raises((NotADirectoryError, ValueError)):
        optional_present(tmp_path / 'file' / 'child')
    assert (tmp_path / 'file').read_bytes() == b'preserved'


def test_windows_missing_descendant_mapping_does_not_hide_file_parent(tmp_path, monkeypatch):
    parent = tmp_path / 'file'
    parent.write_bytes(b'preserved')
    target = parent / 'child'
    lstat = Path.lstat
    def mapped(path, *args, **kwargs):
        if path == target:
            raise FileNotFoundError('Windows PATH_NOT_FOUND for a file parent')
        return lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', mapped)
    with pytest.raises(ValueError, match='regular local'):
        optional_present(target)
    assert parent.read_bytes() == b'preserved'


def test_dangling_link_metadata_is_present_then_rejected_by_existing_regular_guard(tmp_path, monkeypatch):
    path = tmp_path / 'link'
    lstat = Path.lstat
    # The metadata contract is portable without requiring Windows link privileges.
    linked = os.stat_result((stat.S_IFLNK | 0o777, 1, 1, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(Path, 'lstat', lambda item, *a, **kw:
                        linked if item == path else lstat(item, *a, **kw))
    assert optional_present(path)
    with pytest.raises(ValueError, match='regular local'):
        regular(path)


def test_unregistered_local_engine_profile_is_still_never_collected(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Original')
    private = app.root / 'state' / 'audio' / 'engines.json'
    private.parent.mkdir(parents=True)
    private.write_bytes(b'local-only profile: deliberately not portable')
    lstat = Path.lstat
    def forbidden(path, *args, **kwargs):
        if path == private:
            raise AssertionError('A local executable profile is outside typed backup scope.')
        return lstat(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', forbidden)
        digest = create_backup(app, tmp_path / 'backup')
        verify_backup(tmp_path / 'backup', digest)
    assert private.read_bytes() == b'local-only profile: deliberately not portable'


@pytest.mark.parametrize('restore', [False, True])
def test_failed_pending_publication_enumeration_never_marks_complete(tmp_path, monkeypatch, restore):
    """Exercise the real walk of a fully verified staged pending component."""
    import carr_library.library_backup as backup
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending source')
    original = tmp_path / 'queued.txt'
    original.write_text('Original retained pending source.', encoding='utf-8')
    app.plan_import_batch(workspace, [original])
    before = app.database.read_bytes()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder) if restore else None
    destination = tmp_path / 'restored' if restore else folder
    publish, scandir = backup.publish_tree, os.scandir
    blocked, attempts = [], []
    def publishing(source, target, **kwargs):
        blocked.append(Path(source))
        return publish(source, target, **kwargs)
    def denied(path):
        if not isinstance(path, int) and Path(path) in blocked:
            attempts.append(Path(path))
            raise PermissionError('staged pending inventory unreadable')
        return scandir(path)
    with monkeypatch.context() as patch:
        patch.setattr(backup, 'publish_tree', publishing)
        patch.setattr(os, 'scandir', denied)
        with pytest.raises(PermissionError, match='pending inventory unreadable'):
            if restore:
                restore_backup(folder, destination, digest)
            else:
                create_backup(app, destination)
    assert attempts and app.database.read_bytes() == before
    assert original.read_text(encoding='utf-8') == 'Original retained pending source.'
    if restore:
        assert (destination / backup.INCOMPLETE).is_file()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(destination)
        verify_backup(folder, digest)
    else:
        assert not (destination / backup.MANIFEST).exists()
