"""Explicit acceptance against an already offline-installed private Windows bundle.

Never install, repair, delete, or replace an environment. This checks the actual
installed Python application through the exact native launcher, not GUI/NVDA.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

from bundle_identity import snapshot_launcher
from windows_job import run_in_job


def _verify_bundle(bundle):
    # The candidate's install_windows.py remains data. Only the trusted helper
    # adjacent to this source probe is imported for its read-only verifier.
    installer_path = Path(__file__).resolve().parent.parent / "install_windows.py"
    spec = importlib.util.spec_from_file_location("carr_installed_launcher_verify", installer_path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    raw = (bundle / "bundle-manifest.json").read_bytes()
    installer.verify(bundle, manifest_bytes=raw)
    manifest = json.loads(raw)
    _payload, identity = snapshot_launcher(bundle / "ResearchLibrary.exe")
    if (not isinstance(manifest.get("source_commit"), str)
            or not re.fullmatch("[0-9a-f]{40}", manifest["source_commit"])
            or manifest.get("native_launcher") != identity
            or manifest["files"].get("ResearchLibrary.exe") != identity["sha256"]):
        raise ValueError("The installed bundle does not seal the exact approved native entry.")
    return hashlib.sha256(raw).hexdigest(), manifest["source_commit"]


def inspect_installed(bundle):
    if os.name != "nt":
        raise RuntimeError("Installed application acceptance requires native Windows")
    launcher = bundle / "ResearchLibrary.exe"
    manifest_before, source_commit = _verify_bundle(bundle)
    before = hashlib.sha256(launcher.read_bytes()).hexdigest()
    state = subprocess.run([str(launcher), "--launcher-check"], capture_output=True, timeout=15)
    if state.returncode or json.loads(state.stdout).get("state") != "ready":
        raise RuntimeError("A complete offline-installed private package is required; no installation was attempted")
    results = []
    for option in ("--check-installation", "--self-test"):
        process = run_in_job(launcher, [option], cwd=bundle, timeout=300)
        if process.returncode:
            raise RuntimeError(f"Native launcher {option} failed: {process.returncode}; "
                               "use CheckInstallation.cmd for Python diagnostic output")
        results.append(option)
    if hashlib.sha256(launcher.read_bytes()).hexdigest() != before:
        raise RuntimeError("The launcher changed during acceptance")
    if _verify_bundle(bundle) != (manifest_before, source_commit):
        raise RuntimeError("The sealed installation changed during acceptance")
    return {"schema": 1, "status": "passed", "launcher_sha256": before, "checks": results,
            "bundle_manifest_sha256": manifest_before, "source_commit": source_commit,
            "diagnostic_process_tree_bounded": True, "python_child_output_captured": False,
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
