"""Actual generated TXT pilot controls; no SQL seeding or production mocks."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_library_growth.py"
SPEC = importlib.util.spec_from_file_location("_carr_real_library_growth_pilot", SCRIPT)
pilot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pilot
SPEC.loader.exec_module(pilot)
TESTED = "a" * 40


def proof_for_current_package(path):
    import carr_library
    root = Path(carr_library.__file__).resolve().parent
    hashes = {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
              for p in sorted(root.rglob("*.py"))}
    proof = dict(status="PASS", source_commit=TESTED, installed_root=str(root),
                 installed_python_files=len(hashes), source_hashes=hashes)
    pilot.write_record(path, proof)
    return path


@pytest.fixture(scope="module")
def real_case(tmp_path_factory):
    directory = tmp_path_factory.mktemp("actual-library-pilot")
    proof = proof_for_current_package(directory / "source-proof.json")
    result = pilot.run_case(directory / "case", 3, proof, TESTED, segments=40, seconds=90)
    assert result["status"] == "PASS", result.get("error")
    return directory, proof, result


def test_real_import_segments_paging_open_and_retained_retry_are_measured(real_case):
    directory, proof, result = real_case
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    app = LibraryApp(directory / "case" / "library")
    scope = result["workspace"]
    expected = {"catalog_books": 3, "active_editions": 3,
                "source_segments": 120, "indexed_segments": 120}
    assert pilot.corpus_counts(app, scope) == expected
    with app.read(scope) as db:
        rows = db.execute("SELECT p.edition,p.ordinal,p.raw,p.digest,r.asset "
                          "FROM active_page p JOIN revision r ON r.id=p.revision "
                          "WHERE p.workspace=? ORDER BY p.edition,p.ordinal", (scope,)).fetchall()
    assert len(rows) == 120
    for book in range(3):
        records = [row for row in rows if row["edition"] == pilot.book_id(book)]
        assert [row["ordinal"] for row in records] == list(range(1, 41))
        assert [row["raw"] for row in records] == pilot.corpus_segments(book, 40)
        original = pilot.corpus_text(book, 40).encode("utf-8")
        assert app.assets.resolve(records[0]["asset"]).read_bytes() == original
        assert records[0]["asset"] == sha256(original).hexdigest()
        page = ReadingDesk(app, scope, pilot.book_id(book)).page(40)
        assert page.verified and page.raw_text == pilot.corpus_segments(book, 40)[39]
        assert all(row["digest"] == sha256(row["raw"].encode("utf-8")).hexdigest() for row in records)
    assert result["search_coverage"] == {
        "complete": True, "source_windows": 2, "result_calls": 2,
        "unique_source_matches": 120, "expected_source_matches": 120}
    assert result["imported"]["count"] == 3 and result["service_open"]["count"] == 3
    assert result["scoped_search"]["count"] == 2
    assert result["imported"]["p95_seconds"] >= result["imported"]["p50_seconds"] > 0
    assert result["scoped_search"]["p95_seconds"] >= result["scoped_search"]["p50_seconds"] > 0
    assert result["cancellation_recovery"]["cancellation"]["count"] == 3
    assert result["cancellation_recovery"]["retained_retry"]["count"] == 3
    with closing(sqlite3.connect(directory / "case" / "cancel-library" / "imports.sqlite")) as journal:
        jobs = journal.execute("SELECT id,state,count FROM job ORDER BY edition").fetchall()
        counts = journal.execute("SELECT job,count(*) FROM checkpoint GROUP BY job ORDER BY job").fetchall()
    assert len(jobs) == 3 and all(row[1:] == ("complete", 40) for row in jobs)
    assert sorted(row[0] for row in jobs) == sorted(row["job"] for row in result["cancellation_recovery"]["evidence"])
    assert all(row[1] == 40 for row in counts) and len(counts) == 3
    assert not list((directory / "case").glob("cancel-input-*.txt"))
    assert all(row["cancelled_checkpoints"] == 1 and row["external_original_deleted"]
               and row["recovered_segments"] == 40
               for row in result["cancellation_recovery"]["evidence"])
    assert result["scope_controls"]["foreign_edition_open_refused"]
    assert result["scope_controls"]["negative_query_results"] == 0
    assert result["corpus_after_search"] == result["corpus_verification"]
    assert len(result["scoped_locator_query_plans"]["plans"]) == 2
    assert all(plan["rows"] for plan in result["scoped_locator_query_plans"]["plans"])
    pilot.validate_success(result, 3, pilot.source_identity(proof, TESTED))
    disk = pilot.disk_bytes(directory / "case")
    assert result["process_peak_rss_bytes"] > 0 and result["sampled_peak_logical_disk_bytes"] > 0
    assert disk >= sum(row["bytes"] for row in result["original_records"])
    assert result["sampled_peak_logical_disk_bytes"] >= sum(row["bytes"] for row in result["original_records"])
    assert "cold filesystem cache" in result["not_measured"]


@pytest.mark.parametrize("change", [
    "status", "completed_books", "requested_books", "source", "actual_units",
    "post_search", "search_complete", "search_count", "import_count", "cancel_count", "originals",
    "original_hash",
])
def test_incomplete_or_mismatched_child_receipt_never_qualifies(real_case, change):
    _, _, good = real_case
    result = deepcopy(good)
    if change == "status":
        result["status"] = "TIMEOUT"
    elif change == "completed_books":
        result["completed_books"] = 2
    elif change == "requested_books":
        result["requested_books"] = 1000
    elif change == "source":
        result["source"]["tested_commit"] = "b" * 40
    elif change == "actual_units":
        result["corpus_verification"]["source_segments"] = 119
    elif change == "post_search":
        result["corpus_after_search"]["all_retained_originals_checked"] = 2
    elif change == "search_complete":
        result["search_coverage"]["complete"] = False
    elif change == "search_count":
        result["search_coverage"]["unique_source_matches"] = 119
    elif change == "import_count":
        result["imported"]["count"] = 2
    elif change == "cancel_count":
        result["cancellation_recovery"]["cancellation"]["count"] = 0
    elif change == "originals":
        result["original_records"].pop()
    elif change == "original_hash":
        result["original_records"][0]["asset"] = "0" * 64
    with pytest.raises(ValueError):
        pilot.validate_success(result, 3, good["source"])


def test_real_retained_original_mutation_is_refused(tmp_path):
    from carr_library.application import LibraryApp
    app = LibraryApp(tmp_path / "library")
    scope = app.create_workspace("Changed original")
    raw = pilot.corpus_text(0, 2)
    result = app.add_text(scope, "Original", raw, edition_id=pilot.book_id(0), work_id="work")
    records = [dict(edition=pilot.book_id(0), revision=result["id"], asset=result["asset"],
                    bytes=len(raw.encode("utf-8")), segments=2)]
    pilot.audit_corpus(app, scope, records, 2, lambda: None)
    path = app.assets.resolve(result["asset"])
    path.write_bytes(raw.replace("Pilotneedle", "Changedword", 1).encode("utf-8"))
    with pytest.raises((ValueError, OSError)):
        pilot.audit_corpus(app, scope, records, 2, lambda: None)
    assert path.exists()


def test_actual_deadline_retains_partial_data_without_full_scale_pass(tmp_path):
    proof = proof_for_current_package(tmp_path / "source-proof.json")
    result = pilot.run_case(tmp_path / "case", 100, proof, TESTED, seconds=.000001)
    assert result["status"] == "TIMEOUT"
    assert result["phase"] == "import"
    assert result["requested_books"] == 100 and result["completed_books"] == 0
    assert result["imported"]["count"] == 0
    assert result["imported"]["p50_seconds"] is None and result["imported"]["p95_seconds"] is None
    saved, _ = pilot.read_record(tmp_path / "case" / "case-report.json")
    assert saved == result
    assert (tmp_path / "case" / "library" / "library.sqlite").exists()
    with pytest.raises(ValueError):
        pilot.validate_success(result, 100, result["source"])


def test_changed_source_proof_refused_before_new_case_creation(tmp_path):
    proof = proof_for_current_package(tmp_path / "proof.json")
    value, _ = pilot.read_record(proof)
    value["source_hashes"]["application.py"] = "0" * 64
    altered = tmp_path / "altered.json"
    pilot.write_record(altered, value)
    with pytest.raises(ValueError, match="source changed"):
        pilot.run_case(tmp_path / "case", 1, altered, TESTED, segments=2)
    assert not (tmp_path / "case").exists()


def test_checkpoint_publication_is_exclusive_and_empty_metrics_are_not_zero(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    pilot.write_record(checkpoint, {"completed_books": 100, "status": "IN_PROGRESS"})
    before = checkpoint.read_bytes()
    with pytest.raises(FileExistsError):
        pilot.write_record(checkpoint, {"completed_books": 1000, "status": "PASS"})
    assert checkpoint.read_bytes() == before
    assert pilot.samples([])["p95_seconds"] is None
    assert pilot.samples([.4, .1, .3, .2])["p50_seconds"] == .2
    assert pilot.samples([.4, .1, .3, .2])["p95_seconds"] == .4
    with pytest.raises(ValueError):
        pilot.samples([0.0])


@pytest.mark.parametrize("books,segments,seconds", [
    (0, 40, 900), (1001, 40, 900), (True, 40, 900),
    (1, 1, 900), (1, 41, 900), (1, 40, 0), (1, 40, 901),
])
def test_invalid_work_bounds_refuse_before_any_case_files(tmp_path, books, segments, seconds):
    with pytest.raises(ValueError):
        pilot.run_case(tmp_path / "case", books, tmp_path / "absent-proof", TESTED,
                       segments=segments, seconds=seconds)
    assert not (tmp_path / "case").exists()


def test_actual_owned_measurement_child_is_reaped_at_hard_bound(tmp_path):
    # The one-off workflow has already installed the exact package. Use -I here
    # as well so this subprocess cannot fall back to pytest's source PYTHONPATH.
    proof = tmp_path / "installed-proof.json"
    driver = r'''
import importlib.util, json, pathlib, sys, hashlib
import carr_library
script, proof, case = map(pathlib.Path, sys.argv[1:])
root = pathlib.Path(carr_library.__file__).resolve().parent
assert root.is_relative_to(pathlib.Path(sys.prefix).resolve())
hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in root.rglob("*.py")}
proof.write_text(json.dumps(dict(status="PASS", source_commit="a"*40, installed_root=str(root),
                                installed_python_files=len(hashes), source_hashes=hashes)),
                 encoding="utf-8")
spec = importlib.util.spec_from_file_location("_actual_pilot_child_owner", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
result = module.run_child(case, 1000, proof, "a"*40, seconds=.1)
assert result["status"] != "PASS"
assert result["hard_timeout"] and result["process_reaped"]
assert result["returncode"] is not None and result["cleanup_error"] is None
print(json.dumps(result))
'''
    completed = subprocess.run([sys.executable, "-I", "-B", "-c", driver, str(SCRIPT),
                                str(proof), str(tmp_path / "case")],
                               capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["requested_books"] == 1000 and result["hard_timeout_seconds"] == .1
    assert result["process_reaped"] and result["status"] == "TIMEOUT"
    assert (tmp_path / "child-1000-outcome.json").is_file()
    assert (tmp_path / "child-1000.log").is_file()
