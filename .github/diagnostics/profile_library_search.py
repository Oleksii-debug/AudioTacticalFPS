"""Profile the unchanged public large-text/scoped search scenario on real retained bytes.

Forwarding observers and cProfile only: no query, result, source, deadline, cache,
scope or database replacement. These instrumented timings diagnose cost; they
are not uninstrumented throughput or final corpus qualification.
"""
from contextlib import contextmanager
from hashlib import sha256
import argparse
import cProfile
import functools
import importlib.util
import json
import os
from pathlib import Path
import pstats
import sys
import time

MAX_OPERATIONS = 4096
MAX_PROFILE_ROWS = 100_000
MAX_REPORT_BYTES = 8 * 1024**2
FOCUSED = {
    "_search_read", "_search_results", "_search_ranked_page", "search_page",
    "_resolve_selection", "_retained_source", "_asset_source", "_asset_bytes",
    "_loaders", "_selected", "copy", "attest", "strict_text_attestor", "resolve",
    "snapshot", "file_snapshot", "text_segments", "_text_records", "checked_chunks",
    "digest_file", "_opened", "_check", "_check_schema", "_check_revision",
    "_check_locators", "verify_originals", "_checked_hit", "_segment",
    "literal_search", "search", "_sync", "_lexical", "_excerpt", "build", "read",
    "capture", "publish", "_copy_snapshot", "_checked_source",
}


def _benchmark():
    path = Path(__file__).resolve().parents[2] / "carr-library" / "scripts" / "benchmark_library_catalog.py"
    spec = importlib.util.spec_from_file_location("_carr_actual_search_cost_catalog", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The exact adjacent public measurement adapter is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def _write(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("The actual search-cost report exceeded its finite bound.")
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _product_counts(profile):
    result = {}
    entries = profile.getstats()
    if len(entries) > MAX_PROFILE_ROWS:
        raise ValueError("The diagnostic profile function inventory exceeded its bound.")
    for entry in entries:
        code = entry.code
        if isinstance(code, str):
            continue
        filename = code.co_filename.replace("\\", "/")
        if "/carr_library/" not in filename:
            continue
        key = (filename, code.co_firstlineno, code.co_name)
        result[key] = (entry.callcount, entry.inlinetime, entry.totaltime)
    return result


def _difference(before, after):
    rows = []
    for key, current in after.items():
        prior = before.get(key, (0, 0., 0.))
        calls = current[0] - prior[0]
        own, cumulative = current[1] - prior[1], current[2] - prior[2]
        if calls or cumulative > 0:
            rows.append(dict(file=key[0], line=key[1], function=key[2], calls=calls,
                             own_seconds=max(0., own), cumulative_seconds=max(0., cumulative)))
    rows.sort(key=lambda row: (-row["cumulative_seconds"], row["file"], row["line"]))
    return rows[:20]


def _summary(profile):
    statistics = pstats.Stats(profile)
    if len(statistics.stats) > MAX_PROFILE_ROWS:
        raise ValueError("The final diagnostic function inventory exceeded its bound.")
    rows = []
    for (filename, line, function), (primitive, calls, own, cumulative, _) in statistics.stats.items():
        rows.append(dict(file=filename, line=line, function=function, primitive_calls=primitive,
                         calls=calls, own_seconds=own, cumulative_seconds=cumulative))
    rows.sort(key=lambda row: (-row["cumulative_seconds"], row["file"], row["line"]))
    return dict(total_calls=statistics.total_calls, primitive_calls=statistics.prim_calls,
                total_function_own_seconds=statistics.total_tt,
                top_cumulative=rows[:80],
                top_product=[row for row in rows if "/carr_library/" in row["file"].replace("\\", "/")][:100],
                focused=[row for row in rows if row["function"] in FOCUSED][:150])


@contextmanager
def _observe(benchmark, state, profile, output):
    """Retain actual return values/exceptions; all nested spans are explicit."""
    from carr_library.application import LibraryApp
    from carr_library.search.federated import FederatedSearch

    rows, stack, installed = [], [], []
    primary = state["workspace"]
    builds = 0
    stream = (output / "public-calls.jsonl").open("xb")
    stream_bytes = 0

    def append(row):
        nonlocal stream_bytes
        raw = (json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        stream_bytes += len(raw)
        if len(raw) > 256 * 1024 or stream_bytes > MAX_REPORT_BYTES:
            raise ValueError("The actual forwarding-observer log exceeded its finite bound.")
        stream.write(raw)
        stream.flush()

    def observe(owner, name, classify):
        original = getattr(owner, name)

        @functools.wraps(original)
        def measured(*args, **kwargs):
            if len(rows) + len(stack) >= MAX_OPERATIONS:
                raise ValueError("Too many actual public calls for this diagnostic.")
            label, detail = classify(args, kwargs)
            row = dict(index=len(rows)+len(stack), operation=name, phase=label,
                       parent=stack[-1]["index"] if stack else None, **detail)
            stack.append(row)
            before = _product_counts(profile)
            started = time.perf_counter()
            try:
                value = original(*args, **kwargs)
                row["outcome"] = "returned"
                # Only public count metadata is observed; no result is altered.
                if name == "search_page":
                    row.update(results=len(value.results), source_pages=value.source_pages,
                               total_results=value.total_results,
                               has_more_results=value.has_more_results,
                               has_next_source=value.next_source_cursor is not None,
                               ranking_scope=value.ranking_scope,
                               indexed_rows=value.updated_index_rows)
                return value
            except BaseException as error:
                row["outcome"] = "raised"
                row["exception"] = type(error).__name__
                row["message"] = str(error)[:500]
                raise
            finally:
                row["wall_seconds"] = time.perf_counter()-started
                row["product_functions"] = _difference(before, _product_counts(profile))
                popped = stack.pop()
                if popped is not row:
                    raise RuntimeError("Diagnostic observer nesting changed.")
                rows.append(row)
                append(row)

        setattr(owner, name, measured)
        installed.append((owner, name, original, measured))

    def page_label(args, options):
        nonlocal builds
        workspace = args[1] if len(args) > 1 else options["workspace"]
        query = args[2] if len(args) > 2 else options["query"]
        detail = dict(workspace_kind="primary" if workspace == primary else "auxiliary",
                      offset=options.get("offset", 0),
                      continuation=options.get("cursor") is not None)
        if workspace != primary:
            return "federated_source_page", detail
        if options.get("mode") == "thematic" and options.get("ranking_scope") == "all":
            if options.get("cursor") is not None:
                return "bm25_result_page", detail
            builds += 1
            return ("bm25_first_scope" if builds == 1 else "bm25_same_source_reopen"), detail
        if "\n" in query:
            return "unicode_source_window", detail
        if query == "commonrailway" and options.get("title") is None:
            return "literal_source_window", detail
        return "scoped_anchor_query", detail

    def copy_label(args, options):
        workspace = args[1] if len(args) > 1 else options["workspace"]
        return ("source_copy" if workspace == primary else "auxiliary_or_foreign_copy",
                dict(workspace_kind="primary" if workspace == primary else "auxiliary",
                     after_phase=rows[-1]["phase"] if rows else None))

    try:
        observe(LibraryApp, "search_page", page_label)
        observe(LibraryApp, "copy", copy_label)
        observe(FederatedSearch, "search", lambda *_: ("federated_library_window", {}))
        observe(FederatedSearch, "copy", lambda *_: ("federated_source_copy", {}))
        observe(benchmark.fixture, "verify_originals", lambda *_: ("external_original_verification", {}))
        yield rows
    finally:
        for owner, name, original, wrapper in reversed(installed):
            if getattr(owner, name) is not wrapper:
                raise RuntimeError("A diagnostic observer lost its exact callable owner.")
            setattr(owner, name, original)
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()


def run(directory, output, *, seconds=3600, query_seconds=1800, query_samples=50):
    benchmark, benchmark_path = _benchmark()
    directory = benchmark._root(directory)
    state, generation = benchmark._state(directory)
    spec = generation["spec"]
    large = (spec["kind"] == "large_text" and
             generation["generated_bytes"] in (3*1024**2, 9*1024**2))
    corpus = (spec["kind"] == "corpus" and spec["books"] == 1000
              and spec["segments_per_book"] == 40)
    if not (large or corpus):
        raise ValueError("Choose the actual 3/9 MiB or 1000 x 40 diagnostic fixture.")
    if not state["import_complete"]:
        raise ValueError("The genuine saved source import must finish before retrieval diagnosis.")
    output = output.absolute()
    if os.path.lexists(output):
        raise FileExistsError("The diagnostic never adopts an existing output folder.")
    output.mkdir()
    source_paths = (Path(__file__).resolve(), benchmark_path)
    source_before = {str(path): benchmark.fixture.digest_file(path)[0] for path in source_paths}
    actual_before = benchmark.implementation_identity()
    profile = cProfile.Profile()
    started = time.perf_counter()
    result, failure, rows = None, None, []
    try:
        with _observe(benchmark, state, profile, output) as rows:
            profile.enable()
            result = benchmark.search_catalog(directory, seconds=seconds,
                query_seconds=query_seconds, query_samples=query_samples)
            profile.disable()
        return result
    except BaseException as error:
        failure = dict(type=type(error).__name__, message=str(error)[:1000])
        raise
    finally:
        profile.disable()
        source_after = {str(path): benchmark.fixture.digest_file(path)[0] for path in source_paths}
        implementation_after = benchmark.implementation_identity()
        unchanged = (source_before == source_after and
            benchmark._implementation_bytes(actual_before) ==
            benchmark._implementation_bytes(implementation_after))
        by_phase = {}
        for row in rows:
            item = by_phase.setdefault(row["phase"], dict(calls=0, returned=0, raised=0,
                                                          inclusive_wall_seconds=0.))
            item["calls"] += 1
            item[row["outcome"]] += 1
            item["inclusive_wall_seconds"] += row["wall_seconds"]
        report = dict(schema="carr-large-text-search-cost/1",
            status="DIAGNOSTIC_COMPLETE" if result is not None and failure is None and unchanged else "DIAGNOSTIC_INCOMPLETE",
            qualification="DIAGNOSTIC_ONLY_NOT_CORPUS_OR_THROUGHPUT_ACCEPTANCE",
            profiled=True, observer_overhead_included=True,
            nested_spans="Inclusive and overlapping; do not sum phases or cumulative function times.",
            actual_public_scenario="unchanged benchmark_library_catalog.search_catalog",
            source_bytes=generation["generated_bytes"], source_books=state["books"],
            source_segments=state["segments"], manifest_sha256=generation["manifest_sha256"],
            selected_seconds=seconds, selected_query_seconds=query_seconds,
            query_samples=query_samples, elapsed_seconds=time.perf_counter()-started,
            pid=os.getpid(), executable=sys.executable, exception=failure,
            phases=by_phase, public_call_records=len(rows), profile=_summary(profile),
            actual_source_bindings=source_after, implementation_unchanged=unchanged,
            actual_implementation=implementation_after,
            actual_result=result, family_memory="NOT_MEASURED",
            final_2gib_or_50k_capacity="NOT_RUN", physical_or_human_quality="NOT_ASSESSED")
        _write(output / "profile.json", report)
        compact = dict(status=report["status"], source_bytes=report["source_bytes"],
            source_segments=report["source_segments"], elapsed_seconds=report["elapsed_seconds"],
            phases=by_phase, exception=failure, implementation_unchanged=unchanged,
            top_product=report["profile"]["top_product"][:15],
            qualification=report["qualification"])
        print("CARR_LARGE_TEXT_SEARCH_COST " + json.dumps(compact, ensure_ascii=False, sort_keys=True), flush=True)
        if not unchanged:
            raise AssertionError("The exact selected diagnostic or product bytes changed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seconds", type=float, default=3600)
    parser.add_argument("--query-seconds", type=float, default=1800)
    parser.add_argument("--query-samples", type=int, default=50)
    args = parser.parse_args(argv)
    run(args.fixture, args.output, seconds=args.seconds,
        query_seconds=args.query_seconds, query_samples=args.query_samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
