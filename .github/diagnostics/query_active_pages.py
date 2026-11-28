"""Finite read-only SQL and actual ranked-phase diagnosis; never release qualification."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from functools import wraps
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import time

ROOT = Path(__file__).resolve().parents[2] / "carr-library"
OUT = ROOT / "ci-library-sql-cost"
MAX_STEPS = 20_000_000
JOINED = (" FROM active a JOIN revision r ON r.id=a.revision "
          "AND r.workspace=a.workspace AND r.edition=a.edition "
          "JOIN page p ON p.revision=r.id ")
OLD_COLUMNS = "id,workspace,edition,revision,asset,ordinal,physical,printed,verified,raw,digest"
NEW_COLUMNS = "p.id,r.workspace,r.edition,p.revision,r.asset,p.ordinal,p.physical,p.printed,p.verified,p.raw,p.digest"

def write(name, value):
    OUT.mkdir(exist_ok=True)
    with (OUT / name).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")

def digest(path):
    value = sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            value.update(data)
    return value.hexdigest()

def identity():
    assert sys.flags.isolated and not os.environ.get("PYTHONPATH")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()
    assert head == os.environ["CARR_SQL_HEAD"]
    source = (ROOT / "src" / "carr_library").resolve()
    installed = (Path(sysconfig.get_path("purelib")) / "carr_library").resolve()
    assert source != installed and not installed.is_relative_to(source)
    def inventory(base):
        result = []
        for path in sorted(base.rglob("*.py")):
            info = path.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not path.is_symlink()
            assert path.resolve().is_relative_to(base) and info.st_size <= 32*1024**2
            assert not int(getattr(info, "st_file_attributes", 0)) & 0x400
            result.append([path.relative_to(base).as_posix(), digest(path)])
        assert len(result) >= 380
        return result
    rows = inventory(source)
    assert rows == inventory(installed), "Installed/source bytes differ."
    origins = {}
    for name in ("application", "search.paging", "search.ranked_scope", "storage.revisions",
                 "storage.publication", "catalog.intake", "ingest.session"):
        module = importlib.import_module("carr_library." + name)
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(installed) and not path.is_relative_to(source)
        origins[name] = {"path": str(path), "sha256": digest(path)}
    scripts = {name: digest(ROOT / "scripts" / name)
               for name in ("benchmark_library_scale.py", "benchmark_library_catalog.py")}
    scripts["query_active_pages.py"] = digest(Path(__file__))
    return {"commit": head, "run": os.environ["CARR_SQL_RUN"],
            "package_files": len(rows), "inventory": rows, "origins": origins,
            "scripts": scripts, "sqlite": sqlite3.sqlite_version, "python": sys.version}

def benchmark():
    path = ROOT / "scripts" / "benchmark_library_catalog.py"
    spec = importlib.util.spec_from_file_location("_carr_actual_sql_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def measured(db, sql, parameters):
    plan = [tuple(row) for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
    steps = 0
    started = time.perf_counter()
    deadline = started + 30
    def progress():
        nonlocal steps
        steps += 1
        return int(steps > MAX_STEPS or time.perf_counter() >= deadline)
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    changes = db.total_changes
    db.set_progress_handler(progress, 1)
    try:
        rows = [tuple(row) for row in db.execute(sql, parameters)]
    finally:
        db.set_progress_handler(None, 0)
    elapsed = time.perf_counter() - started
    assert db.total_changes == changes and len(rows) <= 101
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return rows, {"plan": plan, "vm_steps": steps, "elapsed_seconds": elapsed,
                  "rows": len(rows), "rows_sha256": sha256(encoded).hexdigest(),
                  "progress_interval": 1, "observer_overhead_included": True}

def pair(db, records, label, old, new, params):
    first, old_result = measured(db, old, params)
    second, new_result = measured(db, new, params)
    assert first == second, (label, "Actual result/order differs.")
    records.append({"label": label, "old_sql": old, "new_sql": new,
                    "old": old_result, "new": new_result, "exact_row_parity": True})
    return first

def sql_diagnosis(module, directory, state):
    app = module.LibraryApp(directory / "library")
    before = digest(app.database)
    records = []
    workspace = state["workspace"]
    with app.read(workspace) as db:
        editions = [row[0] for row in db.execute(
            "SELECT edition FROM active WHERE workspace=? ORDER BY edition LIMIT 500", (workspace,))]
        assert len(editions) == min(500, state["books"])
        placeholders = ",".join("?" for _ in editions)
        for label, position in (("first", None), ("middle", len(editions)//2),
                                ("last", len(editions)-1)):
            old = ("SELECT id,edition,revision,ordinal,length(raw),digest FROM active_page "
                   "WHERE workspace=? AND edition IN (" + placeholders + ")")
            new = ("SELECT p.id,r.edition,p.revision,p.ordinal,length(p.raw),p.digest" +
                   JOINED + "WHERE a.workspace=? AND a.edition IN (" + placeholders + ")")
            parameters = [workspace, *editions]
            if position is not None:
                ordinal = 20 if label == "middle" else 38
                prior = tuple(db.execute(
                    "SELECT p.id,p.revision FROM page p JOIN active a ON a.revision=p.revision "
                    "WHERE a.workspace=? AND a.edition=? AND p.ordinal=?",
                    (workspace, editions[position], ordinal)).fetchone())
                old += (" AND (edition COLLATE BINARY>? OR (edition=? AND "
                        "(ordinal>? OR (ordinal=? AND id>?))))")
                new += (" AND (a.edition COLLATE BINARY>? OR (a.edition=? AND "
                        "(p.ordinal>? OR (p.ordinal=? AND p.id>?))))")
                parameters += [editions[position], editions[position], ordinal, ordinal, prior[0]]
                pair(db, records, "cursor_" + label,
                     "SELECT edition,revision,ordinal,id FROM active_page WHERE workspace=? AND id=?",
                     "SELECT r.edition,p.revision,p.ordinal,p.id" + JOINED +
                     "WHERE a.workspace=? AND p.id=?", (workspace, prior[0]))
            old += " ORDER BY edition COLLATE BINARY,ordinal,id LIMIT ?"
            new += " ORDER BY a.edition COLLATE BINARY,p.ordinal,p.id LIMIT ?"
            parameters.append(101)
            rows = pair(db, records, "select_" + label, old, new, parameters)
            ids = [row[0] for row in rows[:100]]
            assert ids
            marks = ",".join("?" for _ in ids)
            pair(db, records, "refetch_" + label,
                 "SELECT " + OLD_COLUMNS + " FROM active_page WHERE workspace=? AND id IN (" +
                 marks + ") ORDER BY edition,ordinal,id",
                 "SELECT " + NEW_COLUMNS + JOINED + "WHERE a.workspace=? AND p.id IN (" +
                 marks + ") ORDER BY r.edition,p.ordinal,p.id", (workspace, *ids))
        for edition in (editions[0], editions[-1]):
            pair(db, records, "replacement_selection",
                 "SELECT id FROM active_page WHERE workspace=? AND edition=? ORDER BY id",
                 "SELECT p.id" + JOINED + "WHERE a.workspace=? AND a.edition=? ORDER BY p.id",
                 (workspace, edition))
        foreign = state["foreign_workspaces"][0]
        foreign_id = db.execute("SELECT p.id FROM page p JOIN active a ON a.revision=p.revision "
            "WHERE a.workspace=? ORDER BY p.id LIMIT 1", (foreign,)).fetchone()[0]
        wrong = pair(db, records, "foreign_workspace_id",
             "SELECT " + OLD_COLUMNS + " FROM active_page WHERE workspace=? AND id IN (?) ORDER BY edition,ordinal,id",
             "SELECT " + NEW_COLUMNS + JOINED +
             "WHERE a.workspace=? AND p.id IN (?) ORDER BY r.edition,p.ordinal,p.id",
             (workspace, foreign_id))
        assert wrong == []
    assert digest(app.database) == before, "Readonly canonical database bytes changed."
    return {"database_sha256_before_after": before, "records": records,
            "exact_row_parity": True, "product_sql_changed": False}

@contextmanager
def phases(records):
    from carr_library.search import ranked_scope
    originals = {}
    for name in ("_sync", "_lexical"):
        original = getattr(ranked_scope, name)
        originals[name] = original
        def wrap(function, label):
            @wraps(function)
            def invoke(*args, **kwargs):
                assert len(records) < 12
                index = args[0]
                assert isinstance(index, sqlite3.Connection)
                source = args[1] if label == "_sync" else None
                assert source is None or isinstance(source, sqlite3.Connection)
                before = index.total_changes
                started = time.perf_counter()
                completed = False
                result = None
                try:
                    result = function(*args, **kwargs)
                    completed = True
                    return result
                finally:
                    record = {"phase": label, "elapsed_seconds": time.perf_counter()-started,
                              "index_changes": index.total_changes-before, "completed": completed}
                    if completed and label == "_sync":
                        record["returned"] = result
                    records.append(record)
            return invoke
        setattr(ranked_scope, name, wrap(original, name))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(ranked_scope, name, original)

def rank_diagnosis(module, directory, state, generation):
    from carr_library.search.paging import SearchResources
    deadline = module.Deadline(300, lambda: False)
    app = module.LibraryApp(directory / "library")
    editions, _ = module._edition_map(app, state, deadline)
    resources = SearchResources(timeout=120, memory_bytes=state["search_memory_bytes"],
                                disk_bytes=state["search_disk_bytes"])
    records, result = [], []
    try:
        with phases(records):
            for label in ("first", "same_source_reopen"):
                start_record = len(records)
                started = time.perf_counter()
                page = app.search_page(state["workspace"], "commonrailway", mode="thematic",
                    ranking_scope="all", collection=state["collection"], limit=100,
                    resources=resources, cancelled=deadline.stopped)
                elapsed = time.perf_counter()-started
                assert (page.ranking_scope == "all" and page.next_source_cursor is None
                        and page.source_pages == page.total_results == state["segments"]
                        and page.source_editions == state["books"])
                for hit in page.results:
                    module._checked_hit(app, state, generation, editions, hit, cancelled=deadline.stopped)
                last = app.search_page(state["workspace"], "commonrailway", mode="thematic",
                    ranking_scope="all", collection=state["collection"], limit=100,
                    cursor=page.cursor, offset=max(0,page.total_results-100),
                    resources=resources, cancelled=deadline.stopped)
                assert last.results and not last.has_more_results
                module._checked_hit(app, state, generation, editions, last.results[-1],
                                    copy=True, cancelled=deadline.stopped)
                if label == "same_source_reopen":
                    assert page.updated_index_rows == 0
                assert app.close_search()
                selected = records[start_record:]
                assert [row["phase"] for row in selected] == ["_sync", "_lexical"]
                result.append({"label": label, "first_page_seconds": elapsed,
                               "source_pages": page.source_pages, "total_results": page.total_results,
                               "index_bytes": page.index_bytes, "updated_index_rows": page.updated_index_rows,
                               "phases": selected, "other_time": "UNATTRIBUTED",
                               "search_memory_bytes": resources.memory_bytes})
    finally:
        assert app.close_search()
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("before", "measure", "after"))
    args = parser.parse_args()
    current = identity()
    if args.mode == "before":
        write("installed-before.json", current)
        return
    before = json.loads((OUT / "installed-before.json").read_text(encoding="utf-8"))
    assert current == before, "Exact implementation changed."
    if args.mode == "after":
        write("installed-after.json", current)
        print("CARR_SQL_IDENTITY " + json.dumps({"commit":current["commit"],
              "package_files":current["package_files"],"origin_count":len(current["origins"]),
              "unchanged":True}), flush=True)
        return
    module = benchmark()
    directory = Path(os.environ["CARR_SQL_DIR"]).resolve(strict=True)
    state, generation = module._state(directory)
    assert state["import_complete"] and state["books"] == int(os.environ["CARR_SQL_BOOKS"])
    assert state["segments"] == state["books"] * 40
    prior = module.read_json(directory / "benchmark" / "catalog-verification.json", maximum=module.MAX_STATE)
    assert prior["status"] == "CATALOG_VERIFIED" and prior["source_segments"] == state["segments"]
    sql = sql_diagnosis(module, directory, state)
    ranked = rank_diagnosis(module, directory, state, generation)
    originals = module.fixture.verify_originals(directory, generation)
    assert identity() == before
    report = {"status":"DIAGNOSTIC_COMPLETE", "qualification":"READONLY_SQL_AND_ACTUAL_RANK_DIAGNOSIS",
              "final_release":"NOT_QUALIFIED", "commit":current["commit"],"run":current["run"],
              "books":state["books"],"segments":state["segments"],"sql":sql,"ranked":ranked,
              "originals":originals,"product_sql_changed":False}
    write("diagnosis.json", report)
    summary = {key:report[key] for key in ("status","qualification","commit","run","books","segments","ranked")}
    summary["sql"] = [{"label":row["label"],"old_steps":row["old"]["vm_steps"],
                      "new_steps":row["new"]["vm_steps"],"rows":row["old"]["rows"],
                      "old_plan":row["old"]["plan"],"new_plan":row["new"]["plan"]}
                     for row in sql["records"]]
    print("CARR_SQL_DIAGNOSIS " + json.dumps(summary, sort_keys=True), flush=True)

if __name__ == "__main__":
    main()
