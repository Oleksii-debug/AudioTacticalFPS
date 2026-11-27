"""Validate exact recovery sources and prepare a bounded citation composition.

This is source-candidate evidence, never native/installed/release acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

CANONICAL = "966e5ccf892243ead6942c635af5fbab47e72551"
SHARED = "8e6d410019f90f54ea694a5ff69402bc52567d57"
CHECKPOINT = "2b16f4a6b37de990be97edb27dc2a82b4253fb31"
PREFIX = Path("recovery/canonical-composition-20261127")
CITATION_PATHS = (
    "carr-library/src/carr_library/citations/csl.py",
    "carr-library/src/carr_library/academic_integration/formatting.py",
    "carr-library/src/carr_library/academic_integration/UPSTREAM.json",
    "carr-library/tests/academic_integration/test_formatting.py",
    "carr-library/src/carr_library/ui/citation_formatting.py",
    "carr-library/tests/academic_integration/test_citation_ui.py",
)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def inside(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Unexpected recovery path")
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Recovery path leaves its checkout")
    return path


def validate(root, report):
    manifest = json.loads((root / PREFIX / "manifest.json").read_text(encoding="utf-8"))
    if manifest["canonical_commit"] != CANONICAL:
        raise ValueError("Canonical source identity changed")
    checked, compiled = [], []
    for item in manifest["files"]:
        path = inside(root, item["saved_path"])
        data = path.read_bytes()
        actual = blob(data)
        if actual != item["new_blob"]:
            raise ValueError("Candidate bytes differ: " + item["path"])
        checked.append({"path": item["path"], "blob": actual, "sha256": hashlib.sha256(data).hexdigest()})
        if path.suffix == ".py":
            compile(data, item["path"], "exec", dont_inherit=True)
            compiled.append(item["path"])
        elif path.suffix == ".json":
            json.loads(data)
    result = {
        "status": "PASS", "scope": "exact candidate bytes and Python compilation only",
        "validation_commit": git(root, "rev-parse", "HEAD"),
        "recovery_checkpoint": CHECKPOINT, "canonical_commit": CANONICAL,
        "candidates": checked, "compiled_python": compiled,
        "runtime_tests": "NOT_RUN_BY_THIS_STEP", "installed": "NOT_RUN",
        "native_windows": "NOT_RUN", "nvda": "NOT_RUN",
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "candidate_files": len(checked), "compiled_python": len(compiled)}))
    return manifest


def prepare(root, shared, canonical, report, manifest):
    if git(shared, "rev-parse", "HEAD") != SHARED or git(canonical, "rev-parse", "HEAD") != CANONICAL:
        raise ValueError("Composition checkout differs from pinned source")
    # Add only the canonical citation processor/styles, retaining newer shared
    # attestation and publication modules elsewhere in the citations package.
    resource_root = Path("carr-library/src/carr_library/citations")
    for name in ("_vendor_citeproc", "styles"):
        shutil.copytree(canonical / resource_root / name, shared / resource_root / name, dirs_exist_ok=True)
    for name in ("test_csl.py", "test_custom_csl.py"):
        relative = Path("carr-library/tests/citations") / name
        shutil.copy2(canonical / relative, shared / relative)
    selected = {item["path"]: item for item in manifest["files"]}
    for relative in CITATION_PATHS:
        item = selected[relative]
        target = inside(shared, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = inside(root, item["saved_path"]).read_bytes()
        if blob(data) != item["new_blob"]:
            raise ValueError("Candidate changed before composition")
        target.write_bytes(data)
    inventory = []
    for folder in (shared / "carr-library/src", shared / "carr-library/bootstrap"):
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                inventory.append({"path": path.relative_to(shared).as_posix(),
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    result = {"status": "PREPARED_NOT_TESTED", "shared_base": SHARED, "canonical_resources": CANONICAL,
              "recovery_checkpoint": CHECKPOINT, "candidate_paths": list(CITATION_PATHS),
              "scope": "source-only citation composition, not the complete canonical merge",
              "source_inventory": inventory, "installed": "NOT_RUN", "native_windows": "NOT_RUN"}
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "source_files": len(inventory)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--shared", required=True, type=Path)
    parser.add_argument("--canonical", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    manifest = validate(args.root, args.evidence / "candidate-compilation.json")
    prepare(args.root, args.shared, args.canonical, args.evidence / "citation-composition.json", manifest)


if __name__ == "__main__":
    main()
