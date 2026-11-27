"""Isolated Windows rename controls and the existing bounded publisher.

The four original controls never retry. The fifth calls public atomic_bytes.
This diagnostic does not modify production code or intercept a worker API.
Every trial starts a fresh real child and observes only synthetic files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time

BASE = "8fa68b9d9041dd2f4f501f4084a5c9c50998c6f9"
PROCESS_SHA256 = "d9c5be71768b235831a45ff88e7aa1f37d660ef9b2c649ee3be7d4d534465fba"
DURABLE_SHA256 = "22a1ad62bd8a69eaeea957061b0b2fe42e5e8fe4c887901089bf9ed70ba36493"
MODES = ("poll_only", "scandir_only", "attributes_only", "run_process", "run_process_atomic")
CHILD = r"""
import json, os, sys, time
from pathlib import Path
root, release, report = map(Path, sys.argv[1:4])
generations = int(sys.argv[4])
status, pending = root / 'job_status.json', root / 'status.pending'
result = dict(pid=os.getpid(), started_ns=time.monotonic_ns(), writes=[], error=None)
phase, generation = 'initial_write', None
exit_code = 0
try:
    status.write_bytes(b'initial')
    print('out_time_us=1000', flush=True)
    phase = 'wait_for_parent'
    deadline = time.monotonic() + 4
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('Parent did not release the ready child.')
        time.sleep(.005)
    for generation in range(generations):
        phase = 'closed_pending_write'
        pending.write_bytes(('generation-' + str(generation)).encode('ascii'))
        phase = 'one_shot_replace'
        started = time.monotonic_ns()
        try:
            os.replace(pending, status)
        except Exception as error:
            result['writes'].append(dict(generation=generation, begin_ns=started,
                end_ns=time.monotonic_ns(), status='FAIL'))
            raise
        result['writes'].append(dict(generation=generation, begin_ns=started,
            end_ns=time.monotonic_ns(), status='PASS'))
        time.sleep(.01)
except Exception as error:
    exit_code = 1
    result['error'] = dict(type=type(error).__name__, message=str(error),
        winerror=getattr(error, 'winerror', None), errno=getattr(error, 'errno', None),
        phase=phase, generation=generation, observed_ns=time.monotonic_ns())
    print(json.dumps(result['error']), file=sys.stderr, flush=True)
finally:
    result['finished_ns'] = time.monotonic_ns()
    result['exit_code'] = exit_code
    report.write_text(json.dumps(result), encoding='utf-8')
raise SystemExit(exit_code)
"""



def atomic_child():
    """Use the existing public publisher only in the additional fifth mode."""
    setup = """
import hashlib
sys.path.insert(0, sys.argv[5])
from carr_library import durable_state
publisher_origin = Path(durable_state.__file__).resolve()
expected_origin = Path(sys.argv[5]).resolve() / 'carr_library' / 'durable_state.py'
publisher_hash = hashlib.sha256(publisher_origin.read_bytes()).hexdigest()
assert publisher_origin == expected_origin
assert publisher_hash == %r
""" % DURABLE_SHA256
    code = CHILD.replace("from pathlib import Path\n", "from pathlib import Path\n" + setup, 1)
    marker = "phase, generation = 'initial_write', None"
    code = code.replace(marker,
        "result['publisher'] = dict(name='carr_library.durable_state.atomic_bytes', "
        "origin=str(publisher_origin), sha256=publisher_hash)\n" + marker, 1)
    old = """        phase = 'closed_pending_write'
        pending.write_bytes(('generation-' + str(generation)).encode('ascii'))
        phase = 'one_shot_replace'
        started = time.monotonic_ns()
        try:
            os.replace(pending, status)"""
    new = """        phase = 'public_atomic_bytes'
        started = time.monotonic_ns()
        try:
            durable_state.atomic_bytes(status,
                ('generation-' + str(generation)).encode('ascii'), replace=True)"""
    if code.count(old) != 1:
        raise RuntimeError("The unchanged one-shot control preimage was not found.")
    return code.replace(old, new, 1)

def error_info(error):
    return dict(type=type(error).__name__, message=str(error)[:4000],
                winerror=getattr(error, "winerror", None),
                errno=getattr(error, "errno", None), observed_ns=time.monotonic_ns())


class Trace:
    def __init__(self, stage):
        self.stage = stage
        self.events = []
        self.dropped = 0

    def call(self, operation, path, function):
        event = dict(operation=operation, path=str(path.relative_to(self.stage)),
                     begin_ns=time.monotonic_ns())
        try:
            value = function()
            event["status"] = "PASS"
            if hasattr(value, "st_size"):
                event["size"] = value.st_size
                event["attributes"] = getattr(value, "st_file_attributes", None)
            elif isinstance(value, list):
                event["names"] = value[:8]
            return value
        except FileNotFoundError as error:
            event.update(status="MISSING", error=error_info(error))
            raise
        except Exception as error:
            event.update(status="FAIL", error=error_info(error))
            raise
        finally:
            event["end_ns"] = time.monotonic_ns()
            if len(self.events) < 2048:
                self.events.append(event)
            else:
                self.dropped += 1


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


def enumerate_names(stage):
    # The search handle closes before this function returns.
    with os.scandir(stage) as entries:
        return [entry.name for entry in entries]


def control(mode, command, stage, release, trace, query, timeout):
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ready = threading.Event()
    chunks = {"stdout": [], "stderr": []}
    reader_errors = []

    def drain(pipe, name):
        count = 0
        try:
            for line in iter(pipe.readline, b""):
                if name == "stdout" and line.startswith(b"out_time_us="):
                    ready.set()
                count += len(line)
                if count <= 65536:
                    chunks[name].append(line)
                elif not reader_errors:
                    reader_errors.append(dict(type="DiagnosticOutputOverflow"))
        finally:
            pipe.close()

    threads = [threading.Thread(target=drain, args=(getattr(child, name), name),
                                daemon=True) for name in chunks]
    for thread in threads:
        thread.start()
    errors, released = [], False
    deadline = time.monotonic() + timeout
    try:
        while child.poll() is None:
            if time.monotonic() > deadline:
                raise TimeoutError("Diagnostic trial deadline exceeded.")
            if ready.is_set():
                if mode == "scandir_only":
                    try:
                        trace.call("scandir", stage, lambda: enumerate_names(stage))
                    except Exception as error:
                        errors.append(error_info(error))
                elif mode == "attributes_only":
                    # Query BOTH rename source and destination; no enumeration.
                    for path in (stage / "status.pending", stage / "job_status.json"):
                        try:
                            trace.call("GetFileAttributesExW", path, lambda p=path: query(p))
                        except FileNotFoundError:
                            pass  # A moved temporary path legitimately disappears.
                        except Exception as error:
                            errors.append(error_info(error))
                if not released:
                    release.write_bytes(b"Continue the real synthetic worker.")
                    released = True
            try:
                child.wait(timeout=.005)
            except subprocess.TimeoutExpired:
                pass
    except Exception as error:
        errors.append(error_info(error))
    finally:
        stop(child)
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            errors.append(dict(type="DiagnosticPipeDidNotClose"))
    return dict(returncode=child.returncode, pid=child.pid, released=released,
                errors=errors + reader_errors,
                **{name: b"".join(data).decode("utf-8", errors="replace")
                   for name, data in chunks.items()})


def actual_runner(command, stage, release, trace, runner, timeout):
    children, errors, notices = [], [], []
    original_query, original_spawn = runner._live_file_info, runner.subprocess.Popen

    def observed_query(path):
        return trace.call("GetFileAttributesExW", path, lambda: original_query(path))

    def observed_spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    def progress(value):
        notices.append(value)
        release.write_bytes(b"Continue the real synthetic worker.")

    runner._live_file_info = observed_query
    runner.subprocess.Popen = observed_spawn
    output = dict(stdout="", stderr="")
    try:
        result = runner.run_process(command, watch=(stage,), progress=progress,
                                    budget=runner.AudioBudget(timeout=timeout))
        output = dict(stdout=result.stdout.decode("utf-8", errors="replace"),
                      stderr=result.stderr.decode("utf-8", errors="replace"))
    except Exception as error:
        errors.append(error_info(error))
    finally:
        runner._live_file_info, runner.subprocess.Popen = original_query, original_spawn
        for child in children:
            stop(child)
    if len(children) != 1:
        errors.append(dict(type="UnexpectedActualChildCount", count=len(children)))
    return dict(returncode=children[0].returncode if children else None,
                pid=children[0].pid if children else None, notices=notices,
                errors=errors, **output)


def trial(mode, index, generations, runner, output, project):
    temporary = tempfile.TemporaryDirectory(prefix="carr-status-diagnostic-")
    root = Path(temporary.name)
    stage, release, child_report = root / "worker", root / "release", root / "child.json"
    stage.mkdir()
    trace = Trace(stage)
    child_code = atomic_child() if mode == "run_process_atomic" else CHILD
    command = [str(Path(sys.executable).resolve()), "-I", "-B", "-c", child_code,
               str(stage), str(release), str(child_report), str(generations)]
    if mode == "run_process_atomic":
        command.append(str(project / "src"))
    result = dict(mode=mode, iteration=index, started_ns=time.monotonic_ns(),
                  child=None, parent=None, final_payload=None, errors=[])
    try:
        if mode in ("run_process", "run_process_atomic"):
            result["parent"] = actual_runner(command, stage, release, trace, runner, 8)
        else:
            result["parent"] = control(mode, command, stage, release, trace,
                                       runner._live_file_info, 8)
        # These are the first parent payload reads, after the real child is reaped.
        result["child"] = json.loads(child_report.read_text(encoding="utf-8"))
        result["final_payload"] = (stage / "job_status.json").read_text(encoding="ascii")
    except Exception as error:
        result["errors"].append(error_info(error))
    finally:
        result["finished_ns"] = time.monotonic_ns()
        result["queries"], result["queries_dropped"] = trace.events, trace.dropped
        try:
            temporary.cleanup()
        except Exception as error:
            result["errors"].append(dict(phase="cleanup", **error_info(error)))
    child, parent = result["child"] or {}, result["parent"] or {}
    writes = child.get("writes", [])
    ok = (not result["errors"] and not parent.get("errors") and
          parent.get("returncode") == child.get("exit_code") == 0 and
          not child.get("error") and len(writes) == generations and
          all(row["status"] == "PASS" for row in writes) and
          result["final_payload"] == "generation-" + str(generations - 1) and
          result["queries_dropped"] == 0)
    result["status"] = "PASS" if ok else "FAIL"
    name = f"trial-{index:02d}-{mode}.json"
    (output / name).write_text(json.dumps(result, indent=2), encoding="utf-8")
    brief = dict(mode=mode, iteration=index, status=result["status"], report=name,
                 writes=len(writes), child_error=child.get("error"),
                 parent_errors=parent.get("errors", []), errors=result["errors"],
                 query_count=len(trace.events))
    print(json.dumps(brief), flush=True)
    return brief


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=12)
    parser.add_argument("--generations", type=int, default=50)
    parser.add_argument("--allow-non-windows-smoke", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.trials <= 20 or not 1 <= args.generations <= 50:
        parser.error("Bounds: 1–20 trials per mode and 1–50 generations per child.")
    if os.name != "nt" and not args.allow_non_windows_smoke:
        parser.error("Native collision evidence requires Windows.")
    project = args.source_root.resolve()
    source = project / "src" / "carr_library" / "audio" / "process.py"
    actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual_hash != PROCESS_SHA256:
        raise RuntimeError("Production process source differs from the exact 8fa baseline.")
    durable_source = project / "src" / "carr_library" / "durable_state.py"
    if hashlib.sha256(durable_source.read_bytes()).hexdigest() != DURABLE_SHA256:
        raise RuntimeError("Public publisher differs from the exact 8fa baseline.")
    sys.path.insert(0, str(project / "src"))
    from carr_library.audio import process as runner
    if Path(runner.__file__).resolve() != source:
        raise RuntimeError("Unexpected process module origin.")
    args.output.mkdir(parents=True, exist_ok=False)
    summary = dict(base_commit=BASE, diagnostic_commit=os.environ.get("GITHUB_SHA"),
                   python=sys.version, executable=sys.executable, platform=platform.platform(),
                   native_windows=os.name == "nt", production_source=str(source),
                   production_sha256=actual_hash, generations=args.generations,
                   trials_per_mode=args.trials, parent_control_poll_seconds=.005,
                   production_poll_seconds=.05, public_publisher_sha256=DURABLE_SHA256,
                   prior_control_run=45953624408, prior_control_commit="806e270b10b9b9410dc9b205e2e8a802dc230fca",
                   replacement_policy={mode: ("existing public atomic_bytes, native Windows 5/32/33, half-second bound"
                       if mode == "run_process_atomic" else "one-shot os.replace; zero retries") for mode in MODES},
                   interpretation="Diagnostic only; zero observed failures does not prove absence of races.",
                   trials=[])
    for index in range(args.trials):
        # Rotate order to avoid assigning one mode only to cold/warm filesystem state.
        order = MODES[index % len(MODES):] + MODES[:index % len(MODES)]
        for mode in order:
            summary["trials"].append(trial(mode, index, args.generations, runner, args.output, project))
    summary["by_mode"] = {
        mode: dict(passed=sum(row["status"] == "PASS" for row in summary["trials"] if row["mode"] == mode),
                   failed=sum(row["status"] == "FAIL" for row in summary["trials"] if row["mode"] == mode))
        for mode in MODES}
    summary["status"] = "PASS" if all(row["status"] == "PASS" for row in summary["trials"]) else "FAIL"
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("status", "native_windows", "by_mode")}), flush=True)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
