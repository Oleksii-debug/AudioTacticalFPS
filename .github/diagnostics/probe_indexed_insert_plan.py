"""Inspect native indexed INSERT plans on the exact installed current schema.

Canonical LibraryApp/Storage publishers create a small authored fixture first.
During the guarded phase every candidate is EXPLAIN-only under query_only.
This diagnoses compiled native instructions, not large-file capacity or cause.
"""
from contextlib import closing
from hashlib import sha256
import ast
import faulthandler
import importlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "carr-library"
REPORT = PACKAGE / "ci-indexed-insert-plan"
ORIGINS = (
    "carr_library.application",
    "carr_library.storage.revisions",
    "carr_library.storage.publication",
    "carr_library.catalog.registration",
    "carr_library.catalog.intake",
    "carr_library.durable_state",
    "carr_library.search.originals",
)
STAGE = "INSERT INTO indexed SELECT id,raw FROM page WHERE revision=?"
REBUILD = "INSERT INTO indexed SELECT id,raw FROM active_page"
VALUES = "INSERT INTO indexed(id,raw) VALUES(?,?)"
SCALAR = "INSERT INTO indexed(id,raw) VALUES(?,(SELECT raw FROM page WHERE id=?))"
TEMP_OPS = {"OpenEphemeral", "OpenAutoindex", "SorterOpen", "SorterInsert"}
STARTED = time.monotonic()


def check_deadline():
    if time.monotonic() - STARTED > 90:
        raise TimeoutError("The bounded native INSERT-plan observation expired.")


def file_hash(path):
    check_deadline()
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and not path.is_symlink(), str(path)
    assert metadata.st_nlink == 1 and metadata.st_size <= 8 * 1024**2, str(path)
    data = path.read_bytes()
    after = path.lstat()
    assert (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), str(path)
    assert len(data) == metadata.st_size
    return sha256(data).hexdigest()


def inventory(root):
    paths = sorted(root.rglob("*.py"))
    assert 1 <= len(paths) <= 4096
    return [[str(path.relative_to(root)).replace("\\", "/"), file_hash(path)]
            for path in paths]


def identity():
    import carr_library
    assert sys.flags.isolated and not os.environ.get("PYTHONPATH")
    source = (PACKAGE / "src" / "carr_library").resolve()
    installed = (Path(sysconfig.get_paths()["purelib"]) / "carr_library").resolve()
    assert Path(carr_library.__file__).resolve() == installed / "__init__.py"
    assert installed != source and not installed.is_relative_to(PACKAGE / "src")
    source_files, installed_files = inventory(source), inventory(installed)
    assert source_files == installed_files, "Source and installed package differ."
    selected = os.environ["CARR_INSERT_PLAN_HEAD"]
    assert len(selected) == 40 and all(c in "0123456789abcdef" for c in selected)
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=10)
    assert completed.stdout.strip() == selected
    origins = {}
    for name in ORIGINS:
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve()
        relative = Path(*name.split(".")[1:]).with_suffix(".py")
        assert actual == installed / relative, (name, actual)
        assert file_hash(actual) == file_hash(source / relative)
        origins[name] = {"path": str(actual), "sha256": file_hash(actual)}
    return {
        "commit": selected, "python": sys.version, "executable": sys.executable,
        "package_files": len(source_files),
        "package_inventory": source_files,
        "package_sha256": sha256(json.dumps(
            source_files, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest(),
        "origins": origins, "observer_sha256": file_hash(Path(__file__).resolve()),
    }


def plans(db, label, sql, parameters):
    check_deadline()
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    query_plan = [list(row) for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
    opcodes = [list(row) for row in db.execute("EXPLAIN " + sql, parameters)]
    assert len(query_plan) <= 128 and 1 <= len(opcodes) <= 1024
    for row in query_plan + opcodes:
        assert all(not isinstance(value, str) or len(value) <= 2048 for value in row)
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    return {
        "label": label, "sql": sql, "parameters": list(parameters),
        "query_plan": query_plan, "opcodes": opcodes,
        "temporary_instructions": [row for row in opcodes if row[1] in TEMP_OPS],
        "opcode_count": len(opcodes),
    }


def observe():
    from carr_library.application import LibraryApp, _CatalogPublisher
    from carr_library.catalog.registration import Registration
    from carr_library.storage.revisions import CURRENT_SCHEMA, Page, Storage
    import carr_library.storage.revisions as revisions

    assert CURRENT_SCHEMA == 5, CURRENT_SCHEMA
    # Bind these exact production statement strings before inspecting them.
    constants = {node.value for node in ast.walk(ast.parse(
        Path(revisions.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and type(node.value) is str}
    assert STAGE in constants and REBUILD in constants

    with tempfile.TemporaryDirectory(prefix="carr-indexed-insert-plan-") as private:
        app = LibraryApp(Path(private) / "library")
        try:
            workspace = app.create_workspace("Authored native SQL fixture")
            segments = (
                "Needle — точний Cafe\u0301\r\n" + "A" * 4096,
                "Second actual segment.",
                "Third source segment.",
            )
            data = "\f".join(segments).encode("utf-8")
            digest = sha256(data).hexdigest()
            app.assets.retain(data, "text/plain", digest)
            record = Registration(
                "plan-work", "plan-edition",
                dict(sha256=digest, mime="text/plain", size=len(data)),
                "Authored exact INSERT-plan source", ("Fixture Author",),
            )
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
                publisher = _CatalogPublisher(storage, record)
                publisher.publish_revision(
                    workspace, record.edition_id, "plan-current", digest, "fixture",
                    [Page(value) for value in segments])
                storage.check()
                db = storage.db
                assert not db.in_transaction
                assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA
                rows = [tuple(row) for row in db.execute(
                    "SELECT id,raw FROM page WHERE revision=? ORDER BY ordinal",
                    ("plan-current",))]
                assert len(rows) == 3 and [row[1] for row in rows] == list(segments)
                original = app.assets.resolve(digest)
                assert original.is_file()
                original_before = file_hash(original)
                before = file_hash(app.database)
                before_changes = db.total_changes
                before_rows = [tuple(row) for row in db.execute(
                    "SELECT id,raw FROM indexed ORDER BY id")]
                assert before_rows == rows
                trigger = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='indexed_ai'"
                ).fetchone()[0]
                assert "INSERT INTO page_fts" in trigger

                db.execute("PRAGMA query_only=ON")
                try:
                    observations = [
                        plans(db, "production_stage_select", STAGE, ("plan-current",)),
                        plans(db, "production_rebuild_select", REBUILD, ()),
                        plans(db, "bound_values", VALUES, rows[0]),
                        plans(db, "scalar_page_primary_key_values", SCALAR,
                              (rows[0][0], rows[0][0])),
                    ]
                    assert db.total_changes == before_changes
                    assert not db.in_transaction
                    assert [tuple(row) for row in db.execute(
                        "SELECT id,raw FROM indexed ORDER BY id")] == before_rows
                    assert file_hash(app.database) == before
                    assert file_hash(original) == original_before
                    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
                finally:
                    db.execute("PRAGMA query_only=OFF")
                assert db.total_changes == before_changes
                assert file_hash(app.database) == before
            assert file_hash(app.database) == before
            assert file_hash(original) == original_before
            return {
                "schema_version": CURRENT_SCHEMA, "sqlite": sqlite3.sqlite_version,
                "source_bytes": len(data), "source_sha256": digest,
                "source_unchanged": True, "database_sha256": before,
                "database_unchanged": True, "total_changes_unchanged": True,
                "indexed_ai": trigger, "observations": observations,
                "fixture": "Three canonical current-schema pages; no direct SQL seeding.",
                "plan_phase": "EXPLAIN-only, query_only=ON; no candidate INSERT executed.",
            }
        finally:
            assert app.close_search(), "The actual app cursor owner did not close."


def main():
    REPORT.mkdir(exist_ok=True)
    faulthandler.enable()
    faulthandler.dump_traceback_later(110, exit=True)
    before = identity()
    (REPORT / "before.json").write_text(
        json.dumps(before, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    try:
        result = observe()
    finally:
        after = identity()
        (REPORT / "after.json").write_text(
            json.dumps(after, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        assert before == after, "Source, installed code or observer changed."
    output = {
        "schema": "carr-native-indexed-insert-plan/1",
        "qualification": "NATIVE_EXPLAIN_DIAGNOSIS_ONLY", "status": "PASS",
        "head": before["commit"], "run": os.environ["CARR_INSERT_PLAN_RUN"],
        "package_files": before["package_files"],
        "package_sha256": before["package_sha256"],
        "origin_count": len(before["origins"]),
        "observer_sha256": before["observer_sha256"],
        "result": result,
        "limits": "No large-file capacity, live INSERT equivalence, Windows allocation cause, or throughput qualification.",
    }
    encoded = json.dumps(output, ensure_ascii=True, sort_keys=True)
    assert len(encoded.encode("utf-8")) <= 128 * 1024
    (REPORT / "plans.json").write_text(encoded + "\n", encoding="utf-8")
    print("CARR_INDEXED_INSERT_PLAN " + encoded, flush=True)
    faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
