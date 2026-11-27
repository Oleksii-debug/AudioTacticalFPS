"""Finite, model-free Windows experiment for the isolated by-name prototype.

No production source is changed or patched. The two explicit attribute policies
are measured separately. Each status child performs50 real atomic replacements,
with no retry. The held-open child publishes both writes before observation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import time

from nt_metadata import NativeMetadata, abi_layout, nt_path


_DEADLINE = float("inf")


class IncompleteDiagnostic(TimeoutError):
    pass


def time_left(maximum=8):
    remaining = _DEADLINE - time.monotonic()
    if remaining <= 0:
        raise IncompleteDiagnostic("INCOMPLETE: overall360-second diagnostic budget exhausted")
    return min(maximum, remaining)


CHILD = r'''
import json, os, pathlib, sys, time
kind, root, control = sys.argv[1:]
root, control = pathlib.Path(root), pathlib.Path(control)
def wait(name):
    deadline = time.monotonic() + 8
    while not (control / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(name)
        time.sleep(.002)
if kind == 'status':
    target, pending = root / 'job_status.json', root / 'status.pending'
    target.write_bytes(b'0')
    (control / 'ready').write_text('ready', encoding='utf8')
    wait('release')
    for index in range(1, 51):
        pending.write_bytes(str(index).encode('ascii'))
        os.replace(pending, target)
        time.sleep(.001)
    print(json.dumps({'replacements': 50}), flush=True)
elif kind == 'growth':
    with (root / 'дані_𐐀😀.bin').open('wb', buffering=0) as handle:
        handle.write(b'a')
        (control / 'ready').write_text('ready', encoding='utf8')
        wait('grow')
        handle.write(b'b' * (2 * 1024 * 1024))
        # FileIO(buffering=0) write completed; deliberately no fsync or close.
        (control / 'grown').write_text('grown', encoding='utf8')
        wait('release')
    print(json.dumps({'bytes': 2097153}), flush=True)
else:
    raise ValueError(kind)
'''


def error_record(error):
    result = {"type": type(error).__name__, "message": str(error)}
    for name in ("errno", "winerror", "ntstatus", "io_status", "information",
                 "nt_path", "reparse_tag"):
        if hasattr(error, name):
            result[name] = getattr(error, name)
    return result


def wait_control(path, child, *, timeout=8):
    deadline = time.monotonic() + time_left(timeout)
    while not path.exists():
        if child.poll() is not None:
            out, err = child.communicate()
            raise RuntimeError(f"Child exited {child.returncode}: {out!r} {err!r}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Waiting for {path.name}")
        time.sleep(.002)


def finish_child(child):
    """Bound all child cleanup. These controlled children spawn no descendants."""
    if child.poll() is None:
        child.terminate()
    try:
        return child.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.communicate(timeout=3)


def spawn(kind, root, control):
    time_left()
    root.mkdir()
    control.mkdir()
    return subprocess.Popen([sys.executable, "-I", "-S", "-B", "-X", "utf8",
                             "-c", CHILD, kind, str(root), str(control)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf8",
                            errors="replace")


def growth_case(parent, observe):
    root, control = parent / "growth", parent / "growth-control"
    child = spawn("growth", root, control)
    item = {"pid": child.pid}
    try:
        target = root / "дані_𐐀😀.bin"
        wait_control(control / "ready", child)
        item["before"] = asdict(observe(target))
        (control / "grow").write_text("grow", encoding="utf8")
        wait_control(control / "grown", child)
        assert child.poll() is None, "Writer must still hold the file open"
        item["direct_while_open"] = asdict(observe(target))
        with os.scandir(root) as entries:
            paths = [Path(entry.path) for entry in entries]
        assert paths == [target], paths
        item["directory_while_open"] = asdict(observe(paths[0]))
        item["extended_while_open"] = asdict(observe("\\\\?\\" + str(target)))
        assert item["before"]["st_size"] == 1, item
        for name in ("direct_while_open", "directory_while_open", "extended_while_open"):
            assert item[name]["st_size"] == 2097153, item
        (control / "release").write_text("release", encoding="utf8")
        out, err = child.communicate(timeout=8)
        item.update(returncode=child.returncode, stdout=out, stderr=err)
        assert child.returncode == 0, item
        item["final_lstat"] = target.lstat().st_size
        assert item["final_lstat"] == 2097153, item
        item["passed"] = True
    except Exception as error:
        item.update(passed=False, error=error_record(error))
    finally:
        if child.poll() is None:
            item["cleanup_output"] = finish_child(child)
        item["final_child_returncode"] = child.returncode
    return item


def status_case(parent, observe, *, directory, index):
    label = f"{'directory' if directory else 'direct'}-{index:02}"
    root, control = parent / label, parent / (label + "-control")
    child = spawn("status", root, control)
    item = {"pid": child.pid, "directory": directory, "index": index}
    information, io_status = Counter(), Counter()
    queries = 0
    started = time.monotonic()

    def measured(path):
        nonlocal queries
        value = observe(path)
        queries += 1
        information[str(value.information)] += 1
        io_status[f"0x{value.io_status:08x}"] += 1
        return value

    try:
        target = root / "job_status.json"
        wait_control(control / "ready", child)
        item["initial"] = asdict(measured(target))
        (control / "release").write_text("release", encoding="utf8")
        deadline = time.monotonic() + 8
        while child.poll() is None:
            time_left()
            if time.monotonic() >= deadline:
                raise TimeoutError("Status publisher did not exit")
            if directory:
                assert stat.S_ISDIR(measured(root).st_mode)
                with os.scandir(root) as entries:
                    paths = [Path(entry.path) for entry in entries]
            else:
                paths = [target]
            for path in paths:
                try:
                    value = measured(path)
                except FileNotFoundError:
                    continue  # A renamed temporary name may disappear normally.
                assert stat.S_ISREG(value.st_mode), value
            time.sleep(.0005)
        out, err = child.communicate(timeout=3)
        item.update(returncode=child.returncode, stdout=out, stderr=err)
        assert child.returncode == 0, item
        assert json.loads(out)["replacements"] == 50, item
        item["final_contents"] = target.read_text(encoding="ascii")
        assert item["final_contents"] == "50", item
        item["final_lstat"] = target.lstat().st_size
        item["passed"] = True
    except Exception as error:
        item.update(passed=False, error=error_record(error))
    finally:
        if child.poll() is None:
            item["cleanup_output"] = finish_child(child)
        item.update(final_child_returncode=child.returncode, queries=queries,
                    information=dict(information), io_status=dict(io_status),
                    elapsed_seconds=time.monotonic() - started)
    return item


def reparse_cases(parent, observe):
    target = parent / "reparse-target"
    target.mkdir()
    leaf = target / "source.txt"
    leaf.write_bytes(b"reference")
    records = []
    for kind in ("leaf_symlink", "parent_junction"):
        link = parent / kind
        item = {"kind": kind}
        created = False
        try:
            time_left()
            if kind == "leaf_symlink":
                os.symlink(leaf, link)
                path = link
            else:
                output = subprocess.run(["cmd", "/d", "/c", "mklink", "/J",
                                         str(link), str(target)],
                                        stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True,
                                        encoding="utf8", errors="replace", timeout=time_left())
                item["setup"] = {"returncode": output.returncode,
                                 "stdout": output.stdout, "stderr": output.stderr}
                if output.returncode:
                    raise RuntimeError("Junction setup failed")
                path = link / "source.txt"
            created = True
            item["fixture_attributes"] = link.lstat().st_file_attributes
            assert item["fixture_attributes"] & 0x400, item
            try:
                item["observation"] = asdict(observe(path))
                item["rejected"] = False
            except OSError as error:
                item["rejected"] = True
                item["query_error"] = error_record(error)
            item["qualified"] = True
        except Exception as error:
            item.update(qualified=False, setup_error=error_record(error))
        finally:
            if created:
                if kind == "parent_junction":
                    os.rmdir(link)
                else:
                    link.unlink()
        records.append(item)
    return records


def unc_case(target, *, dont_reparse):
    drive, suffix = os.path.splitdrive(str(target))
    path = "\\\\localhost\\" + drive[0] + "$" + suffix
    command = [sys.executable, "-I", "-B", "-X", "utf8",
               str(Path(__file__).with_name("nt_metadata.py"))]
    if not dont_reparse:
        command.append("--allow-reparse-lookup")
    command.append(path)
    item = {"path": path, "nt_path": nt_path(path), "dont_reparse": dont_reparse,
            "scope": "localhost administrative share; no remote host"}
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf8", errors="replace", timeout=time_left())
        item.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        item["qualified"] = result.returncode == 0
    except subprocess.TimeoutExpired as error:
        item.update(qualified=False, error=error_record(error))
    return item


def lane(root, *, dont_reparse, repetitions):
    parent = root / ("strict" if dont_reparse else "explicit-false")
    parent.mkdir()
    item = {"dont_reparse": dont_reparse, "production_adoption": False}
    try:
        time_left()
        observe = NativeMetadata(dont_reparse=dont_reparse)
        target = parent / "звіт_𐐀😀.json"
        target.write_bytes(b"native")
        item["normal"] = asdict(observe(target))
        item["extended"] = asdict(observe("\\\\?\\" + str(target)))
        item["directory"] = asdict(observe(parent))
        assert item["normal"]["st_size"] == item["extended"]["st_size"] == 6
        assert stat.S_ISDIR(item["directory"]["st_mode"])
        try:
            observe(parent / "missing.json")
        except FileNotFoundError as error:
            item["missing"] = error_record(error)
        else:
            raise AssertionError("Missing path must map to FileNotFoundError")
        item["functional_passed"] = True
        item["unc"] = unc_case(target, dont_reparse=dont_reparse)
        item["reparse"] = reparse_cases(parent, observe)
        item["growth"] = growth_case(parent, observe)
        item["stress"] = []
        for directory in (False, True):
            for index in range(repetitions):
                time_left()
                item["stress"].append(status_case(parent, observe,
                                                  directory=directory, index=index))
        item["queries_passed"] = (item["growth"]["passed"]
                                  and all(case["passed"] for case in item["stress"]))
        item["reparse_qualified"] = all(case.get("qualified") and case.get("rejected")
                                          for case in item["reparse"])
        item["passed"] = item["queries_passed"] and (item["reparse_qualified"]
                                                      if dont_reparse else True)
    except Exception as error:
        item.update(passed=False, error=error_record(error))
    return item


def main():
    global _DEADLINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("repetitions must be1..20")
    report = {"schema": "carr.windows-by-name-diagnostic.v1",
              "base_head": "8fa68b9d9041dd2f4f501f4084a5c9c50998c6f9",
              "production_modified": False, "neural_inference": "NOT_RUN",
              "platform": platform.platform(), "python": sys.version,
              "abi": abi_layout(), "repetitions_per_policy_and_watcher": args.repetitions,
              "source_hashes": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ("nt_metadata.py", "probe_windows.py")}}
    started = time.monotonic()
    _DEADLINE = started + 360
    try:
        if os.name != "nt":
            raise RuntimeError("This is an actual-Windows diagnostic")
        with tempfile.TemporaryDirectory(prefix="carr-by-name-") as folder:
            root = Path(folder)
            report["lanes"] = [lane(root, dont_reparse=value, repetitions=args.repetitions)
                               for value in (True, False)]
        report["passed"] = all(item["passed"] for item in report["lanes"])
    except Exception as error:
        report.update(passed=False, error=error_record(error))
    report["elapsed_seconds"] = time.monotonic() - started
    report["incomplete"] = report["elapsed_seconds"] >= 360
    if report["incomplete"]:
        report["passed"] = False
    report["status"] = ("INCOMPLETE" if report["incomplete"] else
                        "PASS" if report["passed"] else "FAIL")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
