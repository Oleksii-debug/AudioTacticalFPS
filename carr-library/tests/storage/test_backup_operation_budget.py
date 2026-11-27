"""Real library backup budgets and cancellation at the completion boundary."""
from hashlib import sha256
import json
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup
from carr_library.ingest.restore import RestoreError
from carr_library.progress import (
    FILENAME as PROGRESS_SETTINGS, ProgressSettings, ProgressSettingsStore,
)


@pytest.fixture
def budget_library(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека з лімітами')
    first = app.create_workspace('First')
    second = app.create_workspace('Second')
    app.add_text(first, 'Перший оригінал', 'Незмінний оригінал. Railway evidence.')
    app.add_text(second, 'Second original', 'Separate second source. Harbour evidence.')
    settings = ProgressSettings(mode='beep', volume=35, interval_seconds=7)
    ProgressSettingsStore(app.root).save(settings)
    originals = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    return app, first, second, settings, originals


def delivered(folder):
    files = [path for path in folder.rglob('*') if path.is_file()]
    return sum(path.stat().st_size for path in files), len(files)


def assert_source_unchanged(app, originals):
    assert {path.name: path.read_bytes() for path in app.assets_dir.iterdir()} == originals
    assert all(path.stat().st_nlink == 1 for path in app.assets_dir.iterdir())


@pytest.mark.parametrize('limited', ('max_bytes', 'max_files'))
def test_exact_delivered_budget_includes_every_file_and_final_manifest(
        budget_library, tmp_path, limited):
    app, first, second, settings, originals = budget_library
    reference = tmp_path / 'reference'
    library_backup.create_backup(app, reference)
    manifest = json.loads((reference / library_backup.MANIFEST).read_bytes())
    assert manifest['version'] == 4
    assert manifest['provider_restore_mode'] == 'LOCAL_ONLY'
    assert len(manifest['assets']) == 2
    assert {row['filename'] for row in manifest['sidecars']} == {PROGRESS_SETTINGS}
    total_bytes, total_files = delivered(reference)
    assert total_files == 2 + len(manifest['assets']) + len(manifest['sidecars'])
    assert total_bytes == (
        manifest['database']['size_bytes']
        + sum(row['size_bytes'] for row in manifest['assets'])
        + sum(row['size_bytes'] for row in manifest['sidecars'])
        + (reference / library_backup.MANIFEST).stat().st_size)
    allowance = dict(max_bytes=total_bytes, max_files=total_files)
    too_small = dict(allowance)
    too_small[limited] -= 1
    rejected = tmp_path / 'over-budget'
    with pytest.raises(RestoreError, match='byte limit' if limited == 'max_bytes' else 'file limit'):
        library_backup.create_backup(app, rejected, **too_small)
    assert not rejected.exists()
    assert not (rejected / library_backup.MANIFEST).exists()

    accepted = tmp_path / 'exact-budget'
    digest = library_backup.create_backup(app, accepted, **allowance)
    assert delivered(accepted) == (total_bytes, total_files)
    assert digest == sha256((accepted / library_backup.MANIFEST).read_bytes()).hexdigest()
    restored = LibraryApp(library_backup.restore_backup(accepted, tmp_path / 'restored', digest))
    assert len(restored.search(first, 'Railway')) == 1
    assert len(restored.search(second, 'Harbour')) == 1
    assert ProgressSettingsStore(restored.root).load() == settings
    assert {path.name: path.read_bytes() for path in restored.assets_dir.iterdir()} == originals
    assert_source_unchanged(app, originals)


@pytest.mark.parametrize(('name', 'value'), (
    ('max_bytes', True), ('max_bytes', 0), ('max_bytes', -1), ('max_bytes', '1000'),
    ('max_bytes', 1000.0), ('max_bytes', library_backup.MAX_BACKUP_BYTES + 1),
    ('max_files', True), ('max_files', 0), ('max_files', -1), ('max_files', '10'),
    ('max_files', 10.0), ('max_files', library_backup.MAX_BACKUP_FILES + 1),
))
def test_invalid_or_raised_backup_limit_is_rejected_before_publication(
        budget_library, tmp_path, name, value):
    app, _, _, _, originals = budget_library
    destination = tmp_path / 'invalid-budget'
    with pytest.raises(RestoreError, match='invalid backup'):
        library_backup.create_backup(app, destination, **{name: value})
    assert not destination.exists()
    assert_source_unchanged(app, originals)


def test_file_budget_cannot_omit_database_or_manifest(budget_library, tmp_path):
    app, _, _, _, _ = budget_library
    destination = tmp_path / 'one-file'
    with pytest.raises(RestoreError, match='file limit'):
        library_backup.create_backup(app, destination, max_files=1)
    assert not destination.exists()


def test_pre_cancelled_backup_publishes_nothing(budget_library, tmp_path):
    app, _, _, _, originals = budget_library
    destination = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError, match='cancelled'):
        library_backup.create_backup(app, destination, cancelled=lambda: True)
    assert not destination.exists()
    assert_source_unchanged(app, originals)


def test_cancellation_after_real_private_staging_publishes_nothing(
        budget_library, tmp_path, monkeypatch):
    app, _, _, _, originals = budget_library
    stopped = Event()
    snapshot_sidecars = library_backup._snapshot_sidecars

    def after_snapshot(*args, **kwargs):
        value = snapshot_sidecars(*args, **kwargs)
        stopped.set()
        return value

    monkeypatch.setattr(library_backup, '_snapshot_sidecars', after_snapshot)
    destination = tmp_path / 'cancelled-before-publication'
    with pytest.raises(InterruptedError, match='cancelled'):
        library_backup.create_backup(app, destination, cancelled=stopped.is_set)
    assert stopped.is_set()
    assert not destination.exists()
    assert_source_unchanged(app, originals)


@pytest.mark.parametrize('last_published', (library_backup.DATABASE, PROGRESS_SETTINGS))
def test_cancellation_during_publication_leaves_no_restorable_manifest(
        budget_library, tmp_path, last_published):
    app, _, _, _, originals = budget_library
    reference = tmp_path / 'reference'
    expected = library_backup.create_backup(app, reference)
    destination = tmp_path / 'interrupted'
    with pytest.raises(InterruptedError, match='cancelled'):
        library_backup.create_backup(app, destination,
            cancelled=lambda: (destination / last_published).is_file())
    assert (destination / last_published).is_file()
    assert not (destination / library_backup.MANIFEST).exists()
    restored = tmp_path / 'must-not-restore'
    with pytest.raises(RestoreError, match='final manifest'):
        library_backup.restore_backup(destination, restored, expected)
    assert not restored.exists()
    assert_source_unchanged(app, originals)


def test_cancellation_after_final_manifest_preserves_actual_completion(
        budget_library, tmp_path, monkeypatch):
    app, first, _, _, originals = budget_library
    destination = tmp_path / 'completed'
    stopped = Event()
    link = library_backup.os.link

    def after_link(source, target, *args, **kwargs):
        result = link(source, target, *args, **kwargs)
        if Path(target) == destination / library_backup.MANIFEST:
            stopped.set()
        return result

    monkeypatch.setattr(library_backup.os, 'link', after_link)
    digest = library_backup.create_backup(app, destination, cancelled=stopped.is_set)
    assert stopped.is_set()
    assert digest == sha256((destination / library_backup.MANIFEST).read_bytes()).hexdigest()
    restored = LibraryApp(library_backup.restore_backup(
        destination, tmp_path / 'restored-completed', digest))
    assert len(restored.search(first, 'Railway')) == 1
    assert_source_unchanged(app, originals)


def test_invalid_cancellation_callback_cannot_publish(budget_library, tmp_path):
    app, _, _, _, _ = budget_library
    destination = tmp_path / 'invalid-callback'
    with pytest.raises(RestoreError, match='cancellation callback'):
        library_backup.create_backup(app, destination, cancelled=True)
    assert not destination.exists()
