"""Bounded diagnostic of production worker bytecode arguments; no private engines."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

BASE = "f4cc2e11cac1576652dee55bf9984935379ac864"
FIX = "54d34377170554b051fe8700bad86ddff14fa355"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ci-bytecode-results"
PATHS = ("src/carr_library/speech/runtime.py", "src/carr_library/speech/reading.py")
TEST = "tests/packaging/test_worker_bytecode.py"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run_phase(name):
    xml = OUT / (name + ".xml")
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", "-m", "pytest", "-q", "-o", "addopts=",
               "-o", "pythonpath=src", "-o", "xfail_strict=true", "--import-mode=importlib",
               "--junitxml=" + str(xml), TEST]
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120)
    log = result.stdout + result.stderr
    (OUT / (name + ".log")).write_text(log, encoding="utf-8")
    print(log, flush=True)
    cases = list(ET.parse(xml).iter("testcase")) if xml.exists() else []
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    return dict(returncode=result.returncode, cases=len(cases), failures=failures,
                errors=errors, skipped=skipped, passed=len(cases)-failures-errors-skipped)


def main():
    OUT.mkdir(exist_ok=True)
    fixed = {name: (ROOT / name).read_bytes() for name in PATHS}
    baseline = {}
    for name in PATHS:
        baseline[name] = subprocess.check_output(
            ["git", "-C", str(ROOT.parent), "show", BASE + ":carr-library/" + name])
    try:
        for name, data in baseline.items():
            (ROOT / name).write_bytes(data)
        before = run_phase("before")
    finally:
        for name, data in fixed.items():
            (ROOT / name).write_bytes(data)
    after = run_phase("after")
    unchanged = all((ROOT / name).read_bytes() == data for name, data in fixed.items())
    expected_before = dict(returncode=1, cases=2, failures=2, errors=0, skipped=0, passed=0)
    expected_after = dict(returncode=0, cases=2, failures=0, errors=0, skipped=0, passed=2)
    passed = before == expected_before and after == expected_after and unchanged
    report = dict(status="PASS" if passed else "FAIL", baseline_source=BASE, application_source=FIX,
                  diagnostic_source=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  before=before, after=after, fixed_sources_unchanged=unchanged,
                  fixed_sha256={name:digest(data) for name,data in fixed.items()},
                  baseline_sha256={name:digest(data) for name,data in baseline.items()},
                  regression_sha256=digest((ROOT / TEST).read_bytes()),
                  platform=sys.platform, scope="real child imports; synthetic COM response; no acoustic claim",
                  manual_nvda="NOT_RUN", final_bundle_seal="NOT_RUN")
    (OUT / "bytecode-worker-proof.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
