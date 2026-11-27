"""Require exact installed native receipts for the composed V3 candidate."""
from argparse import ArgumentParser
from collections import Counter
from contextlib import redirect_stdout
from hashlib import sha256
import importlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("kind")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "scripts/r7_native_oracles.json").read_text(encoding="utf-8"))[args.kind]
    evidence = dict(schema=1, status="FAIL", kind=args.kind, actual_windows=sys.platform == "win32",
                    isolated=bool(sys.flags.isolated), nvda="NOT_RUN", physical_audio="NOT_RUN")
    handle = None
    output = io.StringIO()
    try:
        if sys.platform != "win32" or not sys.flags.isolated:
            raise ValueError("An isolated installed native Windows interpreter is required.")
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        prefix = Path(sys.prefix).resolve(strict=True)

        def identities():
            records = {}
            for name in config["modules"]:
                path = Path(importlib.import_module(name).__file__).resolve(strict=True)
                expected = root / "src" / Path(*name.split(".")).with_suffix(".py")
                if not path.is_relative_to(prefix) or "site-packages" not in {p.casefold() for p in path.parts}:
                    raise ValueError("Native probe imported a source-tree module: " + name)
                digest = sha256(path.read_bytes()).hexdigest()
                if digest != sha256(expected.read_bytes()).hexdigest():
                    raise ValueError("Installed source differs from the candidate: " + name)
                records[name] = dict(path=str(path), sha256=digest)
            return records

        before = identities()
        probe = root / config["probe"]
        digest = sha256(probe.read_bytes()).hexdigest()
        timeout = config.get("subprocess_timeout_seconds")
        if timeout is not None:
            if type(timeout) is not int or not 1 <= timeout <= 300:
                raise ValueError("The finite native subprocess allowance is invalid.")
            try:
                process = subprocess.run(
                    [sys.executable, "-I", "-B", "-X", "utf8", str(probe),
                     *config.get("arguments", [])],
                    cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                    timeout=timeout, check=False)
                output.write(process.stdout)
                status = process.returncode
            except subprocess.TimeoutExpired as error:
                raw = error.stdout or ""
                output.write(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
                raise
            finally:
                print(output.getvalue(), end="", flush=True)
        else:
            namespace = runpy.run_path(str(probe), run_name="carr_composed_native_probe")
            original_argv = sys.argv
            sys.argv = [str(probe), *config.get("arguments", [])]
            try:
                with redirect_stdout(output):
                    status = namespace["main"]()
            finally:
                sys.argv = original_argv
                print(output.getvalue(), end="", flush=True)
        if status != 0:
            raise ValueError("Native probe returned a nonzero result.")
        if identities() != before or sha256(probe.read_bytes()).hexdigest() != digest:
            raise ValueError("Probe or installed source changed during native execution.")
        reports = [json.loads(line) for line in output.getvalue().splitlines()
                   if line.lstrip().startswith("{")]
        reports = [item for item in reports if item.get("status") in ("PASS", "FAIL", "NOT_RUN")]
        if len(reports) != 1:
            raise ValueError("Expected exactly one terminal native receipt.")
        report = reports[0]
        for key, value in config["fields"].items():
            if type(report.get(key)) is not type(value) or report[key] != value:
                raise ValueError("Unexpected native receipt field: " + key)
        expected = Counter(config["checks"])
        if type(report.get("checks")) is list:
            actual = Counter(report["checks"])
        elif type(report.get("checks")) is int:
            actual = Counter(line[6:] for line in output.getvalue().splitlines() if line.startswith("PASS: "))
            if report["checks"] != sum(expected.values()):
                raise ValueError("Native check total differs from its declared population.")
        else:
            raise ValueError("Native receipt has no check inventory.")
        if actual != expected:
            raise ValueError("Native check inventory differs: " + repr((actual, expected)))
        if "probe_modules" in config and "module_origins" not in report:
            raise ValueError("Native probe omitted its required internal source binding.")
        if "module_origins" in report:
            if set(report["module_origins"]) != set(config["probe_modules"]):
                raise ValueError("Native probe's internal module set differs.")
            for name, record in report["module_origins"].items():
                if record != before[name]:
                    raise ValueError("Native probe's internal source binding differs: " + name)
        for field in config.get("positive_metrics", []):
            value = report
            for part in field.split("."):
                value = value[part]
            if type(value) not in (int, float) or not value > 0:
                raise ValueError("Native metric must be positive: " + field)
        for field in config.get("digest_fields", []):
            value = report
            for part in field.split("."):
                value = value[part]
            if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
                raise ValueError("Native digest is missing: " + field)
        def metric(field):
            value = report
            for part in field.split("."):
                value = value[part]
            return value

        for left, right in config.get("equal_fields", []):
            a, b = metric(left), metric(right)
            if type(a) is not type(b) or a != b:
                raise ValueError("Native retained identities differ: " + left + " / " + right)
        for field, count in config.get("digest_lists", {}).items():
            values = metric(field)
            if (type(values) is not list or len(values) != count or len(set(values)) != count
                    or any(type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None
                           for value in values)):
                raise ValueError("Native retained digest inventory is incomplete: " + field)
        if args.kind in ("live", "live-playback"):
            source = "Prefix 😀.\r\nSelected source sentence 🦉. Second source sentence.\nLast paragraph."
            if report["actual_sapi_pcm"]["source_sha256"] != sha256(source.encode("utf-8")).hexdigest():
                raise ValueError("Native speech was not bound to the exact Unicode source.")
        for field, expected_value in config.get("exact_metrics", {}).items():
            value = report
            for part in field.split("."):
                value = value[part]
            if type(value) not in (int, float) or value != expected_value:
                raise ValueError("Native metric differs from the selected setting: " + field)
        if "runtime_digest_field" in config:
            from carr_library.audio.jobs import sha256_file
            runtime = Path(os.environ[config["runtime_environment"]])
            if not runtime.is_absolute():
                raise ValueError("Native runtime must be explicitly selected by absolute path.")
            value = report
            for part in config["runtime_digest_field"].split("."):
                value = value[part]
            if value != sha256_file(runtime):
                raise ValueError("Native PCM does not identify the gate-verified runtime.")
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
        expected_commit = os.environ.get("CARR_EXPECTED_SOURCE_COMMIT", os.environ.get("GITHUB_SHA"))
        if expected_commit and commit != expected_commit:
            raise ValueError("The installed native probe does not belong to the exact event source.")
        if "source_commit" in report and report["source_commit"] != commit:
            raise ValueError("The native receipt names a different source commit.")
        evidence.update(status="PASS", source_commit=commit, source_tree=tree, installed_package=True,
                        module_origins=before, probe_sha256=digest, native=report,
                        checks=sum(expected.values()), full_platform="NOT_CLAIMED")
    except Exception as error:
        evidence["reason"] = type(error).__name__ + ": " + str(error)
    finally:
        if handle is not None:
            handle.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
