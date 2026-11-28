"""Actual native full/incremental snapshots obey caller budgets before publication."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget, MIB
from carr_library import library_backup as full
from carr_library.backup_incremental import (BackupReference, create_incremental_backup,
    verify_incremental_backup, restore_incremental_backup, _reserved_budget)
from carr_library.storage.revisions import Storage


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual budget acceptance')
    assert app.add_text(workspace, 'Original', 'Exact Ω original.\r\nSecond source sentence.',
                        edition_id='original')['state'] == 'complete'
    yield app, workspace


def policy(**changes):
    return replace(BackupBudget(memory_bytes=768*MIB, staging_bytes=256*MIB, seconds=300), **changes)


def test_omitted_and_explicit_native_snapshot_keep_identical_manifest_bytes(library, tmp_path):
    app, workspace = library
    ordinary, selected = tmp_path / 'ordinary', tmp_path / 'selected'
    first = full.create_backup(app, ordinary)
    second = full.create_backup(app, selected, backup_budget=policy())
    assert first == second
    assert (ordinary / 'manifest.json').read_bytes() == (selected / 'manifest.json').read_bytes()
    manifest = full.verify_backup(selected, second, backup_budget=policy())
    assert 'backup_budget' not in manifest
    restored = tmp_path / 'restored'
    full.restore_backup(selected, restored, second, backup_budget=policy())
    recovered = LibraryApp(restored)
    hit, = recovered.search(workspace, 'Second source')
    assert recovered.copy(workspace, hit.ref, hit.revision, 'quote') == 'Second source'


def test_explicit_database_allowance_requires_new_restore_choice(library, tmp_path, monkeypatch):
    app, _ = library
    with closing(Storage(app.database)) as storage:
        actual = storage.db.execute('PRAGMA page_count').fetchone()[0] * storage.db.execute('PRAGMA page_size').fetchone()[0]
    monkeypatch.setattr(full, 'MAX_DATABASE', actual - 1)
    refused = tmp_path / 'refused'
    with pytest.raises(ValueError):
        full.create_backup(app, refused)
    assert not refused.exists()
    target = tmp_path / 'selected'
    selected = policy(database_bytes=actual + MIB)
    checksum = full.create_backup(app, target, backup_budget=selected)
    with pytest.raises(ValueError):
        full.verify_backup(target, checksum)
    with pytest.raises(ValueError):
        full.restore_backup(target, tmp_path / 'unreviewed', checksum)
    assert not (tmp_path / 'unreviewed').exists()
    assert full.verify_backup(target, checksum, backup_budget=selected)['database']['size_bytes'] == actual
    full.restore_backup(target, tmp_path / 'reviewed', checksum, backup_budget=selected)
    assert (tmp_path / 'reviewed' / 'library.sqlite').is_file()


def test_original_staging_shortfall_is_refused_before_opening_a_destination(library, tmp_path, monkeypatch):
    app, workspace = library
    assert app.add_text(workspace, 'Larger original', 'source word ' * 5000,
                        edition_id='larger')['state'] == 'complete'
    with closing(Storage(app.database)) as storage:
        database_bytes = storage.db.execute('PRAGMA page_count').fetchone()[0] * storage.db.execute('PRAGMA page_size').fetchone()[0]
    selected = policy(manifest_bytes=MIB, staging_bytes=2*(database_bytes + MIB + 128))
    ordinary_open, writes = Path.open, []
    def observed(path, mode='r', *args, **kwargs):
        if mode == 'xb' and 'originals' in path.parts and any(
                part.startswith('.carr-library-backup-') for part in path.parts):
            writes.append(path)
            pytest.fail('Original writes began above the explicitly remaining staging budget.')
        return ordinary_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observed)
    target = tmp_path / 'never-published'
    with pytest.raises(ValueError):
        full.create_backup(app, target, backup_budget=selected)
    assert not writes and not target.exists()
    assert not list(tmp_path.glob('.carr-library-backup-*'))


def test_actual_nested_incremental_layers_retain_current_full_restore_authority(library, tmp_path):
    app, workspace = library
    selected = policy()
    base_path = tmp_path / 'base'
    base = BackupReference(base_path, full.create_backup(app, base_path, backup_budget=selected))
    assert app.add_text(workspace, 'Second', 'Second retained generation.', edition_id='second')['state'] == 'complete'
    first = create_incremental_backup(app, tmp_path / 'layer-1', base=base, backup_budget=selected)
    assert app.add_text(workspace, 'Third', 'Third retained generation.', edition_id='third')['state'] == 'complete'
    second = create_incremental_backup(app, tmp_path / 'layer-2',
        base=BackupReference(first.path, first.manifest_sha256), ancestors=(base,), backup_budget=selected)
    bases = (BackupReference(first.path, first.manifest_sha256), base)
    result = verify_incremental_backup(second.path, second.manifest_sha256, bases=bases, backup_budget=selected)
    assert result['chain'] == [second.manifest_sha256, first.manifest_sha256, base.sha256]
    restored = restore_incremental_backup(second.path, tmp_path / 'restored',
        second.manifest_sha256, bases=bases, backup_budget=selected)
    recovered = LibraryApp(restored)
    assert recovered.edition_metadata(workspace, 'second').title == 'Second'
    assert recovered.edition_metadata(workspace, 'third').title == 'Third'
    hit, = recovered.search(workspace, 'Third retained')
    assert recovered.copy(workspace, hit.ref, hit.revision, 'quote') == 'Third retained'


def test_selected_nested_copy_cannot_borrow_bytes_owned_by_its_parent():
    selected = policy(staging_bytes=128*MIB)
    reduced = _reserved_budget(selected, 96*MIB)
    assert reduced.staging_bytes == 32*MIB
    assert reduced.original_bytes == selected.original_bytes
    assert reduced.seconds == selected.seconds
    with pytest.raises(ValueError, match='staging'):
        _reserved_budget(selected, selected.staging_bytes)
    assert _reserved_budget(None, 2**63) is None


def test_cancelled_selected_full_copy_leaves_no_completion_marker(library, tmp_path):
    app, _ = library
    target = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError):
        full.create_backup(app, target, backup_budget=policy(), cancelled=lambda: True)
    assert not target.exists()
    assert not list(tmp_path.glob('.carr-library-backup-*'))
