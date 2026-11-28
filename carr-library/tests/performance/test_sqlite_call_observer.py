"""Real SQLite/profile-owner controls; no import-scale or native timing claim."""
from contextlib import closing
import cProfile
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import FunctionType

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "sqlite_call_observer.py"
SPEC = importlib.util.spec_from_file_location("_carr_sqlite_observer_controls", HELPER)
observer_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer_module)
PILOT_SPEC = importlib.util.spec_from_file_location(
    "_carr_growth_sqlite_controls", ROOT / "scripts" / "benchmark_library_growth.py")
pilot = importlib.util.module_from_spec(PILOT_SPEC)
PILOT_SPEC.loader.exec_module(pilot)


def echo_twice(connection, value):
    first = connection.execute("SELECT ?", (value,)).fetchone(); second = connection.execute("SELECT 7").fetchall()
    return first[0], second


def fetch_in_parts(connection):
    cursor = connection.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
    return cursor.fetchone(), cursor.fetchmany(1), cursor.fetchall()


def implicit_iteration(connection):
    return sum(row[0] for row in connection.execute("SELECT 1 UNION ALL SELECT 2"))


def missing_table(connection, seen):
    try:
        return connection.execute("SELECT * FROM definitely_missing_observer_table").fetchone()
    except sqlite3.Error as error:
        seen.append(error)
        raise


def transaction_write(connection, value):
    connection.execute("INSERT INTO evidence(value) VALUES(?)", (value,))
    return connection.execute("SELECT value FROM evidence").fetchone()


def interruptible(connection):
    return connection.execute(
        "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000) "
        "SELECT sum(x) FROM n").fetchone()


def raise_after_query(connection, failure):
    connection.execute("SELECT 1").fetchone()
    raise failure


def caller_context(connection, value):
    return echo_twice(connection, value)


def replace_profile_hook(connection, hook):
    result = connection.execute("SELECT 9").fetchone()
    sys.setprofile(hook)
    return result


def observe(*functions, context_functions=(), **limits):
    source = Path(__file__).resolve()
    return observer_module.NativeSQLiteObserver(functions,
        {source: sha256(source.read_bytes()).hexdigest()},
        context_functions=context_functions, **limits)


def rows_for(record, operation):
    sites = {site["site_id"]: site for site in record["sites"]}
    return [row for row in record["rows"]
            if sites[row["site_id"]]["operation"] == operation]


def test_same_line_native_calls_have_distinct_exact_spans_and_real_fetch_intervals():
    value = observe(echo_twice)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert type(connection) is sqlite3.Connection
        result = value.run(echo_twice, connection, "exact-parameter")
        assert result == ("exact-parameter", [(7,)])
        assert connection.execute("SELECT 11").fetchone() == (11,)
    record = value.record()
    assert record["status"] == "COMPLETE" and record["errors"] == []
    assert sum(row["calls"] for row in rows_for(record, "execute")) == 2
    assert sum(row["calls"] for row in rows_for(record, "fetchone")) == 1
    assert sum(row["calls"] for row in rows_for(record, "fetchall")) == 1
    executed = [site for site in record["sites"] if site["operation"] == "execute"]
    assert len(executed) == 2
    assert executed[0]["span"]["line"] == executed[1]["span"]["line"]
    assert executed[0]["span"]["column"] != executed[1]["span"]["column"]
    assert executed[0]["site_id"] != executed[1]["site_id"]
    by_id = {site["site_id"]: site for site in record["sites"]}
    for row in record["rows"]:
        assert row["calls"] == row["returns"] == 1 and row["exceptions"] == 0
        assert row["elapsed_ns"] >= row["maximum_ns"] > 0
        offsets = by_id[row["site_id"]]["allowed_instruction_offsets"]
        assert row["start_offsets"] and row["end_offsets"]
        assert set(row["start_offsets"]) <= set(offsets)
        assert set(row["end_offsets"]) <= set(offsets)
    assert record["selected_native_events"] == 8
    assert sys.getprofile() is None


def test_sql_parameters_and_row_values_never_enter_the_observation_record():
    secret = "PRIVATE-binding-value-9a6ff17e"
    value = observe(echo_twice)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(echo_twice, connection, secret)[0] == secret
    raw = json.dumps(value.record(), ensure_ascii=True)
    assert secret not in raw
    assert "SELECT ?" not in raw and "SELECT 7" not in raw
    assert '"locals"' not in raw and '"parameters"' not in raw
    assert "literal_sql_sha256" in raw and "expression_sha256" in raw


def test_explicit_fetch_methods_remain_separate_from_execute():
    value = observe(fetch_in_parts)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(fetch_in_parts, connection) == ((1,), [(2,)], [(3,)])
    record = value.record()
    assert record["status"] == "COMPLETE"
    for name in ("execute", "fetchone", "fetchmany", "fetchall"):
        assert sum(row["calls"] for row in rows_for(record, name)) == 1
    assert "Implicit cursor iteration is excluded" in record["scope"]


def test_implicit_iteration_is_not_misreported_as_measured_fetch_work():
    value = observe(implicit_iteration)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(implicit_iteration, connection) == 3
    record = value.record()
    assert record["status"] == "COMPLETE"
    assert sum(row["calls"] for row in rows_for(record, "execute")) == 1
    assert not any(rows_for(record, name) for name in ("fetchone", "fetchmany", "fetchall"))
    assert record["selected_native_events"] == 2


def test_native_sql_exception_is_paired_and_the_exact_original_exception_propagates():
    value, seen = observe(missing_table), []
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(sqlite3.OperationalError) as caught:
            value.run(missing_table, connection, seen)
        assert seen == [caught.value]
        assert connection.execute("SELECT 1").fetchone() == (1,)
    record = value.record()
    assert record["status"] == "COMPLETE"
    row, = rows_for(record, "execute")
    assert row["calls"] == row["exceptions"] == 1 and row["returns"] == 0
    assert record["selected_native_events"] == 2 and sys.getprofile() is None


def test_native_transaction_row_factory_and_authorizer_stay_owned_by_the_real_connection():
    value = observe(transaction_write)
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE evidence(value TEXT)")
        authorized = []
        def authorize(*args):
            authorized.append(args[0])
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        original_identity = id(connection)
        original_isolation = connection.isolation_level
        result = value.run(transaction_write, connection, "retained")
        assert type(connection) is sqlite3.Connection and id(connection) == original_identity
        assert isinstance(result, sqlite3.Row) and result["value"] == "retained"
        assert connection.row_factory is sqlite3.Row
        assert connection.isolation_level == original_isolation and connection.in_transaction
        assert authorized
        connection.rollback()
        assert not connection.in_transaction
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
    assert value.record()["status"] == "COMPLETE"


def test_real_sqlite_progress_cancellation_and_exception_are_not_replaced():
    value, progress = observe(interruptible), []
    with closing(sqlite3.connect(":memory:")) as connection:
        def cancelled():
            progress.append(True)
            return 1
        connection.set_progress_handler(cancelled, 1)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            value.run(interruptible, connection)
        assert progress
        connection.set_progress_handler(None, 0)
        assert connection.execute("SELECT 5").fetchone() == (5,)
    record = value.record()
    assert record["status"] == "COMPLETE"
    assert sum(row["exceptions"] for row in record["rows"]) == 1
    assert sys.getprofile() is None


def test_python_failure_after_native_calls_keeps_its_exact_identity_and_restores_profile():
    value = observe(raise_after_query)
    failure = RuntimeError("actual caller failure")
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(RuntimeError) as caught:
            value.run(raise_after_query, connection, failure)
        assert caught.value is failure
    record = value.record()
    assert record["status"] == "COMPLETE" and sys.getprofile() is None
    assert sum(row["calls"] for row in record["rows"]) == 2
    assert sum(row["exceptions"] for row in record["rows"]) == 0


def test_only_the_actual_target_code_object_and_registered_caller_are_observed():
    value = observe(echo_twice, context_functions=(caller_context,))
    # Identical source code at another code-object identity must not be enrolled.
    copy_code = echo_twice.__code__.replace()
    copy_function = FunctionType(copy_code, echo_twice.__globals__)
    assert copy_code == echo_twice.__code__ and copy_code is not echo_twice.__code__
    with closing(sqlite3.connect(":memory:")) as connection:
        def actual():
            first = copy_function(connection, "outside")
            second = caller_context(connection, "inside")
            return first, second
        result = value.run(actual)
        assert result == (("outside", [(7,)]), ("inside", [(7,)]))
    record = value.record()
    assert record["status"] == "COMPLETE" and record["selected_native_events"] == 8
    assert all(row["caller"]["function"] == "caller_context" for row in record["rows"])
    function, = record["functions"]
    assert function["invocations"] == 1


def test_other_thread_calls_are_excluded_without_installing_a_thread_hook():
    value, results, errors = observe(echo_twice), [], []
    prior_thread_hook = threading.getprofile()
    def worker():
        try:
            with closing(sqlite3.connect(":memory:")) as connection:
                results.append(echo_twice(connection, "other-thread"))
        except BaseException as error:
            errors.append(error)
    with closing(sqlite3.connect(":memory:")) as connection:
        def actual():
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            return echo_twice(connection, "owner-thread")
        assert value.run(actual) == ("owner-thread", [(7,)])
    assert not errors and results == [("other-thread", [(7,)])]
    record = value.record()
    assert record["status"] == "COMPLETE" and record["selected_native_events"] == 8
    assert record["thread"] == threading.get_ident()
    assert threading.getprofile() is prior_thread_hook


def test_existing_cprofile_hook_is_refused_before_the_operation_and_is_not_removed():
    value, entered = observe(echo_twice), []
    profile = cProfile.Profile()
    profile.enable()
    original = sys.getprofile()
    try:
        with pytest.raises(ValueError, match="Refuse nested profiling"):
            value.run(lambda: entered.append(True))
        assert sys.getprofile() is original
    finally:
        profile.disable()
    assert not entered and sys.getprofile() is None


def test_a_replaced_hook_is_reported_invalid_without_stealing_the_new_owner():
    value = observe(replace_profile_hook)
    def replacement(frame, event, argument):
        return None
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            assert value.run(replace_profile_hook, connection, replacement) == (9,)
        assert sys.getprofile() is replacement
    finally:
        sys.setprofile(None)
    record = value.record()
    assert record["status"] == "INVALID" and record["errors"] == ["observer-hook-replaced"]


@pytest.mark.parametrize("bound", ["events", "rows"])
def test_finite_observation_overflow_is_sticky_and_does_not_change_the_actual_operation(bound):
    value = observe(echo_twice, **({"max_events": 2} if bound == "events" else {"max_rows": 1}))
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(echo_twice, connection, "operation-completed") == ("operation-completed", [(7,)])
        assert connection.execute("SELECT 13").fetchone() == (13,)
    record = value.record()
    assert record["status"] == "INVALID" and record["errors"]
    assert len(record["rows"]) <= record["limits"]["rows"]
    assert sys.getprofile() is None


@pytest.mark.parametrize("limits", [
    {"max_events": 0}, {"max_events": True}, {"max_events": 4097},
    {"max_rows": 0}, {"max_rows": False}, {"max_rows": 129},
])
def test_invalid_bounds_are_refused_before_starting_any_profile(limits):
    with pytest.raises(ValueError, match="bounds"):
        observe(echo_twice, **limits)
    assert sys.getprofile() is None


def test_explicit_wrong_source_hash_refuses_before_the_real_operation():
    with pytest.raises(ValueError, match="source bytes"):
        observer_module.NativeSQLiteObserver((echo_twice,), {Path(__file__).resolve(): "0" * 64})
    assert sys.getprofile() is None


def test_replaced_function_code_cannot_borrow_a_valid_source_file_identity():
    constants = tuple("SELECT 8" if item == "SELECT 7" else item for item in echo_twice.__code__.co_consts)
    forged = FunctionType(echo_twice.__code__.replace(co_consts=constants), echo_twice.__globals__)
    path = Path(__file__).resolve()
    with pytest.raises(ValueError, match="function code"):
        observer_module.NativeSQLiteObserver((forged,), {path: sha256(path.read_bytes()).hexdigest()})
    assert sys.getprofile() is None


def test_source_change_after_observed_operation_invalidates_the_receipt(tmp_path):
    path = tmp_path / "owned-observation-fixture.py"
    path.write_text('def query(connection):\n    return connection.execute("SELECT 17").fetchone()\n', encoding="utf-8")
    namespace = {"__name__": "_carr_temporary_query_fixture"}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    value = observer_module.NativeSQLiteObserver((namespace["query"],),
        {path: sha256(path.read_bytes()).hexdigest()})
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(namespace["query"], connection) == (17,)
    path.write_text(path.read_text(encoding="utf-8") + "# changed source\n", encoding="utf-8")
    record = value.record()
    assert record["status"] == "INVALID" and record["errors"] == ["observed-source-changed"]


def test_observer_cannot_be_reused_or_emit_an_unstarted_success():
    value = observe(echo_twice)
    with pytest.raises(ValueError, match="completed its lifetime"):
        value.record()
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(echo_twice, connection, "first")[0] == "first"
        with pytest.raises(ValueError, match="cannot be reused"):
            value.run(echo_twice, connection, "second")
    assert value.record()["status"] == "COMPLETE" and sys.getprofile() is None


def current_proof(path):
    import carr_library
    package = Path(carr_library.__file__).resolve().parent
    hashes = {p.relative_to(package).as_posix(): sha256(p.read_bytes()).hexdigest()
              for p in sorted(package.rglob("*.py"))}
    pilot.write_record(path, dict(status="PASS", source_commit="a" * 40,
        installed_root=str(package), installed_python_files=len(hashes), source_hashes=hashes))
    return pilot.source_identity(path, "a" * 40)


@pytest.mark.parametrize("mode", ["cprofile", "sqlite"])
def test_actual_public_import_keeps_retention_global_checks_and_default_profile_mode(tmp_path, mode):
    from carr_library.application import LibraryApp
    identity = current_proof(tmp_path / "source.json")
    library = LibraryApp(tmp_path / "library")
    workspace = library.create_workspace("Observer's genuine small fixture")
    text = "Observed needle first.\fObserved needle second."
    selected = pilot.profile_call if mode == "cprofile" else pilot.sqlite_profile_call
    result, receipt = selected(tmp_path, identity, "import-000001",
        {"source_sha256": sha256(text.encode()).hexdigest(), "book_ordinal": 1},
        library.add_text, workspace, "Genuine observer control", text,
        edition_id="observer-edition", work_id="observer-work")
    assert result["state"] == "complete" and result["completed"] == 2
    assert library.assets.resolve(result["asset"]).read_bytes() == text.encode()
    with library.read(workspace) as db:
        assert db.execute("SELECT count(*) FROM active").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM page").fetchone()[0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
    matches = library.search_page(workspace, "needle", mode="terms")
    assert len(matches.results) == 2 and all(row.verified for row in matches.results)
    record, digest = pilot.read_record(tmp_path / receipt["path"])
    assert digest == receipt["sha256"] and record["outcome"] == "returned"
    if mode == "cprofile":
        assert record["schema"] == "carr-library-selected-call-profile/1"
        assert "native_sqlite" not in record
    else:
        assert record["schema"] == "carr-library-selected-native-sqlite-profile/1"
        actual = record["native_sqlite"]
        assert actual["status"] == "COMPLETE" and actual["errors"] == []
        counts = {row["identity"]["function"]: row["invocations"] for row in actual["functions"]}
        assert counts == {"Storage._check": 2, "Storage._check_locators": 3}
        sites = {row["site_id"]: row for row in actual["sites"]}
        parents = {row["caller"]["function"] for row in actual["rows"]
                   if sites[row["site_id"]]["function"]["function"] == "Storage._check"}
        assert parents == {"_CatalogPublisher.publish_revision", "Storage.check"}
        assert not any(site["operation"] not in ("execute", "fetchone") for site in actual["sites"])
        assert record["observer_source_sha256"] == sha256(HELPER.read_bytes()).hexdigest()
    assert sys.getprofile() is None


def test_a_mismatched_diagnostic_helper_cannot_execute_the_public_operation(tmp_path, monkeypatch):
    identity = current_proof(tmp_path / "source.json")
    entered = []
    monkeypatch.setattr(pilot, "SQLITE_OBSERVER_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="observer changed"):
        pilot.sqlite_profile_call(tmp_path, identity, "import-000001", {},
                                  lambda: entered.append(True))
    assert entered == [] and not (tmp_path / "profile-import-000001.json").exists()
    assert sys.getprofile() is None


def test_returned_operation_with_invalid_observation_cannot_be_reported_as_profile_success(tmp_path):
    identity = current_proof(tmp_path / "source.json")
    entered = []
    def ordinary_operation():
        entered.append(True)
        return "actual operation returned without a selected SQLite call"
    with pytest.raises(ValueError, match="not profile evidence"):
        pilot.sqlite_profile_call(tmp_path, identity, "import-000001", {},
                                  ordinary_operation)
    assert entered == [True]
    record, digest = pilot.read_record(tmp_path / "profile-import-000001.json")
    assert record["outcome"] == "returned"
    assert record["native_sqlite"]["status"] == "INVALID"
    assert record["native_sqlite"]["errors"] == ["no-observed-native-calls"]
    assert record["native_sqlite"]["rows"] == [] and digest
    assert sys.getprofile() is None


@pytest.mark.parametrize("failure", [KeyboardInterrupt("exact interruption"), SystemExit(17)],
                         ids=["keyboard-interrupt", "system-exit"])
def test_base_exception_during_real_native_profile_callback_propagates_and_releases_hook(monkeypatch, failure):
    value = observe(echo_twice)
    timestamps = []
    def interrupted_timestamp():
        timestamps.append(True)
        raise failure
    monkeypatch.setattr(observer_module, "perf_counter_ns", interrupted_timestamp)
    with closing(sqlite3.connect(":memory:")) as connection:
        with pytest.raises(type(failure)) as caught:
            value.run(echo_twice, connection, "not returned")
        assert caught.value is failure
        assert timestamps == [True] and sys.getprofile() is None
        assert connection.execute("SELECT 23").fetchone() == (23,)
    record = value.record()
    assert record["status"] == "INVALID"
    assert record["errors"] == ["observer-callback-" + type(failure).__name__]
    assert sys.getprofile() is None


def test_ordinary_diagnostic_exception_remains_invalid_without_changing_real_sqlite_result(monkeypatch):
    value = observe(echo_twice)
    failure = RuntimeError("observer timestamp failed")
    timestamps = []
    def failed_timestamp():
        timestamps.append(True)
        raise failure
    monkeypatch.setattr(observer_module, "perf_counter_ns", failed_timestamp)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert value.run(echo_twice, connection, "returned") == ("returned", [(7,)])
        assert connection.execute("SELECT 29").fetchone() == (29,)
    record = value.record()
    assert timestamps == [True] and sys.getprofile() is None
    assert record["status"] == "INVALID"
    assert record["errors"] == ["observer-callback-RuntimeError"]
