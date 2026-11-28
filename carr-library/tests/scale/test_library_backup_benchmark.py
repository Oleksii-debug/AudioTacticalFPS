"""Real small-corpus contracts for the separate common4 scale measurement.

These tests exercise the same public import and backup APIs. They qualify the
measurement adapter, not the final 50k-book or 2GiB source tiers.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget
from carr_library.durable_state import read_json

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_library_backup.py"


@pytest.fixture(scope="module")
def adapter():
    spec = importlib.util.spec_from_file_location("_actual_library_backup_benchmark", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepared(adapter, tmp_path, *, kind="corpus", complete=True):
    directory = tmp_path / "real-backup-corpus"
    spec = (adapter.fixture.CorpusSpec(books=2, segments_per_book=3, segment_bytes=256)
            if kind == "corpus" else adapter.fixture.LargeTextSpec(2*adapter.MIB, 4))
    generation = adapter.fixture.generate_fixture(directory, spec,
        search_spool_budget_bytes=4*adapter.MIB, free_reserve_bytes=0,
        memory_headroom_bytes=128*adapter.MIB)
    assert generation["status"] == "FIXTURE_GENERATED_NOT_BENCHMARKED", generation
    state = adapter.catalog.initialize(directory, page_files=1, slice_items=1,
        steps=2, slice_seconds=60, memory_bytes=512*adapter.MIB,
        search_disk_bytes=32*adapter.MIB, reserve_bytes=0, foreign_libraries=1)
    assert state["books"] == generation["completed_books"], state
    result = adapter.catalog.run_import(directory, seconds=180,
        **({} if complete else {"stop_after_slices": 1}))
    assert result["status"] == ("IMPORT_COMPLETE" if complete else "PAUSED_BETWEEN_REAL_SLICES")
    return directory, generation


def files(directory):
    return {path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def run(adapter, directory, **kwargs):
    return adapter.backup_restore(directory, seconds=180, memory_bytes=adapter.GIB,
                                  reserve_bytes=0, **kwargs)


@pytest.mark.parametrize("kind", ["corpus", "large_text"])
def test_real_common4_roundtrip_uses_one_external_policy_and_preserves_every_oracle(
        adapter, tmp_path, monkeypatch, kind):
    directory, generation = prepared(adapter, tmp_path, kind=kind)
    original_sources = files(directory / "sources")
    original_state = (directory / "benchmark" / "state.json").read_bytes()
    calls = []
    create = LibraryApp.backup
    verify = adapter.library_backup.verify_backup
    restore = adapter.library_backup.restore_backup

    def observed_create(app, destination, **options):
        assert type(options["backup_budget"]) is BackupBudget
        assert not Path(destination).exists()
        calls.append(("create", options["backup_budget"], options["cancelled"]))
        return create(app, destination, **options)

    def observed_verify(source, expected, **options):
        assert calls and options["backup_budget"] is calls[0][1]
        assert expected == sha256((Path(source) / "manifest.json").read_bytes()).hexdigest()
        calls.append(("verify", options["backup_budget"], options["cancelled"]))
        return verify(source, expected, **options)

    def observed_restore(source, target, expected, **options):
        assert options["backup_budget"] is calls[0][1] and not Path(target).exists()
        calls.append(("restore", options["backup_budget"], options["cancelled"]))
        return restore(source, target, expected, **options)

    def forbidden(*args, **kwargs):
        raise AssertionError("A measured restore must not launch a child or resume an import.")

    monkeypatch.setattr(LibraryApp, "backup", observed_create)
    monkeypatch.setattr(adapter.library_backup, "verify_backup", observed_verify)
    monkeypatch.setattr(adapter.library_backup, "restore_backup", observed_restore)
    monkeypatch.setattr(LibraryApp, "run_import_batch", forbidden)
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = run(adapter, directory)
    assert [name for name, _, _ in calls] == ["create", "verify", "restore"]
    assert all(callback is calls[0][2] for _, _, callback in calls)
    assert result["status"] == "BACKUP_RESTORE_VERIFIED"
    assert result["source_books"] == generation["completed_books"]
    assert result["source_segments"] == generation["segments"]
    assert all(result[key] is True for key in (
        "source_inventory_equal", "archive_inventory_equal", "catalog_equal",
        "raw_equal", "external_originals_equal", "implementation_unchanged"))
    assert result["source_oracle"] == result["restored_oracle"]
    assert result["source_oracle"]["source_segments"] == generation["segments"]
    assert result["passive_restore"]["completed_rows"] == generation["completed_books"]
    assert result["passive_restore"]["fresh_scan_review_required"]
    assert result["passive_restore"]["old_attempts_removed"]
    assert result["passive_restore"]["automatic_resume"] is False
    assert result["last_verified_source_copy"]["actual_verified_copy"]
    assert result["last_verified_source_copy"]["foreign_scope_refused"]
    assert result["last_verified_source_copy"]["last_segment"] == (3 if kind == "corpus" else 4)
    assert result["policy"] == calls[0][1].to_dict()
    assert result["admission"]["existing_fixture"]["logical_bytes"] > generation["generated_bytes"]
    assert result["admission"]["additional_peak_estimate_bytes"] >= (
        result["archive_inventory"]["logical_bytes"] + result["policy"]["staging_bytes"])
    assert all(result["timings"][name] > 0 for name in ("backup_seconds", "verify_seconds", "restore_seconds"))
    assert result["memory"]["coordinator_peak_bytes"] > 0
    assert files(directory / "sources") == original_sources
    assert (directory / "benchmark" / "state.json").read_bytes() == original_state
    assert read_json(directory / "benchmark" / adapter.REPORT, maximum=adapter.MIB) == result
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == generation["completed_books"]


@pytest.mark.parametrize("resource", ["disk", "physical-memory", "selected-policy"])
def test_real_resource_refusal_never_creates_an_archive_or_restored_library(
        adapter, tmp_path, monkeypatch, capsys, resource):
    directory, generation = prepared(adapter, tmp_path)
    before = files(directory / "library")
    if resource == "disk":
        actual = adapter.shutil.disk_usage
        observed = []

        def exhausted(path):
            measured = actual(path)
            observed.append(Path(path))
            return SimpleNamespace(total=measured.total, used=measured.used, free=0)

        monkeypatch.setattr(adapter.shutil, "disk_usage", exhausted)
    elif resource == "physical-memory":
        actual = adapter.fixture.available_memory

        def exhausted():
            measured = actual()
            return {**measured, "available_bytes": 0}

        monkeypatch.setattr(adapter.fixture, "available_memory", exhausted)
    memory = "1" if resource == "selected-policy" else "1024"
    code = adapter.main(["backup", str(directory), "--seconds", "180",
                         "--memory-mib", memory, "--reserve-mib", "0"])
    assert code == 3
    result = read_json(directory / "benchmark" / adapter.REPORT, maximum=adapter.MIB)
    assert result["status"] == "NOT_RUN"
    assert result["archive_created"] is False and result["restore_created"] is False
    assert not (directory / "benchmark" / adapter._ARCHIVE).exists()
    assert not (directory / "benchmark" / adapter._RESTORED).exists()
    assert files(directory / "library") == before
    assert result["source_books"] == generation["completed_books"]
    if resource == "disk":
        assert directory in observed
        assert result["admission"]["disk"]["free_bytes"] == 0
        assert result["admission"]["existing_fixture"]["logical_bytes"] > 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "NOT_RUN"


def test_saved_partial_source_requires_explicit_import_completion_before_backup_measurement(adapter, tmp_path):
    directory, generation = prepared(adapter, tmp_path, complete=False)
    source = files(directory / "library")
    with pytest.raises(AssertionError, match="Complete the actual public import"):
        run(adapter, directory)
    assert files(directory / "library") == source
    assert not (directory / "benchmark" / adapter.REPORT).exists()
    assert not (directory / "benchmark" / adapter._ARCHIVE).exists()
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == 2


def test_existing_selected_output_is_never_overwritten_or_adopted(adapter, tmp_path):
    directory, _ = prepared(adapter, tmp_path)
    archive = directory / "benchmark" / adapter._ARCHIVE
    archive.mkdir()
    (archive / "unrelated.bin").write_bytes(b"Keep the exact existing selected bytes.")
    before = files(archive)
    with pytest.raises(FileExistsError, match="never adopts or overwrites"):
        run(adapter, directory)
    assert files(archive) == before
    assert not (directory / "benchmark" / adapter.REPORT).exists()
    assert not (directory / "benchmark" / adapter._RESTORED).exists()


def test_same_size_mutated_original_fails_before_any_backup_output(adapter, tmp_path):
    directory, _ = prepared(adapter, tmp_path)
    selected = directory / "sources" / "book-00001.txt"
    original = selected.read_bytes()
    before = selected.stat()
    changed = original.replace(b"commonrailway", b"COMMONRAILWAY", 1)
    assert changed != original and len(changed) == len(original)
    selected.write_bytes(changed)
    os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="original source hash or length changed"):
        run(adapter, directory)
    assert selected.read_bytes() == changed
    assert not (directory / "benchmark" / adapter._ARCHIVE).exists()
    assert not (directory / "benchmark" / adapter.REPORT).exists()


def test_real_archive_tamper_is_rejected_by_current_verifier_without_restoring(
        adapter, tmp_path, monkeypatch):
    directory, _ = prepared(adapter, tmp_path)
    before = files(directory / "library")
    actual = adapter.library_backup.verify_backup
    called = []

    def changed_archive(source, expected, **options):
        called.append((Path(source), expected))
        selected = Path(source) / "manifest.json"
        raw = selected.read_bytes()
        selected.write_bytes(raw + b" ")
        return actual(source, expected, **options)

    monkeypatch.setattr(adapter.library_backup, "verify_backup", changed_archive)
    with pytest.raises(ValueError):
        run(adapter, directory)
    assert len(called) == 1 and (called[0][0] / "manifest.json").exists()
    assert sha256((called[0][0] / "manifest.json").read_bytes()).hexdigest() != called[0][1]
    assert files(directory / "library") == before
    assert not (directory / "benchmark" / adapter._RESTORED).exists()
    assert not (directory / "benchmark" / adapter.REPORT).exists()


def test_cancellation_after_actual_restore_database_copy_preserves_completed_archive(
        adapter, tmp_path, monkeypatch):
    directory, _ = prepared(adapter, tmp_path)
    copied, in_restore = [], False
    actual_restore = adapter.library_backup.restore_backup
    actual_copy = adapter.library_backup._copy_database

    def begin_restore(*args, **options):
        nonlocal in_restore
        in_restore = True
        return actual_restore(*args, **options)

    def copied_database(source, target, manifest, **options):
        result = actual_copy(source, target, manifest, **options)
        if in_restore:
            assert Path(target).is_file()
            copied.append(Path(target))
        return result

    monkeypatch.setattr(adapter.library_backup, "restore_backup", begin_restore)
    monkeypatch.setattr(adapter.library_backup, "_copy_database", copied_database)
    with pytest.raises(InterruptedError):
        run(adapter, directory, cancelled=lambda: bool(copied))
    assert len(copied) == 1 and not copied[0].exists()
    archive = directory / "benchmark" / adapter._ARCHIVE
    checksum = sha256((archive / "manifest.json").read_bytes()).hexdigest()
    assert checksum and not (directory / "benchmark" / adapter._RESTORED).exists()
    assert not (directory / "benchmark" / adapter.REPORT).exists()
    # The cancellation never turns an already complete archive into a claimed
    # rollback. Independently verify it through the real current API afterward.
    policy = BackupBudget(memory_bytes=adapter.GIB, staging_bytes=256*adapter.MIB, seconds=120)
    assert adapter.library_backup.verify_backup(archive, checksum, backup_budget=policy)["version"] == 4


def test_extra_actual_restored_workspace_is_detected_by_full_catalog_oracle(
        adapter, tmp_path, monkeypatch):
    directory, _ = prepared(adapter, tmp_path)
    actual = adapter.library_backup.restore_backup
    added = []

    def altered_restored_catalog(*args, **options):
        target = actual(*args, **options)
        app = LibraryApp(target)
        try:
            added.append(app.create_workspace("An unrequested extra restored scope"))
        finally:
            assert app.close_search()
        return target

    monkeypatch.setattr(adapter.library_backup, "restore_backup", altered_restored_catalog)
    with pytest.raises(AssertionError, match="full canonical catalog"):
        run(adapter, directory)
    assert len(added) == 1
    assert (directory / "benchmark" / adapter._RESTORED / "library.sqlite").exists()
    assert not (directory / "benchmark" / adapter.REPORT).exists()


@pytest.mark.parametrize("options", [
    {"seconds": float("nan")}, {"seconds": float("inf")}, {"seconds": 0},
    {"memory_bytes": 0}, {"memory_bytes": True}, {"reserve_bytes": -1},
])
def test_invalid_caller_resources_are_refused_before_any_fixture_creation(adapter, tmp_path, options):
    selected = tmp_path / "does-not-exist"
    with pytest.raises(ValueError):
        adapter.backup_restore(selected, **options)
    assert not selected.exists()
