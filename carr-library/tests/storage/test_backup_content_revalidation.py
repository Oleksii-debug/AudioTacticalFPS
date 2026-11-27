"""Final byte verification for typed sidecars, including Windows metadata behavior."""
import os

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
