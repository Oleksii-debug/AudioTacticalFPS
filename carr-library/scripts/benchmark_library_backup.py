"""Measure common4 backup, verification and fresh restore of a real imported corpus.

The adjacent catalog benchmark creates the genuine library through public import
pages and owned workers. This adapter adds no importer, database writer or backup
format. Every allowance is selected from current measured source inputs before
an archive exists; an archive never chooses its restore resources.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
from tempfile import gettempdir
import time

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget
from carr_library.durable_state import atomic_json, local_path, read_json, regular
from carr_library import library_backup

SCHEMA = "carr-library-backup-benchmark/1"
MIB, GIB = 1024**2, 1024**3
MAX_ROW_BYTES = 64*MIB


class _PolicyCapacity(ValueError):
    """The explicit caller allowance cannot admit the known measured metadata."""
    def __init__(self, details):
        super().__init__("Insufficient selected allowance for measured backup metadata.")
        self.details = details


REPORT = "backup-restore.json"
_ARCHIVE = "backup-full"
_RESTORED = "restored-library"

# Use exactly the adjacent reviewed adapter, with its exact adjacent generator.
# Ordinary product imports keep their selected source/installed context.
_catalog_path = Path(__file__).with_name("benchmark_library_catalog.py")
_spec = importlib.util.spec_from_file_location("_carr_backup_catalog_benchmark", _catalog_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError("The adjacent public catalog benchmark is unavailable.")
catalog = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = catalog
_spec.loader.exec_module(catalog)
fixture = catalog.fixture

_ORIGINS = (
    "backup_budget", "library_backup", "pending_backup", "backup_sidecars",
    "backup_current", "backup_producers", "backup_transcription",
    "ingest.catalog_asset_copy", "ingest.restore", "storage.revisions",
    "search.paging",
)


def implementation_identity():
    result = catalog.implementation_identity()
    package = Path(importlib.import_module("carr_library").__file__).resolve().parent
    for name in _ORIGINS:
        module = importlib.import_module("carr_library." + name)
        path = Path(module.__file__).resolve()
        expected = package.joinpath(*name.split(".")).with_suffix(".py")
        catalog._require(path == expected, "A backup module escaped the selected package.")
        result[name] = dict(path=str(path), sha256=fixture.digest_file(path)[0])
    path = Path(__file__).resolve()
    result["backup_benchmark"] = dict(path=str(path), sha256=fixture.digest_file(path)[0])
    return result


def _require(value, message):
    catalog._require(value, message)


def _round_mib(value):
    return max(MIB, ((value + MIB - 1) // MIB) * MIB)


def _feed(digest, value):
    """Unambiguous bounded field encoding, including actual SQLite BLOB bytes."""
    if value is None:
        raw, kind = b"", b"n"
    elif type(value) is int:
        raw, kind = str(value).encode("ascii"), b"i"
    elif type(value) is float:
        _require(math.isfinite(value), "A measured catalog contains a nonfinite value.")
        raw, kind = value.hex().encode("ascii"), b"f"
    elif isinstance(value, str):
        raw, kind = value.encode("utf-8"), b"s"
    elif isinstance(value, bytes):
        raw, kind = value, b"b"
    else:
        raise TypeError("Unexpected measured catalog value.")
    _require(len(raw) <= MAX_ROW_BYTES, "A measured field exceeds its finite byte bound.")
    digest.update(kind + str(len(raw)).encode("ascii") + b":")
    digest.update(raw)


def _inventory(directory, deadline, maximum, *, identities=False):
    """Hash a finite actual tree, without reading any whole original into RAM."""
    directory = local_path(directory, exists=True)
    digest, count, files, logical = sha256(), 0, 0, 0
    categories = {name: dict(files=0, bytes=0) for name in (
        "database", "journal", "originals", "pending_records", "derived_search", "other")}
    maximum_name = 0

    def visit(parent, depth):
        nonlocal count, files, logical, maximum_name
        deadline.check()
        if depth > 32:
            raise ValueError("The measured private inventory exceeds its depth bound.")
        before = regular(parent, directory=True)
        relative = parent.relative_to(directory).as_posix()
        _feed(digest, "directory")
        _feed(digest, relative)
        if identities:
            for value in (before.st_dev, before.st_ino, before.st_mode,
                          getattr(before, "st_file_attributes", 0)):
                _feed(digest, value)
        names = []
        with os.scandir(parent) as entries:
            for entry in entries:
                deadline.check()
                count += 1
                if count > maximum or len(entry.name) > 255:
                    raise ValueError("The measured private inventory exceeds its finite entry allowance.")
                names.append(entry.name)
        for name in sorted(names):
            deadline.check()
            path = parent / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
                visit(path, depth + 1)
                continue
            name = path.relative_to(directory).as_posix()
            if len(name) > 1024:
                raise ValueError("The measured private inventory path is too long.")
            maximum_name = max(maximum_name, len(str(path)))
            actual, size = fixture.digest_file(path, cancelled=deadline.stopped)
            current = regular(path)
            _require((info.st_dev, info.st_ino, info.st_size, info.st_mode) ==
                     (current.st_dev, current.st_ino, current.st_size, current.st_mode),
                     "A measured private entry changed around its actual hash.")
            for value in ("file", name, size, actual):
                _feed(digest, value)
            if identities:
                for value in (current.st_dev, current.st_ino, current.st_mode,
                              current.st_nlink, getattr(current, "st_file_attributes", 0)):
                    _feed(digest, value)
            files += 1
            logical += size
            first = name.split("/", 1)[0]
            if first in ("library.sqlite", "library.sqlite-wal", "library.sqlite-shm", "library.sqlite-journal"):
                category = "database"
            elif first in ("imports.sqlite", "imports.sqlite-wal", "imports.sqlite-shm", "imports.sqlite-journal"):
                category = "journal"
            elif first == "originals":
                category = "originals"
            elif first in ("import-requests", "import-batches"):
                category = "pending_records"
            elif first in ("search-cache", "semantic-cache"):
                category = "derived_search"
            else:
                category = "other"
            categories[category]["files"] += 1
            categories[category]["bytes"] += size
        after = regular(parent, directory=True)
        _require((before.st_dev, before.st_ino, before.st_mode,
                  getattr(before, "st_file_attributes", 0)) ==
                 (after.st_dev, after.st_ino, after.st_mode,
                  getattr(after, "st_file_attributes", 0)),
                 "The measured private directory was replaced.")

    visit(directory, 0)
    deadline.check()
    return dict(sha256=digest.hexdigest(), files=files, entries=count,
                logical_bytes=logical, maximum_absolute_path_characters=maximum_name,
                categories=categories, identity_bound=identities)


def _quoted(identifier):
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier or len(identifier) > 256:
        raise ValueError("Invalid actual SQLite inventory identifier.")
    return '"' + identifier.replace('"', '""') + '"'


def _catalog_digest(app, deadline):
    """Compare every persisted canonical table, including real FTS storage."""
    digest, tables = sha256(), {}
    with app.read(budget=deadline) as db:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ROW_BYTES)
        selected = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 257"))
        _require(len(selected) <= 256, "The actual canonical table inventory exceeds its bound.")
        for name, sql in selected:
            deadline.check()
            _feed(digest, name)
            _feed(digest, sql)
            columns = list(db.execute("PRAGMA table_info(" + _quoted(name) + ")"))
            _require(0 < len(columns) <= 128, "The actual canonical column inventory is invalid.")
            for column in columns:
                for value in column:
                    _feed(digest, value)
            primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            order = ",".join(_quoted(column) for column in primary) if primary else "rowid"
            rows = 0
            for row in db.execute("SELECT * FROM " + _quoted(name) + " ORDER BY " + order):
                deadline.check()
                _feed(digest, rows)
                for value in row:
                    _feed(digest, value)
                rows += 1
                if rows > 100_000_000:
                    raise ValueError("An actual catalog table exceeds the finite benchmark row bound.")
            _feed(digest, rows)
            tables[name] = rows
    return dict(sha256=digest.hexdigest(), tables=tables)


def _raw_catalog(app, directory, state, generation, editions, deadline):
    """Independent generated-manifest oracle for every retained source and raw row."""
    digest, books, segments, raw_bytes, characters = sha256(), 0, 0, 0, 0
    with app.read(state["workspace"], budget=deadline) as db:
        actual = db.execute("SELECT count(*) FROM catalog_edition WHERE workspace=?",
                            (state["workspace"],)).fetchone()[0]
        _require(actual == state["books"], "The restored catalog book count differs.")
        for source in fixture.source_records(directory, state["manifest_sha256"], cancelled=deadline.stopped):
            deadline.check()
            book = source["book"]
            edition = bytes(editions[book*16:book*16+16]).hex()
            row = db.execute(
                "SELECT e.asset,w.title,a.mime,a.size FROM catalog_edition e "
                "JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work "
                "JOIN catalog_asset a ON a.digest=e.asset WHERE e.workspace=? AND e.id=?",
                (state["workspace"], edition)).fetchone()
            _require(row is not None and tuple(row) == (
                source["source_sha256"], Path(source["file"]).stem, "text/plain", source["size_bytes"]),
                "The catalog source/edition binding changed during backup or restore.")
            observed = 0
            for observed, row in enumerate(db.execute(
                    "SELECT ordinal,raw,digest FROM active_page WHERE workspace=? AND edition=? ORDER BY ordinal",
                    (state["workspace"], edition)), 1):
                deadline.check()
                _require(observed <= len(source["segments"]), "An extra restored raw segment appeared.")
                oracle = source["segments"][observed-1]
                encoded = row["raw"].encode("utf-8")
                _require(row["ordinal"] == oracle["ordinal"]
                    and sha256(encoded).hexdigest() == row["digest"] == oracle["sha256"]
                    and len(encoded) == oracle["size_bytes"] and len(row["raw"]) == oracle["characters"]
                    and oracle["anchor"] in row["raw"],
                    "Restored Unicode, CRLF, formfeed order or raw source digest differs.")
                for value in (book, edition, observed, row["digest"], len(encoded), len(row["raw"])):
                    _feed(digest, value)
                segments += 1
                raw_bytes += len(encoded)
                characters += len(row["raw"])
            _require(observed == len(source["segments"]), "The final original segment is missing.")
            retained = app.assets.resolve(source["source_sha256"], max_bytes=source["size_bytes"],
                                          cancelled=deadline.stopped)
            _require(fixture.digest_file(retained, cancelled=deadline.stopped) ==
                     (source["source_sha256"], source["size_bytes"]), "A retained original changed.")
            books += 1
        actual = db.execute("SELECT count(*) FROM active_page WHERE workspace=?",
                            (state["workspace"],)).fetchone()[0]
        _require(books == state["books"] and actual == segments == state["segments"],
                 "The complete raw-source population differs.")
    return dict(source_books=books, source_segments=segments, raw_utf8_bytes=raw_bytes,
                raw_characters=characters, sha256=digest.hexdigest())


def _policy(app, state, measured, seconds, memory_bytes, deadline):
    """Finite allowances from the current live library, never from an archive."""
    with app.read(budget=deadline) as db:
        assets, asset_bytes, original = db.execute(
            "SELECT count(*),coalesce(sum(size),0),coalesce(max(size),1) FROM catalog_asset").fetchone()
        database_bytes = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    journal = app.root / "imports.sqlite"
    regular(journal)
    with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as db:
        db.set_progress_handler(lambda: int(deadline.stopped()), 1000)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            jobs, maximum_segments = db.execute("SELECT count(*),coalesce(max(count),1) FROM job").fetchone()
            checkpoints = db.execute("SELECT count(*) FROM checkpoint").fetchone()[0]
            journal_bytes = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
        finally:
            db.set_progress_handler(None, 0)
            db.rollback()
    fields = measured["categories"]
    original = max(original, state["import_budget"]["source_bytes"])
    database_bytes = _round_mib(max(database_bytes, fields["database"]["bytes"]) + MIB)
    journal_bytes = _round_mib(max(journal_bytes, fields["journal"]["bytes"]) + MIB)
    retained_bytes = fields["originals"]["bytes"]
    pending_files = fields["pending_records"]["files"] + fields["originals"]["files"] + 16
    # Every request and batch binding has one path from this authored fixture.
    # This is a conservative known-record estimate; the unchanged core enforces
    # its exact manifest and metadata bounds while constructing the real archive.
    path_bound = max(measured["maximum_absolute_path_characters"], len(str(app.root)) + 96)
    manifest_bytes = _round_mib(max(8*MIB,
        256 * (assets + pending_files) + (2*state["books"] + 64) * (path_bound + 768) + MIB))
    pending_bytes = _round_mib(journal_bytes + fields["pending_records"]["bytes"] + retained_bytes + MIB)
    # Restore keeps one independently verified pending copy while projecting
    # the current paths into its fresh root. Staged/final hard links share bytes.
    staging_bytes = _round_mib(database_bytes + retained_bytes + fields["other"]["bytes"]
                              + 2*pending_bytes + 2*manifest_bytes + 64*MIB)
    required_metadata_memory = max(512*MIB + 8*manifest_bytes,
        512*MIB + 8*(fields["pending_records"]["bytes"] + 2048*jobs)
        + 1024*(fields["pending_records"]["files"] + jobs))
    if manifest_bytes > 256*MIB or memory_bytes < required_metadata_memory:
        raise _PolicyCapacity(dict(manifest_estimate_bytes=manifest_bytes,
            maximum_manifest_bytes=256*MIB, selected_memory_bytes=memory_bytes,
            required_metadata_memory_bytes=required_metadata_memory))
    policy = BackupBudget(database_bytes=database_bytes, original_bytes=original,
        assets=max(1, assets), asset_bytes=max(1, asset_bytes), journal_bytes=journal_bytes,
        pending_files=max(1, pending_files), pending_bytes=pending_bytes,
        jobs=max(1, jobs), checkpoints=max(1, checkpoints), segments=max(1, maximum_segments),
        manifest_bytes=manifest_bytes, seconds=seconds, memory_bytes=memory_bytes,
        staging_bytes=staging_bytes)
    archive_bound = database_bytes + retained_bytes + fields["other"]["bytes"] + pending_bytes + manifest_bytes
    return policy, dict(archive_upper_bytes=archive_bound,
        restore_or_verify_staging_upper_bytes=staging_bytes,
        measured_catalog_assets=assets, measured_journal_jobs=jobs,
        measured_journal_checkpoints=checkpoints,
        note="Conservative caller-selected accounting, not an enforced OS disk or memory quota.")


def _admission(directory, existing, policy, estimate, reserve_bytes):
    temporary = local_path(Path(gettempdir()), exists=True)
    regular(temporary, directory=True)
    usage, temp_usage = shutil.disk_usage(directory), shutil.disk_usage(temporary)
    fixture_info = regular(directory, directory=True)
    temporary_info = regular(temporary, directory=True)
    same_volume = fixture_info.st_dev == temporary_info.st_dev
    additional = estimate["archive_upper_bytes"] + policy.staging_bytes + 64*MIB + reserve_bytes
    temporary_required = policy.staging_bytes + 64*MIB + reserve_bytes
    memory = fixture.available_memory()
    required_memory = policy.memory_bytes + 64*MIB
    reasons = []
    if usage.free < additional:
        reasons.append("insufficient current fixture-volume free space")
    if not same_volume and temp_usage.free < temporary_required:
        reasons.append("insufficient current verifier temporary-volume free space")
    if memory["available_bytes"] is None or memory["available_bytes"] < required_memory:
        reasons.append("insufficient measured current physical/cgroup memory")
    return dict(status="READY_FOR_BACKUP" if not reasons else "NOT_RUN", reasons=reasons,
        existing_fixture=existing, additional_peak_estimate_bytes=additional,
        combined_existing_and_additional_estimate_bytes=existing["logical_bytes"]+additional,
        disk=dict(path=str(directory), total_bytes=usage.total, used_bytes=usage.used,
            free_bytes=usage.free, device=fixture_info.st_dev, inode=fixture_info.st_ino,
            required_additional_bytes=additional),
        verifier_temporary=dict(path=str(temporary), same_volume=same_volume,
            total_bytes=temp_usage.total, used_bytes=temp_usage.used, free_bytes=temp_usage.free,
            device=temporary_info.st_dev, inode=temporary_info.st_ino, required_bytes=temporary_required),
        memory={**memory, "required_headroom_bytes": required_memory},
        reserve_bytes=reserve_bytes, estimate=estimate)


def _passive_restore(source, restored, state, deadline):
    """Use the existing validated batch views; only documented rebasing differs."""
    identity, previous, page_number, rows = state["first_batch"], None, 0, 0
    while True:
        deadline.check()
        old = source.import_batch(state["workspace"], identity)
        new = restored.import_batch(state["workspace"], identity)
        _require(old["version"] == new["version"] == 3 and new["data_dir"] == str(restored.root)
            and new["scan"]["review_required"] is True
            and new["scan"]["page"] == page_number and new["scan"]["previous"] == previous
            and new["scan"]["next"] == old["scan"]["next"]
            and new["import_budget"] == old["import_budget"] == state["import_budget"]
            and len(new["rows"]) == len(old["rows"]),
            "Restored saved scan identity or explicit review revocation differs.")
        for prior, current in zip(old["rows"], new["rows"]):
            deadline.check()
            for key in ("row_id", "request_id", "work_id", "edition_id", "manifest",
                        "result", "state", "message", "file", "title", "duplicate_of"):
                _require(current[key] == prior[key], "A restored completed source was rebound.")
            _require(current["state"] == "complete", "A restored row was automatically resumed.")
            manifest = current["manifest"]
            expected = restored.root / "originals" / (manifest["sha256"] + ".txt")
            _require(Path(current["source"]) == expected,
                     "The restored source does not reference its retained original.")
            request = read_json(restored.root / "import-requests" / (current["request_id"] + ".json"),
                                maximum=256*1024)
            _require(request["data_dir"] == str(restored.root) and Path(request["source"]) == expected
                and all(key not in request for key in ("attempt", "publication_candidate", "publication_watermark")),
                "A restored request retained old execution authority.")
            rows += 1
        if old["scan"]["cursor"]["complete"]:
            break
        _require(old["scan"]["next"] is not None and page_number < state["books"] + 1000,
                 "Restored scan traversal does not make bounded progress.")
        previous, identity, page_number = identity, old["scan"]["next"], page_number + 1
    _require(rows == state["books"], "A restored saved source page was omitted.")
    return dict(completed_rows=rows, scan_pages=page_number+1,
                fresh_scan_review_required=True, old_attempts_removed=True, automatic_resume=False)


def _last_copy(app, state, generation, editions, deadline):
    """Reach the last original logical segment through the actual bounded reader."""
    from carr_library.search.paging import SearchResources
    book = state["books"] - 1
    count = (generation["spec"]["segments_per_book"]
             if generation["spec"]["kind"] == "corpus" else generation["spec"]["segments"])
    query = (fixture.anchor(book, count) + " commonrailway archive.\r\n"
             "Київ — Žilina; cafe\u0301; літо")
    resources = SearchResources(timeout=max(.1, min(86400, deadline.remaining())),
        memory_bytes=state["search_memory_bytes"], disk_bytes=state["search_disk_bytes"])
    cursor, offset, windows, matched = None, 0, 0, 0
    while True:
        deadline.check()
        page = app.search_page(state["workspace"], query, mode="phrase", title=f"book-{book:05d}",
            collection=state["collection"], limit=100, resources=resources,
            cursor=cursor, offset=offset, cancelled=deadline.stopped)
        if offset == 0:
            windows += 1
        _require(windows <= state["segments"] + 1, "Restored source navigation repeated without progress.")
        for hit in page.results:
            matched += 1
            _require(matched == 1 and hit.quote == query, "The final exact Unicode quotation changed.")
            catalog._checked_hit(app, state, generation, editions, hit, copy=True,
                                 cancelled=deadline.stopped)
        if page.has_more_results:
            _require(bool(page.results), "A restored result cursor made no progress.")
            cursor, offset = page.cursor, page.offset + len(page.results)
        elif page.next_source_cursor is not None:
            cursor, offset = page.next_source_cursor, 0
        else:
            break
    _require(matched == 1 and app.close_search(), "The last source copy or current cursor cleanup failed.")
    return dict(book=book, last_segment=count, source_windows=windows,
                quote_sha256=sha256(query.encode("utf-8")).hexdigest(),
                actual_verified_copy=True, foreign_scope_refused=True)


def _report(directory, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > MIB:
        raise ValueError("The final backup benchmark report exceeds its finite bound.")
    atomic_json(directory / "benchmark" / REPORT, value)


def backup_restore(directory, *, seconds=3600.0, memory_bytes=2*GIB,
                   reserve_bytes=GIB, cancelled=lambda: False):
    """Use the current common4 path with explicit external resources and a fresh root."""
    deadline = catalog.Deadline(seconds, cancelled)
    stopped = deadline.stopped
    if (type(memory_bytes) is not int or not 0 < memory_bytes <= 2**63-1
            or type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 2**63-1):
        raise ValueError("Choose finite explicit memory and free-space allowances.")
    started = time.perf_counter()
    with catalog._locked(directory) as directory:
        state, generation = catalog._state(directory)
        _require(state["import_complete"] and state["scan_complete"],
                 "Complete the actual public import and source traversal before backup measurement.")
        archive, restored_path = directory / "benchmark" / _ARCHIVE, directory / "benchmark" / _RESTORED
        for selected in (archive, restored_path, directory / "benchmark" / REPORT):
            if os.path.lexists(selected):
                raise FileExistsError("The backup benchmark never adopts or overwrites an existing output: " + str(selected))
        maximum = min(2_000_000, 20*state["books"] + 10_000)
        implementation = implementation_identity()
        control_paths = (directory / "owner.json", directory / "generation.json",
                         directory / "sources.jsonl", directory / "benchmark" / "state.json")
        controls = {path.relative_to(directory).as_posix():
                    fixture.digest_file(path, cancelled=deadline.stopped) for path in control_paths}
        external_before = fixture.verify_originals(directory, generation, cancelled=deadline.stopped)
        measured_existing = fixture.tree_bytes(directory, maximum_entries=maximum,
                                                cancelled=deadline.stopped)
        source, restored = LibraryApp(directory / "library"), None
        times = {}
        try:
            source_before = _inventory(source.root, deadline, maximum, identities=True)
            try:
                policy, estimate = _policy(source, state, source_before, seconds, memory_bytes, deadline)
            except _PolicyCapacity as error:
                result = dict(schema=SCHEMA, status="NOT_RUN", stage="backup policy admission",
                    source_books=state["books"], source_segments=state["segments"],
                    selected_seconds=seconds, selected_memory_bytes=memory_bytes,
                    reserve_bytes=reserve_bytes, admission=error.details,
                    archive_created=False, restore_created=False)
                _report(directory, result)
                return result
            selected_policy = policy.to_dict()
            admission = _admission(directory, measured_existing, policy, estimate, reserve_bytes)
            if admission["status"] == "NOT_RUN":
                result = dict(schema=SCHEMA, status="NOT_RUN", stage="backup resource admission",
                    source_books=state["books"], source_segments=state["segments"],
                    policy=selected_policy, admission=admission, archive_created=False, restore_created=False)
                _report(directory, result)
                return result
            # Every following operation uses the same already selected typed
            # allowance and the same outer deadline/cancellation callback.
            began = time.perf_counter()
            editions, scan_pages = catalog._edition_map(source, state, deadline)
            original_raw = _raw_catalog(source, directory, state, generation, editions, deadline)
            original_catalog = _catalog_digest(source, deadline)
            _require(source.close_search(), "The source library retains a live search owner.")
            times["source_oracle_seconds"] = time.perf_counter()-began
            began = time.perf_counter()
            checksum = source.backup(archive, include_generated=False,
                backup_budget=policy, cancelled=stopped)
            times["backup_seconds"] = time.perf_counter()-began
            deadline.check()
            _require(type(checksum) is str and len(checksum) == 64
                and fixture.digest_file(archive / "manifest.json", cancelled=deadline.stopped)[0] == checksum,
                "The actual backup receipt differs from its published manifest.")
            archive_before = _inventory(archive, deadline, maximum)
            began = time.perf_counter()
            verified = library_backup.verify_backup(archive, checksum,
                backup_budget=policy, cancelled=stopped)
            times["verify_seconds"] = time.perf_counter()-began
            _require(verified["version"] == 4 and verified["kind"] == "carr-library",
                     "The real verifier did not accept the existing common4 format.")
            # The verifier's manifest is evidence only. Its values never enter
            # policy construction, which completed before source.backup above.
            del verified
            deadline.check()
            began = time.perf_counter()
            actual_root = library_backup.restore_backup(archive, restored_path, checksum,
                backup_budget=policy, cancelled=stopped)
            times["restore_seconds"] = time.perf_counter()-began
            _require(actual_root == restored_path and restored_path != source.root
                and not (restored_path / ".restore-incomplete").exists(),
                "The actual restore did not finish in its fresh selected root.")
            deadline.check()
            began = time.perf_counter()
            restored = LibraryApp(restored_path)
            restored_raw = _raw_catalog(restored, directory, state, generation, editions, deadline)
            restored_catalog = _catalog_digest(restored, deadline)
            _require(restored_raw == original_raw, "The entire restored raw-source oracle differs.")
            _require(restored_catalog == original_catalog, "The full canonical catalog or FTS bytes differ logically.")
            passive = _passive_restore(source, restored, state, deadline)
            quotation = _last_copy(restored, state, generation, editions, deadline)
            _require(restored.close_search(), "The restored library retains a live search owner.")
            times["restored_oracle_and_copy_seconds"] = time.perf_counter()-began
            began = time.perf_counter()
            _require(_catalog_digest(source, deadline) == original_catalog,
                     "The source canonical catalog changed during backup or restore.")
            _require(catalog._edition_map(source, state, deadline)[0] == editions,
                     "The source completed-row identities changed.")
            _require(source.close_search(), "The source owner did not close before final comparison.")
            source_after = _inventory(source.root, deadline, maximum, identities=True)
            _require(source_after == source_before, "The source library inventory changed during the measured roundtrip.")
            archive_after = _inventory(archive, deadline, maximum)
            _require(archive_after == archive_before, "Verification or restore changed the selected archive.")
            external_after = fixture.verify_originals(directory, generation, cancelled=deadline.stopped)
            _require(external_after == external_before, "The authored external original inventory changed.")
            _require({path.relative_to(directory).as_posix():
                fixture.digest_file(path, cancelled=deadline.stopped) for path in control_paths} == controls,
                "The benchmark source manifest, generation or prior import evidence changed.")
            after_implementation = implementation_identity()
            _require(catalog._implementation_bytes(after_implementation) ==
                     catalog._implementation_bytes(implementation),
                     "The selected implementation changed during the actual roundtrip.")
            _require(policy.to_dict() == selected_policy, "The selected external resource policy changed.")
            deadline.check()
            times["final_independent_comparison_seconds"] = time.perf_counter()-began
            result = dict(schema=SCHEMA, status="BACKUP_RESTORE_VERIFIED", pid=os.getpid(),
                source_books=state["books"], source_segments=state["segments"],
                durable_scan_pages=scan_pages, policy=selected_policy, admission=admission,
                archive_path=str(archive), restored_root=str(restored_path), manifest_sha256=checksum,
                source_inventory_equal=True, archive_inventory_equal=True,
                catalog_equal=True, raw_equal=True, external_originals_equal=True,
                implementation_unchanged=True, passive_restore=passive,
                source_oracle=original_raw, restored_oracle=restored_raw,
                canonical_catalog=original_catalog, source_inventory=source_before,
                archive_inventory=archive_before, originals=external_after,
                last_verified_source_copy=quotation,
                measured_restored=fixture.tree_bytes(restored_path, maximum_entries=maximum,
                                                      cancelled=deadline.stopped),
                measured_final_fixture=fixture.tree_bytes(directory, maximum_entries=maximum,
                                                           cancelled=deadline.stopped),
                timings=times, elapsed_seconds=time.perf_counter()-started,
                memory=fixture.process_memory(), implementation=after_implementation,
                automatic_resume=False, policy_authority="explicit caller, selected before archive creation",
                measurement_scope="actual current process plus real common4 operations; no concurrent family peak claim",
                final_disk_measurement_boundary="after restore and all oracles, before writing this final report",
                compatibility_scope="same installed producer and reader; earlier-version fixtures are a separate gate")
            deadline.check()
            _report(directory, result)
            return result
        finally:
            for app in (restored, source):
                if app is not None and not app.close_search():
                    raise RuntimeError("The backup benchmark retains an unconfirmed search cleanup owner.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup",))
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--seconds", type=float, default=3600)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--reserve-mib", type=int, default=1024)
    args = parser.parse_args(argv)
    result = backup_restore(args.fixture, seconds=args.seconds,
        memory_bytes=args.memory_mib*MIB, reserve_bytes=args.reserve_mib*MIB)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 3 if result.get("status") == "NOT_RUN" else 0


if __name__ == "__main__":
    raise SystemExit(main())
