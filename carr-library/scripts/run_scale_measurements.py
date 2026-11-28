"""Run the reviewed scale adapters with bounded, separately classified evidence.

This Windows CI driver uses fresh synthetic profiles only. Unit-contract success,
a fully observed profile, missing cancellation proof, resource refusal and actual
failure have different outcomes. No outcome qualifies human performance, NVDA,
physical audio, private models or an OS-enforced memory limit.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import csv
from hashlib import sha1, sha256
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import time
import tomllib
import traceback
import types

SCHEMA = "carr-measured-scale-run/1"
TASK_SCHEMA = "carr-measured-scale-stage/1"
MIB = 1024**2
MAX_LOG = 4*MIB
MAX_JSON = 2*MIB
MAX_STAGE_JSON = 64*1024
MAX_RESUME_CALLS = 128
MAX_ARTIFACT_BYTES = 96*MIB
ROOT = Path(__file__).resolve().parents[1]
HELPER = "scripts/ci_feature_capacity.py"
WHEEL_HELPER = "packaging/source_wheel.py"
POLICY = "scripts/ci_check.py"
REFERENCE = "review/capacity-python-reference-2026-11-28.json"
SCRIPTS = (
    "benchmark_library_scale.py", "benchmark_library_catalog.py",
    "benchmark_library_backup.py",
)
CONTRACTS = (
    "tests/scale/test_library_scale_contract.py",
    "tests/scale/test_library_catalog_benchmark.py",
    "tests/scale/test_library_backup_benchmark.py",
    "tests/scale/test_library_scale_posix.py",
)
BINDINGS = (HELPER, WHEEL_HELPER, POLICY, REFERENCE, "pyproject.toml",
    "packaging/windows-cp311.lock", "packaging/windows-semantic-cp311.lock",
    "scripts/windows_reader_player_smoke.py", "scripts/run_scale_measurements.py",
    ) + tuple("scripts/" + item for item in SCRIPTS) + CONTRACTS
STAGES = ("01-generate", "02-initialize", "03-cancel", "04-postcommit-exit",
          "05-resume", "06-verify", "07-search", "08-backup")
EXPORTS = {
    "preflight.json": "fixture-preflight.json",
    "generation.json": "fixture-generation.json",
    "verified-generation.json": "fixture-verified-generation.json",
    "benchmark/cancellation.json": "import-cancellation.json",
    "benchmark/authored-crash.json": "authored-postcommit-exit.json",
    "benchmark/catalog-verification.json": "catalog-verification.json",
    "benchmark/search-measurements.json": "search-measurements.json",
    "benchmark/backup-restore.json": "backup-restore.json",
}


def require(value, message):
    if not value:
        raise AssertionError(message)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def read_bytes(path, maximum=MAX_JSON):
    from carr_library.durable_state import read_bytes_snapshot
    return read_bytes_snapshot(path, max_bytes=maximum)[0]


def read_json(path, maximum=MAX_JSON):
    from carr_library.durable_state import read_json_snapshot
    return read_json_snapshot(path, max_bytes=maximum)[0]


def present(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def save(path, value):
    from carr_library.durable_state import atomic_json
    data = canonical(value)
    maximum = MAX_STAGE_JSON if path.name.endswith(("-request.json", "-result.json")) else MAX_JSON
    require(len(data) <= maximum, "One scale receipt exceeded its fixed JSON allowance.")
    atomic_json(path, value, replace=present(path))


def git(*arguments):
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(ROOT.parent), *arguments],
        env=dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0"),
        capture_output=True, check=True, timeout=20)
    require(len(result.stdout) <= 4*MIB, "The finite committed source inventory grew unexpectedly.")
    return result.stdout


def blob_hash(raw):
    return sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def bound_module(name, relative, head):
    path = ROOT / relative
    raw = read_bytes(path)
    committed = git("show", head + ":carr-library/" + relative)
    require(raw == committed, "The selected helper differs from its exact Git commit: " + relative)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def identity(head):
    """Reuse existing whole-namespace/RECORD and setuptools source binding."""
    require(re.fullmatch(r"[0-9a-f]{40}", head) is not None, "An exact source commit is required.")
    require(sys.flags.isolated and sys.dont_write_bytecode and __debug__,
            "Scale receiving requires isolated, nonoptimized, bytecode-free Python.")
    require(not any(os.environ.get(key) for key in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")),
            "Ambient package/test configuration is not part of this receiving run.")
    helper = bound_module("_carr_scale_identity", HELPER, head)
    source_wheel = bound_module("_carr_scale_source_wheel", WHEEL_HELPER, head)
    helper.checkout_head(ROOT, head)
    entries = source_wheel._tree(ROOT.parent, head)
    source = (ROOT / "src/carr_library").resolve(strict=True)
    installed = (Path(sysconfig.get_path("purelib")) / "carr_library").resolve(strict=True)
    require(installed != source and not installed.is_relative_to(source),
            "Measured stages must use the actual isolated installed package.")
    package = importlib.import_module("carr_library")
    require(Path(package.__file__).resolve() == installed / "__init__.py",
            "The measured process imported another package origin.")

    bindings = {}
    for relative in BINDINGS:
        path = ROOT / relative
        raw = read_bytes(path)
        row = entries.get("carr-library/" + relative)
        require(row is not None and row[0] in ("100644", "100755") and row[1] == "blob"
                and blob_hash(raw) == row[2], "A selected script/test/helper changed: " + relative)
        bindings[relative] = {"sha256": sha256(raw).hexdigest(), "bytes": len(raw), "git_blob": row[2]}
    workflow = ROOT.parent / ".github/workflows/carr-scale-evaluation.yml"
    workflow_raw = read_bytes(workflow)
    require(workflow_raw == git("show", head + ":.github/workflows/carr-scale-evaluation.yml"),
            "The dispatched scale workflow differs from its committed bytes.")
    bindings["../.github/workflows/carr-scale-evaluation.yml"] = {
        "sha256": sha256(workflow_raw).hexdigest(), "bytes": len(workflow_raw),
        "git_blob": blob_hash(workflow_raw)}

    reference = read_json(ROOT / REFERENCE)
    require(reference["schema"] == "carr-python-reference/1"
            and reference["status"] == "SOURCE_REFERENCE_NOT_RUNTIME_QUALIFICATION",
            "The committed source reference has an unexpected meaning.")
    expected = reference["compact_mapping"]
    require(len(expected) == 398 and helper.inventory(source) == helper.inventory(installed) == expected,
            "The full source/installed Python namespace differs from the committed 398-module reference.")
    recorded_git = {row["path"]: row["git_blob"] for row in reference["git_rows"]}
    require(len(recorded_git) == 398, "The source reference repeats Python paths.")
    current_git = {path: row[2] for path, row in entries.items()
        if path.startswith("carr-library/src/carr_library/") and path.endswith(".py")}
    require(current_git == recorded_git, "The committed reference does not describe this actual source head.")
    record_hash = helper.installed_record(installed, expected)

    distribution = importlib.metadata.distribution("universal-research-library")
    require(Path(distribution.locate_file("carr_library")).resolve() == installed,
            "The source-bound distribution is not the selected package owner.")
    record = distribution.read_text("RECORD")
    require(record is not None and sha256(record.encode("utf-8")).hexdigest() == record_hash,
            "Installed RECORD changed during proof.")
    record_rows = {}
    for name, checksum, size in csv.reader(record.splitlines()):
        require(name not in record_rows, "Installed RECORD repeats a path.")
        record_rows[name] = (checksum, size)

    project_raw = read_bytes(ROOT / "pyproject.toml")
    project = tomllib.loads(project_raw.decode("utf-8"))
    admitted, required = source_wheel._inventory(project, entries)
    payload = {}
    for destination, original in sorted(admitted.items()):
        raw = read_bytes(ROOT.parent / original, maximum=8*MIB)
        row = entries[original]
        require(row[0] in ("100644", "100755") and row[1] == "blob" and blob_hash(raw) == row[2],
                "A committed source payload changed: " + original)
        target = Path(distribution.locate_file(destination))
        require(target.resolve().is_relative_to(installed.parent)
                and read_bytes(target, maximum=8*MIB) == raw,
                "An installed payload differs from its exact committed source: " + destination)
        checksum = sha256(raw).hexdigest()
        encoded = base64.urlsafe_b64encode(bytes.fromhex(checksum)).decode("ascii").rstrip("=")
        require(record_rows[destination] == ("sha256=" + encoded, str(len(raw))),
                "Installed RECORD does not own the complete selected payload: " + destination)
        payload[destination] = {"sha256": checksum, "bytes": len(raw), "git_blob": row[2]}

    entry_names = [name for name in record_rows if name.endswith(".dist-info/entry_points.txt")]
    require(len(entry_names) == 1, "Expected one installed application entry-point declaration.")
    entry_name, = entry_names
    entry_raw = read_bytes(Path(distribution.locate_file(entry_name)))
    entry_hash = sha256(entry_raw).hexdigest()
    entry_encoded = base64.urlsafe_b64encode(bytes.fromhex(entry_hash)).decode("ascii").rstrip("=")
    require(record_rows[entry_name] == ("sha256=" + entry_encoded, str(len(entry_raw))),
            "Installed RECORD does not own the actual entry-point declaration.")
    source_wheel._entry_points({entry_name: entry_raw}, entry_name.rsplit("/", 1)[0] + "/",
                               project["project"])
    return {
        "commit": head, "python_files": len(expected), "python_inventory": expected,
        "installed_root": str(installed), "source_root": str(source),
        "payload_files": len(payload), "required_runtime_files": len(required), "payload_inventory": payload,
        "RECORD_sha256": record_hash, "entry_points_sha256": sha256(entry_raw).hexdigest(),
        "bindings": bindings, "python": sys.version,
        "executable": str(Path(sys.executable).resolve()), "prefix": str(Path(sys.prefix).resolve()),
    }


def offline(operation, head):
    # This is the existing strict policy, with its one test invocation replaced
    # by the explicit trusted script operation. All network/telemetry/exit
    # statements stay intact. Measurement is not labeled pytest qualification.
    policy = bound_module("_carr_scale_ci_policy", POLICY, head).PYTEST_CHILD
    old = "status = pytest.main(sys.argv[1:], plugins=[policy])"
    require(policy.count(old) == 1, "The reviewed existing offline-policy call changed.")
    code = policy.replace(old, "status = _scale_operation()")
    namespace = {"__name__": "__main__", "_scale_operation": operation}
    os.environ.update(ORT_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DO_NOT_TRACK="1")
    try:
        exec(compile(code, "<existing-scale-offline-policy>", "exec"), namespace)
    except SystemExit as error:
        require(type(error.code) is int, "The existing offline policy returned a noninteger verdict.")
        require(not namespace["policy"].seen, "The existing policy reported an expected failure.")
        return error.code
    raise AssertionError("The existing offline policy did not produce its exit verdict.")


def child(task_path, expected_sha):
    from carr_library.durable_state import read_json_snapshot
    task, checksum = read_json_snapshot(task_path, max_bytes=MAX_STAGE_JSON)
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha) is not None
            and checksum == expected_sha, "The captured stage request changed.")
    require(set(task) == {"schema", "head", "script", "argv", "identity_sha256"}
            and task["schema"] == TASK_SCHEMA and task["script"] in SCRIPTS
            and type(task["argv"]) is list and len(task["argv"]) <= 40
            and all(type(item) is str and len(item) <= 4096 and "\0" not in item for item in task["argv"]),
            "Invalid bounded scale stage request.")
    before = identity(task["head"])
    require(sha256(canonical(before)).hexdigest() == task["identity_sha256"],
            "The stage did not retain its parent's exact committed/installed identity.")

    def execute():
        import runpy
        previous = sys.argv
        sys.argv = [str(ROOT / "scripts" / task["script"]), *task["argv"]]
        try:
            try:
                runpy.run_path(sys.argv[0], run_name="__main__")
            except SystemExit as error:
                require(type(error.code) is int, "The selected adapter returned a noninteger exit.")
                return error.code
            return 0
        finally:
            sys.argv = previous

    error = None
    try:
        status = offline(execute, task["head"])
    except BaseException as caught:
        error = caught
        raise
    finally:
        from carr_library.audio.cleanup import drain_pending_cleanup
        interruption = drain_pending_cleanup(error)
        require(identity(task["head"]) == before, "Stage source/installed identity changed.")
        if error is None and interruption is not None:
            raise interruption
    return status


def interpreter():
    executable, environment = sys.executable, dict(os.environ)
    if (sys.prefix != sys.base_prefix and getattr(sys, "_base_executable", None)
            and os.path.normcase(sys._base_executable) != os.path.normcase(executable)):
        environment.pop("PYTHONEXECUTABLE", None)
        environment["__PYVENV_LAUNCHER__"] = executable
        executable = sys._base_executable
    environment.pop("PYTHONPATH", None)
    return str(Path(executable).resolve(strict=True)), environment


def bounded_log(source, destination):
    """Retain one bounded per-stage log across independently owned resume calls."""
    from carr_library.durable_state import regular
    if not present(source):
        return {"file": destination.name, "missing": True, "observed_bytes": None,
                "retained_bytes": 0, "truncated": False}
    info = regular(source)
    offset = regular(destination).st_size if present(destination) else 0
    require(offset <= MAX_LOG, "A previous measured log exceeded its allowance.")
    with source.open("rb") as stream:
        data = stream.read(MAX_LOG - offset)
    with destination.open("ab" if present(destination) else "xb") as stream:
        stream.write(data)
    retained = read_bytes(destination, maximum=MAX_LOG)
    return {"file": destination.name, "offset": offset, "observed_bytes": info.st_size,
        "retained_bytes": len(data), "truncated": info.st_size > len(data),
        "aggregate_bytes": len(retained), "aggregate_sha256": sha256(retained).hexdigest()}


def failure(error, *, trace=False):
    result = {"type": type(error).__name__, "message": str(error)[:4000]}
    if trace:
        result["traceback"] = "".join(traceback.format_exception(error, limit=12))[-12000:]
    return result


def run_stage(name, script, arguments, *, report, work, head, before, timeout):
    from carr_library.audio.cleanup import drain_pending_cleanup, retained_context
    from carr_library.audio.process import owned_process
    require(name in STAGES and script in SCRIPTS, "Unknown measured stage.")
    task = {"schema": TASK_SCHEMA, "head": head, "script": script, "argv": list(arguments),
            "identity_sha256": sha256(canonical(before)).hexdigest()}
    task_path = report / (name + "-request.json")
    if present(task_path):
        require(read_json(task_path) == task, "A repeated stage changed its captured invocation.")
    else:
        save(task_path, task)
    request_sha = sha256(read_bytes(task_path)).hexdigest()
    result_path = report / (name + "-result.json")
    prior = read_json(result_path) if present(result_path) else None
    invocations = [] if prior is None else prior["invocations"]
    require(prior is None or (name == "05-resume" and prior["status"] == "RETURNED"),
            "Only a completed public import slice may be explicitly continued.")
    require(len(invocations) < MAX_RESUME_CALLS, "Measured resume exceeded its declared invocation inventory.")
    ordinal = len(invocations) + 1
    executable, environment = interpreter()
    command = [executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()),
               "--stage-task", str(task_path), "--stage-sha256", request_sha]
    stem = name + "-" + str(ordinal)
    stdout_path, stderr_path = work / (stem + ".stdout"), work / (stem + ".stderr")
    started, caught, code = time.monotonic(), None, None
    observed = {"name": name, "script": script, "argv": list(arguments),
        "status": "RUNNING", "timeout_seconds": timeout, "request_sha256": request_sha,
        "invocations": invocations}
    save(result_path, observed)
    print(json.dumps({"stage": name, "invocation": ordinal, "status": "STARTED",
                      "timeout_seconds": timeout}), flush=True)
    try:
        with retained_context(ExitStack()) as streams:
            output = streams.enter_context(stdout_path.open("xb"))
            errors = streams.enter_context(stderr_path.open("xb"))
            with owned_process(command, stdout=output, stderr=errors, env=environment, cwd=ROOT) as process:
                while process.poll() is None:
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError("The explicit measured stage deadline elapsed.")
                    if max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_LOG:
                        raise ValueError("A measured stage exceeded its bounded diagnostic output.")
                    time.sleep(.05)
                code = process.returncode
                require(max(os.fstat(output.fileno()).st_size, os.fstat(errors.fileno()).st_size) <= MAX_LOG,
                        "A completed measured stage exceeded its bounded diagnostic output.")
    except BaseException as error:
        caught = error
    interruption = drain_pending_cleanup(caught)
    if caught is None and interruption is not None:
        caught = interruption
    # The exact family and retained file handles are quiet before copying logs.
    try:
        stdout = bounded_log(stdout_path, report / (name + "-stdout.log"))
        stderr = bounded_log(stderr_path, report / (name + "-stderr.log"))
        require(not stdout["truncated"] and not stderr["truncated"],
                "Repeated measured invocations exceeded their aggregate log allowance.")
        if caught is None:
            require(not stdout.get("missing") and not stderr.get("missing"),
                    "An actual measured invocation lost its captured diagnostic files.")
    except BaseException as log_error:
        stdout = locals().get("stdout", {"file": name + "-stdout.log", "capture": "FAILED"})
        stderr = locals().get("stderr", {"file": name + "-stderr.log", "capture": "FAILED"})
        observed["log_error"] = failure(log_error)
        if caught is None:
            caught = log_error
    elapsed = time.monotonic() - started
    invocations.append({"ordinal": ordinal, "returncode": code, "elapsed_seconds": elapsed,
        "stdout_bytes": stdout.get("observed_bytes"), "stderr_bytes": stderr.get("observed_bytes")})
    observed.update(elapsed_seconds=sum(row["elapsed_seconds"] for row in invocations),
        returncode=code, status="RETURNED" if caught is None else "FAILED",
        error=None if caught is None else failure(caught), stdout=stdout, stderr=stderr)
    try:
        require(identity(head) == before, "A measured stage changed committed/source/installed identities.")
        observed["identity_after"] = "EXACT"
    except BaseException as identity_error:
        observed["identity_after"] = "FAILED"
        observed["identity_error"] = failure(identity_error)
        observed["status"] = "FAILED"
        if caught is None:
            caught = identity_error
    save(result_path, observed)
    print(json.dumps({"stage": name, "invocation": ordinal, "status": observed["status"],
        "returncode": code, "elapsed_seconds": elapsed}), flush=True)
    if caught is not None:
        raise caught
    return observed


def allowed_artifacts():
    return {"scale-measurement.json", "identity-before.json", "identity-after.json",
            "application-admission.json", *EXPORTS.values(),
            *(name + "-" + suffix for name in STAGES for suffix in
              ("request.json", "result.json", "stdout.log", "stderr.log"))}


def collect(fixture, report):
    """Copy only fixed bounded receipt names; never copy a corpus or archive."""
    copied = {}
    if fixture is not None:
        for relative, target in EXPORTS.items():
            source = fixture / relative
            if present(source):
                from carr_library.durable_state import read_json_snapshot
                value, checksum = read_json_snapshot(source, max_bytes=MAX_JSON)
                require(type(value) is dict, "An exported measurement receipt must be an object.")
                raw = read_bytes(source)
                require(sha256(raw).hexdigest() == checksum, "The completed receipt changed during export.")
                destination = report / target
                if present(destination):
                    require(read_bytes(destination) == raw, "An exported receipt changed unexpectedly.")
                else:
                    with destination.open("xb") as stream:
                        stream.write(raw)
                copied[relative] = {"file": target, "sha256": checksum, "bytes": len(raw)}
    total, files = 0, []
    from carr_library.durable_state import regular
    for path in sorted(report.iterdir()):
        require(path.name in allowed_artifacts(), "Unexpected measurement artifact entry.")
        info = regular(path)
        maximum = MAX_LOG if path.suffix == ".log" else (
            MAX_STAGE_JSON if path.name.endswith(("-request.json", "-result.json")) else MAX_JSON)
        require(info.st_size <= maximum, "A measurement artifact exceeds its per-file cap.")
        total += info.st_size
        files.append(path.name)
    # Reserve room for the final summary rewrite even on a measured failure.
    require(len(files) <= 64 and total <= MAX_ARTIFACT_BYTES-MAX_JSON,
            "The complete measurement evidence exceeds its fixed artifact allowance.")
    return {"receipts": copied, "files": files, "total_bytes_before_final_summary": total}

def measured(args):
    require(os.name == "nt", "This receiving driver admits only the requested Windows measurements.")
    require(type(args.seconds) in (int, float) and math.isfinite(args.seconds)
            and 60 <= args.seconds <= 10500, "Choose an explicit bounded measured-profile duration.")
    from carr_library.durable_state import local_path, regular
    artifact_root = local_path(ROOT / "ci-scale-results", exists=True)
    report_parent = local_path(args.report_dir.absolute().parent, root=artifact_root, exists=True)
    regular(report_parent, directory=True)
    report = report_parent / args.report_dir.name
    report.mkdir()  # Never adopt an earlier receiving result.
    before, fixture, work, caught = None, None, None, None
    started = time.monotonic()
    deadline = started + args.seconds
    books = 1 if args.profile == "2gib" else int(args.profile)
    authored_fields = 1000 if args.profile == "2gib" else books*40
    original_bytes = 2*1024**3 if args.profile == "2gib" else books*(40*256+39)+3
    result = {
        "schema": SCHEMA, "status": "RUNNING", "commit": args.head, "profile": args.profile,
        "contracts": "SEPARATE_REQUIRED_WORKFLOW_JOBS", "python_files": None,
        "payload_files": None, "stages": [], "cancellation": "NOT_RUN",
        "requested": {"books": books, "authored_fields": authored_fields, "original_bytes": original_bytes},
        "resource_policy": {"import_memory_mib": 1024, "backup_memory_mib": 2048,
            "free_reserve_mib": 1024, "query_disk_headroom_mib": 2048,
            "source_slice_seconds": 600, "query_call_seconds": 60,
            "profile_seconds": args.seconds, "maximum_resume_invocations": MAX_RESUME_CALLS,
            "os_memory_enforcement": "NOT_MEASURED"},
        "performance_acceptance": "NOT_ESTABLISHED", "simultaneous_family_peak": "NOT_MEASURED",
        "NVDA": "NOT_RUN", "private_models": "NOT_RUN", "physical_audio": "NOT_RUN",
        "failure": None,
    }
    save(report / "scale-measurement.json", result)

    def step(name, script, arguments, maximum):
        remaining = deadline - time.monotonic() - 90
        if remaining < .1:
            raise TimeoutError("No measured-profile allowance remains for the next actual stage.")
        try:
            observed = run_stage(name, script, arguments, report=report, work=work,
                head=args.head, before=before, timeout=min(maximum, remaining))
        finally:
            path = report / (name + "-result.json")
            if present(path):
                observed = read_json(path, maximum=MAX_STAGE_JSON)
                result["stages"] = [row for row in result["stages"] if row["name"] != name] + [observed]
                save(report / "scale-measurement.json", result)
        return observed["returncode"]

    def stage_receipt(relative, expected_status):
        value = read_json(fixture / relative)
        require(type(value) is dict and value.get("status") == expected_status,
                "The actual stage did not emit " + expected_status)
        return value

    def perform():
        source_arguments = (["--large-bytes", str(2*1024**3), "--segments", "1000"]
            if args.profile == "2gib" else
            ["--books", args.profile, "--segments", "40", "--segment-bytes", "256"])
        code = step("01-generate", SCRIPTS[0], ["--output-dir", str(fixture), *source_arguments], 900)
        if code == 2:
            stage_receipt("generation.json", "NOT_RUN")
            result.update(status="NOT_RUN", refusal_stage="generator host admission")
            return 3
        require(code == 0, "The actual source generator failed.")
        generated = stage_receipt("generation.json", "FIXTURE_GENERATED_NOT_BENCHMARKED")
        require(generated["completed_books"] == books and generated["generated_bytes"] == original_bytes
                and generated["segments"] == authored_fields,
                "The generated original population differs from the requested measured profile.")
        code = step("02-initialize", SCRIPTS[1], ["init", str(fixture),
            "--memory-mib", "1024", "--reserve-mib", "1024", "--search-disk-mib", "2048",
            "--page-files", "1000", "--steps", "1001", "--slice-seconds", "600",
            "--foreign-libraries", "21"], 900)
        admission = read_json(report / "02-initialize-stdout.log")
        require(type(admission) is dict and admission.get("schema") == "carr-library-catalog-benchmark/2",
                "The actual public initializer did not return its bounded receipt.")
        save(report / "application-admission.json", admission)
        if code == 3:
            require(admission["status"] == "NOT_RUN" and admission["admission"]["status"] == "NOT_RUN",
                    "An application refusal lacks actual host admission evidence.")
            result.update(status="NOT_RUN", refusal_stage="application host admission")
            return 3
        require(code == 0 and read_json(fixture / "benchmark/state.json") == admission,
                "The actual public import initialization failed or changed its saved state.")
        code = step("03-cancel", SCRIPTS[1], ["cancel", str(fixture), "--seconds", "900",
                                              "--cancel-after-worker-seconds", "0.5"], 960)
        require(code in (0, 3), "The actual cancellation stage failed.")
        cancellation = read_json(fixture / "benchmark/cancellation.json")
        if code == 0:
            require(cancellation["status"] == "OWNED_WORKER_CANCELLATION_MEASURED"
                    and cancellation["native_cancelled"] is True
                    and cancellation["child_start_observed"] is True
                    and cancellation["same_saved_identities"] is True,
                    "The cancellation stage lacks its actual owned-worker evidence.")
            result["cancellation"] = "MEASURED"
        else:
            require(cancellation["status"] == "NOT_RUN", "Unqualified cancellation lost its actual receipt.")
        code = step("04-postcommit-exit", SCRIPTS[1],
            ["import", str(fixture), "--seconds", "7200", "--crash-after-first-commit"], 7260)
        require(code == 73, "The requested postcommit process death was not observed.")
        crash = read_json(fixture / "benchmark/authored-crash.json")
        require(crash["boundary"] == "durable batch save after actual owned worker return"
                and type(crash["pid"]) is int and crash["pid"] > 0 and crash["committed"] >= 1
                and any(row.get("outcome") == "returned" and row.get("returncode") == 0
                        and type(row.get("pid")) is int and row["pid"] > 0 for row in crash["workers"]),
                "The authored process death did not preserve its real worker/commit boundary.")
        # Each existing public batch invocation receives its own exact native
        # owner. A single 50k-worker outer Job would exceed the unchanged 4096
        # lifetime inventory even when its children ran sequentially.
        for invocation in range(MAX_RESUME_CALLS):
            code = step("05-resume", SCRIPTS[1],
                ["import", str(fixture), "--seconds", "7200", "--stop-after-slices", "1"], 7260)
            require(code == 0, "The actual import resume failed.")
            state = read_json(fixture / "benchmark/state.json")
            if state["import_complete"] is True and state["scan_complete"] is True:
                break
        else:
            result.update(status="NOT_RUN", refusal_stage="declared measured resume inventory",
                refusal_reason="The selected original population needs more saved public slices than this run admits.")
            return 3
        code = step("06-verify", SCRIPTS[1], ["verify", str(fixture), "--seconds", "7200"], 7260)
        require(code == 0, "Independent complete-catalog verification failed.")
        verified = stage_receipt("benchmark/catalog-verification.json", "CATALOG_VERIFIED")
        require(verified["source_books"] == books and verified["authored_source_fields"] == authored_fields
                and verified["implementation_unchanged"] is True,
                "Independent catalog verification omitted original sources.")
        code = step("07-search", SCRIPTS[1], ["search", str(fixture), "--seconds", "7200",
                                            "--query-seconds", "60", "--query-samples", "20"], 7260)
        require(code == 0, "The current bounded search measurement failed.")
        searched = stage_receipt("benchmark/search-measurements.json", "MEASURED_SYNTHETIC_CORPUS")
        require(searched["source_books"] == books and searched["authored_source_fields"] == authored_fields
                and searched["implementation_unchanged"] is True,
                "Search measurement omitted original sources or changed implementation.")
        code = step("08-backup", SCRIPTS[2], ["backup", str(fixture), "--seconds", "3600",
            "--memory-mib", "2048", "--reserve-mib", "1024"], 3660)
        if code == 3:
            stage_receipt("benchmark/backup-restore.json", "NOT_RUN")
            result.update(status="NOT_RUN", refusal_stage="backup caller/host admission")
            return 3
        require(code == 0, "Actual native5 backup, verify or fresh passive restore failed.")
        backup = stage_receipt("benchmark/backup-restore.json", "BACKUP_RESTORE_VERIFIED")
        require(backup["native_backup_format"] == 5 and backup["automatic_resume"] is False
                and backup["source_books"] == books and all(backup[name] is True for name in
                    ("source_inventory_equal", "archive_inventory_equal", "catalog_equal", "raw_equal",
                     "external_originals_equal", "implementation_unchanged")),
                "The actual backup measurement changed its format, originals or passive behavior.")
        result["status"] = ("MEASURED_PROFILE_COMPLETE" if result["cancellation"] == "MEASURED"
                            else "MEASURED_WITH_CANCEL_NOT_RUN")
        return 0 if result["cancellation"] == "MEASURED" else 4

    code = 1
    try:
        before = identity(args.head)
        result.update(python_files=before["python_files"], payload_files=before["payload_files"],
                      identity_before_sha256=sha256(canonical(before)).hexdigest())
        save(report / "identity-before.json", before)
        temporary = local_path(os.environ["RUNNER_TEMP"], exists=True)
        work_parent = local_path(args.work_root.absolute().parent, root=temporary, exists=True)
        regular(work_parent, directory=True)
        work = work_parent / args.work_root.name
        require(args.work_root.is_absolute() and not work.is_relative_to(ROOT.parent)
                and not work.is_relative_to(report) and not report.is_relative_to(work),
                "Keep synthetic corpora in the private runner directory, outside source and artifacts.")
        work.mkdir()
        fixture = work / "fixture"
        result["fixture_path"] = str(fixture)
        save(report / "scale-measurement.json", result)
        code = offline(perform, args.head)
    except BaseException as error:
        caught = error
        result.update(status="FAILED", failure=failure(error, trace=True))
    finally:
        from carr_library.audio.cleanup import drain_pending_cleanup
        try:
            interruption = drain_pending_cleanup(caught)
            if interruption is not None:
                result.update(status="FAILED", deferred_interruption=failure(interruption))
        except BaseException as cleanup_error:
            result.update(status="FAILED", cleanup_error=failure(cleanup_error))
        try:
            after = identity(args.head)
            save(report / "identity-after.json", after)
            require(before is not None and after == before,
                    "The complete source/test/helper/installed identity changed or lacked its initial proof.")
            result["identity_after_sha256"] = sha256(canonical(after)).hexdigest()
        except BaseException as evidence_error:
            result.update(status="FAILED", identity_error=failure(evidence_error))
        try:
            result["evidence"] = collect(fixture, report)
        except BaseException as evidence_error:
            result.update(status="FAILED", evidence_error=failure(evidence_error))
        result["elapsed_seconds"] = time.monotonic() - started
        save(report / "scale-measurement.json", result)
        print(json.dumps({"status": result["status"], "profile": args.profile,
            "commit": args.head, "elapsed_seconds": result["elapsed_seconds"],
            "performance_acceptance": "NOT_ESTABLISHED"}), flush=True)
    # Final identity/evidence/cleanup failures override any earlier operation
    # result. In particular, a return inside a try/finally cannot report success.
    expected = {"MEASURED_PROFILE_COMPLETE": 0, "NOT_RUN": 3, "MEASURED_WITH_CANCEL_NOT_RUN": 4}
    return code if result["status"] in expected and code == expected[result["status"]] else 1

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head")
    parser.add_argument("--profile", choices=("1000", "10000", "50000", "2gib"))
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--seconds", type=float, default=9600)
    parser.add_argument("--stage-task", type=Path)
    parser.add_argument("--stage-sha256")
    args = parser.parse_args(argv)
    if args.stage_task is not None:
        require(args.stage_sha256 is not None and all(value is None for value in
                (args.head, args.profile, args.work_root, args.report_dir)),
                "A stage request cannot override its captured head/profile.")
        return child(args.stage_task, args.stage_sha256)
    require(all(value is not None for value in (args.head, args.profile, args.work_root, args.report_dir))
            and args.stage_sha256 is None, "Choose the explicit measured head, profile and fresh paths.")
    return measured(args)


if __name__ == "__main__":
    raise SystemExit(main())
