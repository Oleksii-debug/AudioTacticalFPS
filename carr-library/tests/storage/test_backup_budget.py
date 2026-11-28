"""Exact external backup policies; archive contents cannot raise caller limits."""
from dataclasses import replace
from pathlib import Path
import pytest

from carr_library.backup_budget import (BackupBudget, checked_budget, bounded_cancel,
    check_metadata_memory, source_limit, MIB, GIB)
from carr_library import backup_budget as module
from carr_library import library_backup
from carr_library.application import LibraryApp


@pytest.mark.parametrize('field,value', [
    ('assets', True), ('assets', 0), ('assets', 2**31),
    ('source_bytes', 100), ('original_bytes', True), ('original_bytes', 2**63),
    ('database_bytes', -1), ('pending_files', 1.5), ('seconds', float('inf')),
    ('seconds', True), ('seconds', 86401), ('manifest_bytes', 257*MIB),
    ('memory_bytes', 64*MIB), ('staging_bytes', 0),
])
def test_only_exact_finite_known_policy_fields_are_accepted(field, value):
    raw = BackupBudget().to_dict()
    raw[field] = value
    with pytest.raises((ValueError, TypeError)):
        BackupBudget.from_dict(raw)


def test_explicit_large_counts_and_bytes_are_caller_data_not_archive_authority():
    selected = BackupBudget(original_bytes=2*GIB, database_bytes=32*GIB,
        assets=50_000, asset_bytes=64*GIB, journal_bytes=32*GIB,
        pending_files=200_000, pending_bytes=64*GIB, jobs=50_000,
        checkpoints=2_000_000, manifest_bytes=64*MIB,
        seconds=86400, memory_bytes=4*GIB, staging_bytes=256*GIB)
    assert BackupBudget.from_dict(selected.to_dict()) == selected
    assert checked_budget(selected) is selected and checked_budget(None) is None
    assert source_limit('text/plain', selected) == 2*GIB
    assert source_limit('application/pdf', selected) == 64*MIB
    assert source_limit('image/tiff', selected) == 64*MIB
    assert source_limit('application/epub+zip', selected) == 64*MIB
    with pytest.raises(TypeError):
        checked_budget(selected.to_dict())


def test_one_selected_operation_deadline_is_not_reset_by_nested_checks(monkeypatch):
    current = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: current[0])
    selected = replace(BackupBudget(), seconds=2)
    stopped = bounded_cancel(lambda: False, selected)
    assert stopped() is False
    current[0] = 101.9
    assert stopped() is False
    current[0] = 102.0
    with pytest.raises(TimeoutError, match='work allowance'):
        stopped()


def test_user_stop_has_priority_and_default_callback_is_unchanged():
    original = lambda: True
    assert bounded_cancel(original, None) is original
    assert bounded_cancel(original, BackupBudget())() is True


def test_metadata_accounting_combines_live_wire_bytes_and_record_count():
    selected = replace(BackupBudget(), memory_bytes=768*MIB)
    check_metadata_memory(8*MIB, 1000, selected)
    with pytest.raises(ValueError, match='memory allowance'):
        check_metadata_memory(32*MIB, 1, selected)
    with pytest.raises(ValueError, match='memory allowance'):
        check_metadata_memory(0, 300_000, selected)
    # Omitted policy retains the legacy checked decoder limits.
    check_metadata_memory(32*MIB, 300_000, None)


def test_explicit_manifest_limit_is_checked_before_its_descriptor_is_read(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded manifest')
    app.add_text(workspace, 'Source', 'Exact original.')
    folder = tmp_path / 'backup'
    checksum = library_backup.create_backup(app, folder)
    original = Path.open
    def guarded(path, *args, **kwargs):
        if path == folder / 'manifest.json':
            pytest.fail('A manifest above the selected byte budget must not be opened.')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    with pytest.raises(ValueError, match='manifest size limit'):
        library_backup.verify_backup(folder, checksum, backup_budget=BackupBudget(
            manifest_bytes=1, memory_bytes=768*MIB, staging_bytes=GIB))


@pytest.mark.parametrize('selected', [None, BackupBudget()])
def test_invalid_cancellation_retains_existing_value_error_contract(selected):
    with pytest.raises(ValueError, match='callback'):
        bounded_cancel(True, selected)


def test_nested_deadline_retains_first_clock_and_rejects_replacement(monkeypatch):
    current = [10.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: current[0])
    selected = replace(BackupBudget(), seconds=2)
    first = bounded_cancel(lambda: False, selected)
    current[0] = 11.9
    assert bounded_cancel(first, selected) is first
    with pytest.raises(ValueError, match='retain'):
        bounded_cancel(first, replace(selected, seconds=20))
    current[0] = 12.0
    with pytest.raises(TimeoutError):
        bounded_cancel(bounded_cancel(first, selected), selected)()


def test_reduced_child_policy_keeps_outer_operation_deadline(monkeypatch):
    from carr_library.backup_incremental import _reserved_budget, _child_cancel
    current = [10.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: current[0])
    selected = replace(BackupBudget(), seconds=2)
    first = bounded_cancel(lambda: False, selected)
    current[0] = 11.9
    reduced = _reserved_budget(selected, MIB)
    child = bounded_cancel(_child_cancel(first, reduced), reduced)
    assert reduced.staging_bytes == selected.staging_bytes - MIB
    assert child() is False
    current[0] = 12.0
    with pytest.raises(TimeoutError):
        child()


def test_host_preflight_refuses_low_resources_without_creating_paths(tmp_path, monkeypatch):
    selected = replace(BackupBudget(), memory_bytes=768*MIB, staging_bytes=128*MIB)
    monkeypatch.setattr(module, 'available_memory', lambda: 767*MIB)
    with pytest.raises(ValueError, match='free memory'):
        module.preflight(tmp_path, selected)
    monkeypatch.setattr(module, 'available_memory', lambda: selected.memory_bytes)
    from types import SimpleNamespace
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=191*MIB))
    with pytest.raises(ValueError, match='free disk'):
        module.preflight(tmp_path, selected)
    assert not list(tmp_path.iterdir())


def test_no_archive_policy_field_can_extend_current_full_format(tmp_path):
    from hashlib import sha256
    import json
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('No archive authority')
    assert app.add_text(workspace, 'Original', 'Exact synthetic text.')['state'] == 'complete'
    backup = tmp_path / 'backup'
    library_backup.create_backup(app, backup)
    path = backup / 'manifest.json'
    value = json.loads(path.read_bytes())
    value['backup_budget'] = BackupBudget(original_bytes=2*GIB).to_dict()
    raw = (json.dumps(value, sort_keys=True) + '\n').encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        library_backup.verify_backup(backup, sha256(raw).hexdigest())
