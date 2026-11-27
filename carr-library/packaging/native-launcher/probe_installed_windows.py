"""Explicit acceptance against an already offline-installed private Windows bundle.

Never install, repair, delete, or replace an environment. This checks the actual
installed Python application through the exact native launcher, not GUI/NVDA.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def inspect_installed(bundle):
    if os.name != "nt":
        raise RuntimeError("Installed application acceptance requires native Windows")
    launcher = bundle / "ResearchLibrary.exe"
    before = hashlib.sha256(launcher.read_bytes()).hexdigest()
    state = subprocess.run([str(launcher), "--launcher-check"], capture_output=True, timeout=15)
    if state.returncode or json.loads(state.stdout).get("state") != "ready":
        raise RuntimeError("A complete offline-installed private package is required; no installation was attempted")
    results = []
    for option in ("--check-installation", "--self-test"):
        process = subprocess.run([str(launcher), option], capture_output=True, timeout=300)
        if process.returncode:
            raise RuntimeError(f"Native launcher {option} failed: {process.returncode}; "
                               + process.stderr.decode("utf-8", "replace"))
        results.append(option)
    if hashlib.sha256(launcher.read_bytes()).hexdigest() != before:
        raise RuntimeError("The launcher changed during acceptance")
    return {"schema": 1, "status": "passed", "launcher_sha256": before, "checks": results,
            "actual_installed_app_self_test_verified": True, "actual_fresh_installation_verified": False,
            "gui_verified": False, "nvda_verified": False, "authenticode_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = inspect_installed(args.bundle_root.resolve(strict=True))
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
