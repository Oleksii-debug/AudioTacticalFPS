"""Read-only exact Git three-way reconciliation; never execute repository code."""
import ast
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

OURS = "87d1ec6a05963531c5c1907f74884faddac2093e"
INCOMING = "7e8f9d2ef6922bbd7a3fcf02996860ebc57e273f"
BASE = "e5c64a5245583f8f8b404a0f042f6ca438c24d7f"
MAX_BODY = 4 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
ROOT = Path.cwd()
OUT = ROOT / "merge-audit-output"
OUT.mkdir(exist_ok=False)
GIT_OPTIONS = ["-c", "core.hooksPath=/dev/null", "-c", "filter.lfs.required=false",
               "-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.clean=",
               "-c", "user.name=Read-only merge assessment", "-c", "user.email=merge-audit@invalid"]

def git(args, *, cwd=ROOT, allowed=(0,)):
    result = subprocess.run(["git", *GIT_OPTIONS, *args], cwd=cwd, capture_output=True, timeout=180)
    if result.returncode not in allowed:
        raise RuntimeError({"git": args, "exit": result.returncode,
                            "stderr": result.stderr.decode("utf-8", "replace")[:8000]})
    return result

def text(args, **kwargs):
    return git(args, **kwargs).stdout.decode("utf-8", "strict").strip()

def entries(ref):
    result = {}
    for row in git(["ls-tree", "-r", "-z", ref]).stdout.split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        if kind != "blob":
            raise RuntimeError("Unsupported non-blob leaf " + repr(name))
        path = name.decode("utf-8", "strict")
        if path in result:
            raise RuntimeError("Duplicate path")
        result[path] = {"mode": mode, "sha": oid}
    return result

def blob(oid):
    size = int(text(["cat-file", "-s", oid]))
    if size > MAX_BODY:
        raise RuntimeError("Per-file bound exceeded for " + oid)
    data = git(["cat-file", "blob", oid]).stdout
    assert len(data) == size
    assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == oid
    return data

def body(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "base64": base64.b64encode(data).decode("ascii")}

def syntax_summary(data):
    try:
        tree = ast.parse(data.decode("utf-8-sig"))
    except (UnicodeError, SyntaxError) as exc:
        return {"parsed": False, "error": str(exc)}
    definitions = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            key = f"{node.lineno}:{type(node).__name__}:{node.name}"
            definitions[key] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    return {"parsed": True, "definitions": definitions}

assert text(["merge-base", OURS, INCOMING]) == BASE
maps = {name: entries(oid) for name, oid in [("base", BASE), ("ours", OURS), ("incoming", INCOMING)]}
scratch = Path(tempfile.mkdtemp(prefix="exact-merge-audit-", dir=os.environ["RUNNER_TEMP"]))
work = scratch / "worktree"
git(["worktree", "add", "--detach", str(work), OURS])
merge = git(["merge", "--no-commit", "--no-ff", INCOMING], cwd=work, allowed=(0, 1))
index = {}
for row in git(["ls-files", "--stage", "-z"], cwd=work).stdout.split(b"\0"):
    if not row:
        continue
    metadata, name = row.split(b"\t", 1)
    mode, oid, stage = metadata.decode("ascii").split(" ")
    path = name.decode("utf-8", "strict")
    index.setdefault(path, {})[stage] = {"mode": mode, "sha": oid}
conflict_paths = sorted(path for path, stages in index.items() if set(stages) != {"0"})
unmerged = sorted(x.decode("utf-8", "strict") for x in git(["diff", "--name-only", "--diff-filter=U", "-z"], cwd=work).stdout.split(b"\0") if x)
assert unmerged == conflict_paths
assert (merge.returncode == 0) == (not conflict_paths)
index_rows = []
auto_bodies = []
conflicts = []
for path in sorted(set(index) | set(maps["ours"]) | set(maps["incoming"])):
    stages = index.get(path, {})
    row = {"path": path, "base": maps["base"].get(path), "ours": maps["ours"].get(path),
           "incoming": maps["incoming"].get(path), "index": stages}
    index_rows.append(row)
    if set(stages) == {"0"} and stages["0"] not in (row["ours"], row["incoming"]):
        item = {"path": path, "result": stages["0"], **body(blob(stages["0"]["sha"]))}
        if path.endswith(".py"):
            item["syntax"] = syntax_summary(blob(stages["0"]["sha"]))
        auto_bodies.append(item)
    elif stages and set(stages) != {"0"}:
        item = {"path": path, "stages": stages}
        candidate = work / path
        if candidate.is_symlink():
            item["worktree"] = {"kind": "symlink", "target": os.readlink(candidate)}
        elif candidate.is_file():
            data = candidate.read_bytes()
            if len(data) > MAX_BODY:
                raise RuntimeError("Conflict file bound exceeded")
            item["worktree"] = body(data)
            item["marker_count"] = data.count(b"<<<<<<< ")
        else:
            item["worktree"] = None
        if path.endswith(".py"):
            item["side_syntax"] = {stage: syntax_summary(blob(entry["sha"]))
                                   for stage, entry in stages.items()}
        conflicts.append(item)
summary = {
    "schema": "carr.exact-incoming-merge-assessment.v1", "status": "CONFLICTS_REQUIRE_REVIEW" if conflicts else "TEXT_MERGE_ONLY",
    "base": BASE, "ours": OURS, "incoming": INCOMING, "git": text(["--version"]),
    "source_trees": {name: text(["rev-parse", oid + "^{tree}"]) for name, oid in [("base", BASE), ("ours", OURS), ("incoming", INCOMING)]},
    "source_leaf_counts": {name: len(rows) for name, rows in maps.items()},
    "merge_exit": merge.returncode, "conflict_count": len(conflicts),
    "new_automatic_blob_count": len(auto_bodies), "index_path_count": len(index),
    "merge_stdout": merge.stdout.decode("utf-8", "strict"), "merge_stderr": merge.stderr.decode("utf-8", "strict"),
    "repository_code_executed": False, "refs_updated": False, "runtime_status": "NOT_RUN",
    "scope": "Exact native Git textual merge assessment only; conflict resolution and behavioral integration remain unqualified."
}
records = [{"record": "summary", "value": summary},
           {"record": "index", "value": index_rows}]
records += [{"record": "automatic", "value": item} for item in auto_bodies]
records += [{"record": "conflict", "value": item} for item in conflicts]
encoded = [json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")) for item in records]
payload = ("\n".join(encoded) + "\n").encode("utf-8")
if len(payload) > MAX_OUTPUT:
    raise RuntimeError("Complete report exceeds bounded output; no truncated report will be emitted")
(OUT / "exact-merge-audit.jsonl").write_bytes(payload)
print("MERGE_AUDIT_BEGIN")
for line in encoded:
    print(line, flush=True)
print(json.dumps({"record": "end", "records": len(records), "bytes": len(payload),
                  "sha256": hashlib.sha256(payload).hexdigest()}, sort_keys=True))
print("MERGE_AUDIT_END")
