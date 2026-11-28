"""Actual small-corpus contracts for the public scale measurement adapter.

The small authored corpus validates measurement and restart correctness. These
tests do not qualify 1k/10k/50k books or any multi-gigabyte input.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.models import AudioBudget
from carr_library.audio.process import run_process
from carr_library.durable_state import read_json

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_library_catalog.py"


@pytest.fixture(scope="module")
def adapter():
    spec = importlib.util.spec_from_file_location("_actual_catalog_benchmark", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def generated(adapter, tmp_path, *, books=3, segments=3, segment_bytes=256):
    directory = tmp_path / "actual-corpus"
    result = adapter.fixture.generate_fixture(directory,
        adapter.fixture.CorpusSpec(books=books, segments_per_book=segments,
                                   segment_bytes=segment_bytes),
        search_spool_budget_bytes=4*adapter.MIB, free_reserve_bytes=0,
        memory_headroom_bytes=128*adapter.MIB)
    assert result["status"] == "FIXTURE_GENERATED_NOT_BENCHMARKED", result
    return directory, result


def initialize(adapter, directory, *, steps=2):
    state = adapter.initialize(directory, page_files=2, slice_items=2,
        steps=steps, slice_seconds=60, memory_bytes=512*adapter.MIB,
        search_disk_bytes=32*adapter.MIB, reserve_bytes=0, foreign_libraries=2)
    assert state.get("schema") == adapter.SCHEMA and "workspace" in state, state
    return state


def invoke(directory, command, *options):
    executable = sys.executable
    environment = dict(os.environ)
    if (os.name == "nt" and sys.prefix != sys.base_prefix
            and getattr(sys, "_base_executable", None)
            and os.path.normcase(sys._base_executable) != os.path.normcase(executable)):
        environment.pop("PYTHONEXECUTABLE", None)
        environment["__PYVENV_LAUNCHER__"] = executable
        executable = sys._base_executable
    # Isolated ordinary installed import; the saved benchmark compares exact
    # product/script bytes and reports the actual selected paths, not source aliases.
    return run_process([str(Path(executable).resolve()), "-I", "-B", str(SCRIPT),
                        command, str(directory), *options],
        env=environment, owned_tree=True, allow_failure=True,
        budget=AudioBudget(timeout=180, max_log_bytes=8*1024**2,
                           max_output_bytes=32*1024**2, reserve_disk_bytes=0))


def test_real_public_pages_partial_source_raw_catalog_and_complete_scoped_search(adapter, tmp_path):
    directory, generation = generated(adapter, tmp_path)
    state = initialize(adapter, directory)
    app = LibraryApp(directory / "library")
    first = app.import_batch(state["workspace"], state["first_batch"])
    identities = [(r["row_id"], r["request_id"], r["edition_id"]) for r in first["rows"]]
    before = {p.name: sha256(p.read_bytes()).hexdigest()
              for p in (directory / "sources").iterdir()}
    partial = adapter.run_import(directory, seconds=180, stop_after_slices=1)
    assert partial["status"] == "PAUSED_BETWEEN_REAL_SLICES"
    saved = LibraryApp(app.root).import_batch(state["workspace"], state["first_batch"])
    assert saved["rows"][0]["state"] == "queued"
    assert saved["rows"][0]["result"]["state"] == "pending"
    assert saved["rows"][0]["result"]["completed"] == 2
    assert [(r["row_id"], r["request_id"], r["edition_id"]) for r in saved["rows"]] == identities
    complete = adapter.run_import(directory, seconds=180)
    assert complete["status"] == "IMPORT_COMPLETE"
    proof = adapter.verify_catalog(directory, seconds=180)
    assert proof["source_books"] == 3 and proof["source_segments"] == 9
    assert proof["durable_scan_pages"] == 2 and proof["implementation_unchanged"]
    assert proof["worker_peaks"]["measured_worker_exits"] >= 3
    assert proof["worker_peaks"]["maximum_worker_own_peak_bytes"] > 0
    assert proof["worker_peaks"]["simultaneous_family_peak_bytes"] is None
    assert proof["operations"]["measured_import_seconds"] > 0
    measured = adapter.search_catalog(directory, seconds=180, query_seconds=60, query_samples=2)
    assert measured["status"] == "MEASURED_SYNTHETIC_CORPUS"
    assert measured["source_books"] == 3 and measured["source_segments"] == 9
    assert measured["literal_page_latency"]["samples"] >= 1
    assert measured["bounded_title_anchor_latency"]["samples"] == 2
    assert measured["complete_scope_ranking"][0]["total_results"] == 9
    assert measured["complete_scope_ranking"][1]["updated_index_rows"] == 0
    assert len(measured["unicode_quotations"]) == 2
    assert measured["explicit_library_navigation"] == {"selected": 2, "pages": 1}
    assert measured["federated_cleanup_confirmed"] is True
    assert measured["semantic_model"] == "NOT_RUN"
    assert measured["simultaneous_family_peak"] == "NOT_MEASURED"
    assert measured["separate_process"] is False
    assert "same process" in measured["cold_definition"]
    assert before == {p.name: sha256(p.read_bytes()).hexdigest()
                      for p in (directory / "sources").iterdir()}
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == 3


def test_actual_postcommit_process_death_resumes_same_queue_without_duplicate_revision(adapter, tmp_path):
    directory, generation = generated(adapter, tmp_path, books=3, segments=1)
    state = initialize(adapter, directory)
    app = LibraryApp(directory / "library")
    first = app.import_batch(state["workspace"], state["first_batch"])
    identities = [(r["row_id"], r["request_id"], r["edition_id"]) for r in first["rows"]]
    crashed = invoke(directory, "import", "--seconds", "120", "--crash-after-first-commit")
    assert crashed.returncode == 73, (crashed.returncode, crashed.stdout, crashed.stderr)
    marker = read_json(directory / "benchmark" / "authored-crash.json", maximum=65536)
    assert marker["pid"] != os.getpid() and marker["committed"] == 1
    assert marker["boundary"] == "durable batch save after actual owned worker return"
    assert marker["workers"] and marker["workers"][0]["peak_bytes"] > 0
    saved = LibraryApp(app.root).import_batch(state["workspace"], state["first_batch"])
    assert saved["counts"]["complete"] == 1 and saved["counts"]["queued"] == 1
    assert [(r["row_id"], r["request_id"], r["edition_id"]) for r in saved["rows"]] == identities
    continued = invoke(directory, "import", "--seconds", "120")
    assert continued.returncode == 0, (continued.stdout, continued.stderr)
    verified = invoke(directory, "verify", "--seconds", "120")
    assert verified.returncode == 0, (verified.stdout, verified.stderr)
    report = read_json(directory / "benchmark" / "catalog-verification.json", maximum=65536)
    assert report["separate_process"] is True and report["pid"] != state["created_pid"]
    assert report["source_books"] == report["source_segments"] == 3
    assert report["operations"]["authored_postcommit_crash"]["pid"] == marker["pid"]
    assert report["implementation_unchanged"]
    with app.read(state["workspace"]) as db:
        assert db.execute("SELECT count(*) FROM revision WHERE workspace=?",
                          (state["workspace"],)).fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM active_page WHERE workspace=?",
                          (state["workspace"],)).fetchone()[0] == 3
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == 3


def test_real_resource_preflight_cannot_turn_impossible_admission_into_a_benchmark(adapter, tmp_path):
    directory, generation = generated(adapter, tmp_path, books=1, segments=1)
    result = adapter.initialize(directory, memory_bytes=2**62, reserve_bytes=0,
                                foreign_libraries=1)
    assert result["status"] == "NOT_RUN"
    assert result["admission"]["memory"]["required_headroom_bytes"] > 2**62
    assert not (directory / "library").exists() and not (directory / "benchmark").exists()
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == 1


def test_existing_profile_is_refused_before_any_catalog_write(adapter, tmp_path):
    directory, generation = generated(adapter, tmp_path, books=1, segments=1)
    library = directory / "library"
    library.mkdir()
    marker = library / "authored-existing-profile"
    marker.write_bytes(b"must remain untouched")
    with pytest.raises(FileExistsError):
        initialize(adapter, directory)
    assert marker.read_bytes() == b"must remain untouched"
    assert not (library / "library.sqlite").exists()
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == 1


def test_cancelled_measurement_never_emits_a_search_or_import_success(adapter, tmp_path):
    directory, _ = generated(adapter, tmp_path, books=1, segments=1)
    state = initialize(adapter, directory)
    with pytest.raises(InterruptedError):
        adapter.run_import(directory, seconds=120, cancelled=lambda: True)
    assert not (directory / "benchmark" / "search-measurements.json").exists()
    current = read_json(directory / "benchmark" / "state.json", maximum=65536)
    assert current["import_complete"] is False
    app = LibraryApp(directory / "library")
    assert app.browse(state["workspace"]) == ()


def test_external_original_mutation_fails_final_independent_verification(adapter, tmp_path):
    directory, _ = generated(adapter, tmp_path, books=1, segments=1)
    state = initialize(adapter, directory)
    assert adapter.run_import(directory, seconds=120)["status"] == "IMPORT_COMPLETE"
    original = directory / "sources" / "book-00000.txt"
    raw = original.read_bytes()
    original.write_bytes(raw[:-1] + b"X")
    with pytest.raises(ValueError, match="source hash or length"):
        adapter.verify_catalog(directory, seconds=120)
    assert not (directory / "benchmark" / "catalog-verification.json").exists()
    app = LibraryApp(directory / "library")
    with app.read(state["workspace"]) as db:
        asset, = db.execute("SELECT asset FROM catalog_edition WHERE workspace=?",
                            (state["workspace"],)).fetchone()
    assert app.assets.resolve(asset).read_bytes() == raw


def test_real_multi_mib_segment_uses_the_explicit_search_window_memory_allowance(adapter, tmp_path):
    directory, _ = generated(adapter, tmp_path, books=1, segments=1,
                             segment_bytes=3*adapter.MIB)
    state = adapter.initialize(directory, page_files=2, slice_items=2, steps=2,
        slice_seconds=60, memory_bytes=512*adapter.MIB,
        search_disk_bytes=128*adapter.MIB, reserve_bytes=0, foreign_libraries=1)
    assert state["search_memory_bytes"] == 97*adapter.MIB + 3*32
    assert adapter.run_import(directory, seconds=180)["status"] == "IMPORT_COMPLETE"
    assert adapter.verify_catalog(directory, seconds=180)["raw_utf8_bytes"] == 3*adapter.MIB
    result = adapter.search_catalog(directory, seconds=180, query_seconds=120, query_samples=2)
    assert result["status"] == "MEASURED_SYNTHETIC_CORPUS"
    assert result["source_books"] == result["source_segments"] == 1
    assert result["search_memory_allowance_bytes"] == state["search_memory_bytes"]
    assert result["complete_scope_ranking"][0]["total_results"] == 1
    assert result["complete_scope_ranking"][1]["updated_index_rows"] == 0
    assert result["unicode_quotations"][0]["last_segment"] == 1
    assert result["federated_cleanup_confirmed"] is True


@pytest.mark.parametrize("kind", ["corpus", "large_text"])
def test_authored_bom_is_counted_in_the_original_record_allowance(adapter, tmp_path, kind):
    from carr_library.catalog.intake import file_snapshot, TextSegments, IntakeError

    directory = tmp_path / "actual-bom-profile"
    spec = (adapter.fixture.CorpusSpec(1, 1, 256) if kind == "corpus"
            else adapter.fixture.LargeTextSpec(259, 1))
    generation = adapter.fixture.generate_fixture(directory, spec,
        search_spool_budget_bytes=4*adapter.MIB, free_reserve_bytes=0,
        memory_headroom_bytes=128*adapter.MIB)
    assert generation["status"] == "FIXTURE_GENERATED_NOT_BENCHMARKED"
    source = directory / "sources" / "book-00000.txt"
    original = source.read_bytes()
    assert len(original) == 259 and original[:3] == b"\xef\xbb\xbf"
    budget = adapter._profile(generation, seconds=60, steps=2, memory_bytes=512*adapter.MIB)
    assert budget.source_bytes == len(original) and budget.segment_bytes == len(original)
    captured = file_snapshot(source.parent, source.name, max_bytes=budget.source_bytes)
    with pytest.raises(IntakeError, match="segment exceeds the selected allowance"):
        TextSegments(captured, max_segments=1, max_segment_bytes=budget.segment_bytes-1)
    segments = TextSegments(captured, max_segments=1, max_segment_bytes=budget.segment_bytes)
    assert len(segments) == 1 and segments[0].encode("utf-8") == original[3:]
    assert source.read_bytes() == original


def test_actual_last_unicode_quote_is_beyond_the_first_large_source_window(adapter, tmp_path):
    from carr_library.search.paging import SearchResources

    directory, generation = generated(adapter, tmp_path, books=1, segments=3,
                                      segment_bytes=3*adapter.MIB)
    state = adapter.initialize(directory, page_files=2, slice_items=2, steps=4,
        slice_seconds=60, memory_bytes=512*adapter.MIB,
        search_disk_bytes=128*adapter.MIB, reserve_bytes=0, foreign_libraries=1)
    assert adapter.run_import(directory, seconds=180)["status"] == "IMPORT_COMPLETE"
    assert adapter.verify_catalog(directory, seconds=180)["raw_utf8_bytes"] == 9*adapter.MIB
    original = directory / "sources" / "book-00000.txt"
    original_digest = adapter.fixture.digest_file(original)
    query = (adapter.fixture.anchor(0, 3) + " commonrailway archive.\r\n"
             "Київ — Žilina; cafe\u0301; літо")
    app = LibraryApp(directory / "library")
    try:
        first = app.search_page(state["workspace"], query, mode="phrase", title="book-00000",
            collection=state["collection"],
            resources=SearchResources(timeout=120, memory_bytes=state["search_memory_bytes"],
                                      disk_bytes=state["search_disk_bytes"]))
        assert first.results == ()
        assert first.next_source_cursor is not None and not first.has_more_results
        assert first.source_pages == 1
    finally:
        assert app.close_search()
    measured = adapter.search_catalog(directory, seconds=180, query_seconds=120, query_samples=2)
    assert measured["status"] == "MEASURED_SYNTHETIC_CORPUS"
    assert measured["source_books"] == 1 and measured["source_segments"] == 3
    assert measured["unicode_quotations"] == [{
        "book": 0, "last_segment": 3, "source_windows": 3,
        "quote_sha256": sha256(query.encode("utf-8")).hexdigest()}]
    assert measured["complete_scope_ranking"][0]["total_results"] == 3
    assert measured["complete_scope_ranking"][1]["updated_index_rows"] == 0
    assert measured["federated_cleanup_confirmed"] is True
    assert adapter.fixture.digest_file(original) == original_digest
    assert adapter.fixture.verify_originals(directory, generation)["verified_sources"] == 1
