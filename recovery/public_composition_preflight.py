"""Inspect an exact public-source merge without publishing application changes.

This cannot recover or replace the preserved, unavailable local c04/791 work.
The runner only writes a detached temporary worktree and evidence files.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

SHARED = "96294e97498f2133bf93f02ce72f84b6e709c72b"
CANONICAL = "3beebd5aca900b8bf2e86498ae79db4015c96705"
CHECKPOINT = "56a502124e41baf90ee87bdca43ff53b38dd3dc7"
PREFIX = "recovery/canonical-composition-20261127"


def git(root, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed ({result.returncode}): "
                           + result.stderr.decode("utf-8", "replace")[-4000:])
    return result


def text_git(root, *args):
    return git(root, *args).stdout.decode("utf-8").strip()


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def tree(root, revision):
    rows = {}
    for entry in git(root, "ls-tree", "-rz", revision).stdout.split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, sha = meta.decode("ascii").split()
        rows[path.decode("utf-8")] = {"mode": mode, "type": kind, "sha": sha}
    return rows


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid source path")
    result = root / path
    if not result.resolve().is_relative_to(root.resolve()) or result.is_symlink():
        raise ValueError("Source path escapes checkout")
    return result


def verify_candidates(root, manifest):
    checked, base_trees = [], {}
    for item in manifest["files"]:
        data = safe_path(root, item["saved_path"]).read_bytes()
        if blob_sha(data) != item["new_blob"]:
            raise ValueError("Candidate blob differs: " + item["path"])
        base_ref = item.get("base_ref")
        base_blob = item.get("base_blob")
        base_status = "NO_RECORDED_BASE" if not base_blob else "NOT_VERIFIED_MISSING_REF"
        if base_ref and base_blob:
            if base_ref not in base_trees:
                base_trees[base_ref] = tree(root, base_ref)
            base_path = item.get("base_path", item["path"])
            actual = base_trees[base_ref].get(base_path, {}).get("sha")
            if actual != base_blob:
                raise ValueError(f"Recorded base differs: {item['path']}: {actual} != {base_blob}")
            base_status = "PASS"
        if item["path"].endswith(".py"):
            compile(data, item["path"], "exec", dont_inherit=True)
        checked.append({"path": item["path"], "blob": item["new_blob"],
                        "base_ref": base_ref, "base_blob": base_blob,
                        "base_validation": base_status})
    return checked


def inspect_merge(root, work, manifest):
    shared_tree, canonical_tree = tree(root, SHARED), tree(root, CANONICAL)
    candidates = {item["path"]: item for item in manifest["files"]}
    git(root, "worktree", "add", "--detach", str(work), SHARED)
    result = git(
        work, "-c", "user.name=CARR source preflight",
        "-c", "user.email=carr-source-preflight@invalid.example",
        "merge", "--no-commit", "--no-ff", CANONICAL, check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-4000:])
    indexed, conflicts = {}, collections.defaultdict(dict)
    for entry in git(work, "ls-files", "--stage", "-z").stdout.split(b"\0"):
        if not entry:
            continue
        meta, encoded_path = entry.split(b"\t", 1)
        mode, sha, stage = meta.decode("ascii").split()
        path = encoded_path.decode("utf-8")
        if stage == "0":
            indexed[path] = {"mode": mode, "sha": sha}
        else:
            conflicts[path][stage] = {"mode": mode, "sha": sha}
    records = []
    for path, stages in sorted(conflicts.items()):
        item = candidates.get(path, {})
        record = {
            "path": path, "stages": stages,
            "shared_blob": shared_tree.get(path, {}).get("sha"),
            "canonical_blob": canonical_tree.get(path, {}).get("sha"),
            "candidate_blob": item.get("new_blob"),
            "candidate_base_ref": item.get("base_ref"),
            "candidate_base_blob": item.get("base_blob"),
            "status": "UNRESOLVED_SOURCE_MERGE",
        }
        records.append(record)
        print("CARR_MERGE_CONFLICT " + json.dumps(record, sort_keys=True), flush=True)
    tracked = set(indexed) | set(conflicts)
    relevant = lambda path: path.startswith(("carr-library/", ".github/workflows/"))
    missing_canonical = [
        {"path": path, "canonical_blob": row["sha"],
         "shared_blob": shared_tree.get(path, {}).get("sha"),
         "candidate_blob": candidates.get(path, {}).get("new_blob")}
        for path, row in sorted(canonical_tree.items())
        if relevant(path) and row["type"] == "blob" and path not in tracked
    ]
    missing_shared = [
        {"path": path, "shared_blob": row["sha"],
         "canonical_blob": canonical_tree.get(path, {}).get("sha"),
         "candidate_blob": candidates.get(path, {}).get("new_blob")}
        for path, row in sorted(shared_tree.items())
        if relevant(path) and row["type"] == "blob" and path not in tracked
    ]
    automerged = [
        {"path": path, "blob": row["sha"],
         "shared_blob": shared_tree.get(path, {}).get("sha"),
         "canonical_blob": canonical_tree.get(path, {}).get("sha")}
        for path, row in sorted(indexed.items())
        if relevant(path) and row["sha"] not in {
            shared_tree.get(path, {}).get("sha"),
            canonical_tree.get(path, {}).get("sha"),
        }
    ]
    for name, rows in (("MISSING_CANONICAL", missing_canonical),
                       ("MISSING_SHARED", missing_shared),
                       ("AUTOMERGED", automerged)):
        for row in rows:
            print("CARR_MERGE_" + name + " " + json.dumps(row, sort_keys=True), flush=True)
    merge_tree = text_git(work, "write-tree") if not conflicts else None
    return {
        "status": "PUBLIC_MERGE_INSPECTED_NOT_QUALIFIED",
        "shared_commit": SHARED, "canonical_commit": CANONICAL,
        "recovery_checkpoint": CHECKPOINT, "merge_exit": result.returncode,
        "merge_tree_if_resolved": merge_tree,
        "conflicts": records, "missing_canonical": missing_canonical,
        "missing_shared": missing_shared, "automerged": automerged,
        "application_ref_mutations": "NONE",
        "candidate_application": "NONE",
        "local_c04_791_reconciliation": "NOT_RUN_UNAVAILABLE_LOCAL_OBJECTS",
        "runtime_tests": "NOT_RUN", "native_windows": "NOT_RUN",
        "merge_stdout": result.stdout.decode("utf-8", "replace"),
        "merge_stderr": result.stderr.decode("utf-8", "replace"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    args.evidence.mkdir(parents=True, exist_ok=True)
    for commit in (SHARED, CANONICAL, CHECKPOINT):
        if text_git(root, "rev-parse", commit + "^{commit}") != commit:
            raise ValueError("Pinned commit identity changed")
    manifest = json.loads((root / PREFIX / "manifest.json").read_text(encoding="utf-8"))
    expected = git(root, "show", CHECKPOINT + ":" + PREFIX + "/manifest.json").stdout
    if (root / PREFIX / "manifest.json").read_bytes() != expected:
        raise ValueError("Checkpoint manifest differs")
    checked = verify_candidates(root, manifest)
    print("CARR_CANDIDATES_VERIFIED " + json.dumps({
        "candidate_bytes": len(checked),
        "recorded_bases_verified": sum(item["base_validation"] == "PASS" for item in checked),
        "recorded_bases_missing_ref": sum(item["base_validation"] == "NOT_VERIFIED_MISSING_REF" for item in checked),
        "runtime_tests": "NOT_RUN",
    }), flush=True)
    with tempfile.TemporaryDirectory(prefix="carr-public-merge-", dir=os.environ.get("RUNNER_TEMP")) as scratch:
        work = Path(scratch) / "composition"
        try:
            report = inspect_merge(root, work, manifest)
            report["validation_commit"] = text_git(root, "rev-parse", "HEAD")
            report["verified_candidates"] = checked
            (args.evidence / "public-merge-preflight.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print("CARR_MERGE_SUMMARY " + json.dumps({
                "conflicts": len(report["conflicts"]),
                "missing_canonical": len(report["missing_canonical"]),
                "missing_shared": len(report["missing_shared"]),
                "automerged": len(report["automerged"]),
                "status": report["status"],
            }), flush=True)
        finally:
            if work.exists():
                git(root, "worktree", "remove", "--force", str(work), check=False)


if __name__ == "__main__":
    main()
