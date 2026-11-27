"""Validate exact recovery sources and prepare a bounded citation composition.

This is source-candidate evidence, never native/installed/release acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import textwrap
import tomllib
import yaml
from pathlib import Path
import shutil
import subprocess

CANONICAL = "966e5ccf892243ead6942c635af5fbab47e72551"
SHARED = "8e6d410019f90f54ea694a5ff69402bc52567d57"
CHECKPOINT = "f30136619c028a1a95dd6ca17b63e020f1b9d407"
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



def validate_configs(root, manifest):
    selected = {item["path"]: item for item in manifest["files"]}
    workflows, embedded = [], []
    for item in manifest["files"]:
        path = inside(root, item["saved_path"])
        if path.suffix == ".toml":
            value = tomllib.loads(path.read_text(encoding="utf-8"))
            packages = value["tool"]["setuptools"]["packages"]
            assert len(packages) == len(set(packages)), "Duplicate application package"
        if path.suffix not in (".yml", ".yaml"):
            continue
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(value, dict) and isinstance(value.get("jobs"), dict)
        workflows.append(item["path"])
        for job in value["jobs"].values():
            for step in job.get("steps", ()):
                run = step.get("run", "")
                for match in re.finditer(r"(?m)^\s*(\$\w+)\s*=\s*@'\s*\n([\s\S]*?)^\s*'@\s*$", run):
                    body = textwrap.dedent(match.group(2))
                    significant = [line.strip() for line in body.splitlines()
                                   if line.strip() and not line.lstrip().startswith("#")]
                    if significant and significant[0].startswith(("import ", "from ")):
                        compile(body, item["path"] + ":" + match.group(1), "exec", dont_inherit=True)
                        embedded.append({"workflow": item["path"], "variable": match.group(1)})
    dependency_counts = {}
    for stem in ("windows", "windows-semantic"):
        lock_path = "carr-library/packaging/" + stem + "-cp311.lock"
        sbom_path = "carr-library/packaging/" + stem + "-sbom.cdx.json"
        if lock_path not in selected or sbom_path not in selected:
            continue
        lock = inside(root, selected[lock_path]["saved_path"]).read_text(encoding="utf-8")
        pins = dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock))
        normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
        pins = {normalize(name): version for name, version in pins.items()}
        sbom = json.loads(inside(root, selected[sbom_path]["saved_path"]).read_text(encoding="utf-8"))
        components = {normalize(item["name"]): item["version"] for item in sbom["components"]}
        assert pins == components, (stem, sorted(pins.items()), sorted(components.items()))
        assert pins["quickjs-ng"] == "0.18.0.2"
        dependency_counts[stem] = len(pins)
    return {"yaml_workflows": workflows, "embedded_python": embedded,
            "dependency_lock_sbom_components": dependency_counts}


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
        "configuration_validation": validate_configs(root, manifest),
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
              "recovery_checkpoint": CHECKPOINT, "validation_commit": git(root, "rev-parse", "HEAD"),
              "candidate_paths": list(CITATION_PATHS),
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
