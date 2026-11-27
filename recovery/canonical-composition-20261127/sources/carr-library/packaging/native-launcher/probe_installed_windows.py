"""Verify an already installed private bundle through its exact native launcher.

The trusted packaging verifier checks the complete seal before and after the
diagnostics. This is fixed-path process acceptance, not GUI/NVDA, signing or
fresh-installation acceptance. Never install, repair or replace an environment.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

PACKAGING = Path(__file__).resolve().parent.parent


def _helper(name):
    spec = importlib.util.spec_from_file_location(
        "carr_launcher_" + name, PACKAGING / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verified_seal(bundle, source_commit=None, *, require_installed=True):
    delivery = _helper("create_delivery")
    raw = delivery._read_manifest(bundle, lambda: False)
    if source_commit is None:
        source_commit = json.loads(raw).get("source_commit")
    manifest = delivery._manifest(raw, source_commit)
    _helper("install_windows").verify(bundle, manifest_bytes=raw)
    files = manifest["files"]
    wheels = [name for name in files if name.startswith("wheelhouse/universal_research_library-")
              and name.endswith(".whl")]
    if "ResearchLibrary.exe" not in files or len(wheels) != 1:
        raise ValueError("The sealed bundle must contain the native launcher and one application wheel.")
    if require_installed:
        delivery._plain(bundle / ".venv" / ".installation-complete")
        delivery._plain(bundle / ".venv" / "Scripts" / "python.exe")
    return raw, {
        "source_commit": manifest["source_commit"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "launcher_sha256": files["ResearchLibrary.exe"],
        "application_wheel": wheels[0],
        "application_wheel_sha256": files[wheels[0]],
    }


def inspect_installed(bundle, source_commit=None):
    if os.name != "nt":
        raise RuntimeError("Installed application acceptance requires native Windows")
    bundle = Path(bundle).absolute()
    raw, identity = _verified_seal(bundle, source_commit)
    launcher = bundle / "ResearchLibrary.exe"
    state = subprocess.run([str(launcher), "--launcher-check"], capture_output=True, timeout=15)
    if state.returncode or json.loads(state.stdout) != {"schema": 1, "state": "ready"}:
        raise RuntimeError("A complete offline-installed private package is required; no installation was attempted")
    results = []
    for option in ("--check-installation", "--self-test"):
        process = subprocess.run([str(launcher), option], capture_output=True, timeout=300)
        if process.returncode:
            raise RuntimeError(f"Native launcher {option} failed: {process.returncode}; "
                               + process.stderr.decode("utf-8", "replace"))
        after, after_identity = _verified_seal(bundle, identity["source_commit"])
        if after != raw or after_identity != identity:
            raise RuntimeError("The sealed bundle changed during native launcher acceptance")
        results.append(option)
    return {
        "schema": 1, "status": "passed", **identity, "checks": results,
        "sealed_bundle_verified_before_and_after": True,
        "source_commit_independently_specified": source_commit is not None,
        "acceptance_scope": "fixed-path diagnostic exit status from the verified sealed installation",
        "actual_installed_app_self_test_verified": True,
        "actual_fresh_installation_verified": False,
        "gui_verified": False, "nvda_verified": False, "authenticode_verified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--source-commit", help="Expected exact application source commit")
    args = parser.parse_args()
    report = inspect_installed(args.bundle_root, args.source_commit)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
