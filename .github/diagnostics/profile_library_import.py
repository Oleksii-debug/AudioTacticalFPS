"""Bounded actual public-import cost diagnosis, never a corpus qualification.

Only observer calls and cProfile recording are added around the existing parent
and the existing owned worker. Import bytes, APIs, deadlines, publication checks,
resource accounting and returned worker results are unchanged. Profiled elapsed
times include instrumentation overhead and are not throughput acceptance.
"""
from contextlib import contextmanager
from hashlib import sha256
import argparse
import atexit
import cProfile
import functools
import importlib.util
import json
import os
from pathlib import Path
import pstats
import sys
import time

MAX_PROFILE_ROWS = 100_000
ANCHOR = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"


def _write(path, value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(payload.encode("utf-8")) > 2 * 1024**2:
        raise ValueError("The bounded diagnostic report is too large.")
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _profile_result(profile, started):
    profile.disable()
    statistics = pstats.Stats(profile)
    if len(statistics.stats) > MAX_PROFILE_ROWS:
        raise ValueError("The diagnostic function inventory exceeded its allowance.")
    rows = []
    for (filename, line, function), (primitive, calls, own, cumulative, _) in statistics.stats.items():
        rows.append(dict(file=filename, line=line, function=function, primitive_calls=primitive,
                         calls=calls, own_seconds=own, cumulative_seconds=cumulative))
    rows.sort(key=lambda row: (-row["cumulative_seconds"], row["file"], row["line"]))
    product = [row for row in rows if "carr_library" in row["file"]]
    focused = [row for row in rows if row["function"] in {
        "prepare_request", "record_progress", "load_request", "_check", "_check_schema",
        "_check_revision", "_check_locators", "validate_candidate", "stage_revision",
        "check", "validate", "_publication_state", "reconcile", "run_slice", "run_child",
        "_import_file", "retain", "retain_snapshot", "_replace_state", "atomic_json",
        "text_segments", "run_process", "_wait_posix_group"}]
    return dict(pid=os.getpid(), executable=sys.executable,
                wall_seconds=time.perf_counter() - started,
                total_calls=statistics.total_calls, primitive_calls=statistics.prim_calls,
                total_function_own_seconds=statistics.total_tt,
                top_cumulative=rows[:80], top_product=product[:100], focused=focused[:100],
                measurement="cProfile; includes observer overhead; not uninstrumented throughput")


def start_child(path):
    """Called inside the unchanged, actually owned isolated import interpreter."""
    profile = cProfile.Profile()
    started = time.perf_counter()

    def finish():
        _write(path, _profile_result(profile, started))

    atexit.register(finish)
    profile.enable()


def _benchmark():
    path = Path(__file__).resolve().parents[2] / "carr-library" / "scripts" / "benchmark_library_catalog.py"
    spec = importlib.util.spec_from_file_location("carr_actual_catalog_cost_benchmark", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the exact checked-in measurement adapter.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _observe_parent(directory):
    from carr_library.audio import process
    from carr_library.ingest import batches, slices, worker
    originals = (batches.ImportBatchStore.run, slices.run_slice, worker._invoke, process.run_process)
    spans, native_records = [], []
    script_path = Path(__file__).resolve()
    selected_sha256 = sha256(script_path.read_bytes()).hexdigest()

    def span(kind, original):
        @functools.wraps(original)
        def run(*args, **kwargs):
            if len(spans) >= 64:
                raise ValueError("The bounded diagnostic slice inventory exceeded its allowance.")
            record = dict(kind=kind, start=time.perf_counter())
            spans.append(record)
            try:
                return original(*args, **kwargs)
            finally:
                record["end"] = time.perf_counter()
                record["wall_seconds"] = record["end"] - record["start"]
        return run

    def native(args, **kwargs):
        if not isinstance(args, (tuple, list)) or "-c" not in args:
            return originals[3](args, **kwargs)
        offset = args.index("-c") + 1
        if offset >= len(args) or ANCHOR not in args[offset]:
            return originals[3](args, **kwargs)
        if args[offset].count(ANCHOR) != 1 or kwargs.get("owned_tree") is not True:
            raise AssertionError("The diagnostic must use the exact existing owned worker.")
        if len(native_records) >= 16:
            raise ValueError("The finite real-worker diagnostic allowance was exceeded.")
        if sha256(script_path.read_bytes()).hexdigest() != selected_sha256:
            raise ValueError("The exact observer source changed before child launch.")
        report = directory / ("child-%04d.json" % (len(native_records) + 1))
        command = list(args)
        prefix = ("_carr_cost=runpy.run_path(" + repr(str(script_path)) +
                  ",run_name='carr_actual_import_cost_child'); " +
                  "_carr_cost['start_child'](" + repr(str(report)) + "); ")
        command[offset] = command[offset].replace(ANCHOR, prefix + ANCHOR)
        record = dict(index=len(native_records) + 1, start=time.perf_counter(),
                      profile_file=report.name, observer_sha256=selected_sha256)
        native_records.append(record)
        try:
            result = originals[3](command, **kwargs)
            record.update(native_elapsed=result.elapsed, returncode=result.returncode,
                          stdout_sha256=sha256(result.stdout).hexdigest())
            if result.returncode == 0 and not report.is_file():
                raise AssertionError("The actual completed worker did not record its profile.")
            return result
        finally:
            record["end"] = time.perf_counter()
            record["wall_seconds"] = record["end"] - record["start"]

    batches.ImportBatchStore.run = span("public_batch", originals[0])
    slices.run_slice = span("parent_slice", originals[1])
    worker._invoke = span("current_native_invoke", originals[2])
    process.run_process = native
    try:
        yield spans, native_records
    finally:
        batches.ImportBatchStore.run, slices.run_slice, worker._invoke, process.run_process = originals


def _partitions(spans):
    result = []
    for index, batch in enumerate(item for item in spans if item["kind"] == "public_batch"):
        children = [item for item in spans if item["kind"] == "parent_slice"
                    and batch["start"] <= item["start"] <= item["end"] <= batch["end"]]
        invokes = [item for item in spans if item["kind"] == "current_native_invoke"
                   and batch["start"] <= item["start"] <= item["end"] <= batch["end"]]
        row = dict(index=index + 1, public_batch_seconds=batch["wall_seconds"])
        if len(children) == len(invokes) == 1:
            child, invoke = children[0], invokes[0]
            row.update(parent_prepare_seconds=child["start"] - batch["start"],
                       parent_slice_prelaunch_seconds=invoke["start"] - child["start"],
                       native_invoke_seconds=invoke["wall_seconds"],
                       parent_reconciliation_seconds=child["end"] - invoke["end"],
                       parent_final_save_seconds=batch["end"] - child["end"])
        else:
            row.update(parent_slice_calls=len(children), native_invoke_calls=len(invokes))
        result.append(row)
    return result


def profile_import(fixture, output):
    output.mkdir(parents=True, exist_ok=False)
    benchmark = _benchmark()
    profile = cProfile.Profile()
    started = time.perf_counter()
    result = None
    with _observe_parent(output) as (spans, workers):
        try:
            profile.enable()
            result = benchmark.run_import(fixture, seconds=1800)
            if result.get("status") != "IMPORT_COMPLETE":
                raise AssertionError("The representative actual public import did not complete.")
        finally:
            data = _profile_result(profile, started)
            data.update(status="DIAGNOSTIC_ONLY", result=result,
                        spans=spans, partitions=_partitions(spans), native_workers=workers,
                        corpus_tier_qualification="NOT_RUN")
            _write(output / "parent.json", data)
            print("CARR_IMPORT_COST_PARENT " + json.dumps({
                key: data[key] for key in ("status", "result", "wall_seconds", "partitions",
                                           "native_workers", "top_cumulative", "top_product", "focused")
            }, sort_keys=True), flush=True)
    for path in sorted(output.glob("child-*.json")):
        if path.stat().st_size > 2 * 1024**2:
            raise ValueError("The actual worker profile exceeded its record allowance.")
        data = json.loads(path.read_text(encoding="utf-8"))
        shown = dict(data, top_cumulative=data["top_cumulative"][:25],
                     top_product=data["top_product"][:35], focused=data["focused"][:40])
        print("CARR_IMPORT_COST_CHILD " + json.dumps(shown, sort_keys=True), flush=True)
    return result


def query_plans(fixture, output):
    """EXPLAIN only: no candidate SQL statement is executed or substituted."""
    from carr_library.application import LibraryApp
    benchmark = _benchmark()
    state, _ = benchmark._state(fixture)
    app = LibraryApp(fixture / "library")
    with app.read(state["workspace"]) as db:
        selected = db.execute(
            "SELECT id,workspace,edition FROM revision WHERE workspace=? ORDER BY id LIMIT 1",
            (state["workspace"],)).fetchone()
        if selected is None:
            raise AssertionError("The actual imported revision is missing.")
        revision, workspace, edition = tuple(selected)
        expected = "SELECT id,raw FROM page WHERE revision=?"
        actual = "SELECT i.id,i.raw FROM indexed i JOIN page p ON p.id=i.id WHERE p.revision=?"
        queries = [
            ("current_pages", "SELECT raw,digest FROM page WHERE revision=?", (revision,)),
            ("expected_minus_actual", expected + " EXCEPT " + actual, (revision, revision)),
            ("actual_minus_expected", actual + " EXCEPT " + expected, (revision, revision)),
            ("previous_indexed", "SELECT 1 FROM indexed i JOIN page p ON p.id=i.id "
             "JOIN revision r ON r.id=p.revision WHERE r.workspace=? AND r.edition=? "
             "AND r.id<>? LIMIT 1", (workspace, edition, revision)),
            ("retire_old_indexed", "DELETE FROM indexed WHERE id IN "
             "(SELECT id FROM active_page WHERE workspace=? AND edition=?)", (workspace, edition)),
            ("new_edition_retire", "DELETE FROM indexed WHERE id IN "
             "(SELECT id FROM active_page WHERE workspace=? AND edition=?)",
             (workspace, "not-an-existing-edition-diagnostic")),
            ("insert_indexed", "INSERT INTO indexed SELECT id,raw FROM page WHERE revision=?", (revision,)),
        ]
        plans = []
        for label, sql, bindings in queries:
            rows = [tuple(row) for row in db.execute("EXPLAIN QUERY PLAN " + sql, bindings)]
            if len(rows) > 128:
                raise AssertionError("The bounded query-plan record allowance was exceeded.")
            plans.append(dict(label=label, sql=sql, plan=rows))
        indexes = [tuple(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','table') "
            "AND tbl_name IN ('page','indexed','active','revision') ORDER BY type,name")]
        value = dict(status="DIAGNOSTIC_ONLY", plans=plans, schema=indexes,
                     pages=db.execute("SELECT count(*) FROM page").fetchone()[0],
                     revisions=db.execute("SELECT count(*) FROM revision").fetchone()[0])
    _write(output / "query-plans.json", value)
    print("CARR_IMPORT_QUERY_PLANS " + json.dumps(value, sort_keys=True), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    profile_import(args.fixture.resolve(), args.output.resolve())
    query_plans(args.fixture.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
