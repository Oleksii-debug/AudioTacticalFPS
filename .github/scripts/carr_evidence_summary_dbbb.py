"""Bounded read-only summary of existing public synthetic CI evidence artifacts."""
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import urllib.parse
import urllib.request
import zipfile
REPOSITORY = "Oleksii-debug/AudioTacticalFPS"
RUN = 46031920263
HEAD = "94ecfdd48c65d03f0b29abee9000fd4e2a1610ee"
ARTIFACTS = (
    (15219682723, "e62c9c91dc49507b928dd113814b122014602e9f340dd34351075e09306a9c1b"),
    (15219374659, "73da99c774c97c347c93607a320701f07595512adccc7f00aa19f76ed229ef2d"),
    (15219017917, "7376120d1d6de7c6e47b94da495d06db622bedfc806f6f795052fc6eacf3f489"),
)
class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Unexpected artifact redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if parsed.hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected
opener = urllib.request.build_opener(SafeRedirect())
def fetch(url, limit):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "CARR-bounded-evidence-reader",
               "Authorization": "Bearer " + os.environ["GH_TOKEN"]}
    with opener.open(urllib.request.Request(url, headers=headers), timeout=45) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Evidence read exceeded its reviewed bound")
        return data
remaining = 150000
def emit(text):
    global remaining
    if remaining <= 0:
        return
    accepted = str(text)[:remaining]
    print(accepted, flush=True)
    remaining -= len(accepted) + 1
for identity, digest in ARTIFACTS:
    endpoint = "https://api.github.com/repos/" + REPOSITORY + "/actions/artifacts/" + str(identity)
    metadata = json.loads(fetch(endpoint, 128 * 1024))
    if (metadata["id"] != identity or metadata["workflow_run"]["id"] != RUN
            or metadata["workflow_run"]["head_sha"] != HEAD
            or metadata.get("expired") or metadata["digest"] != "sha256:" + digest):
        raise ValueError("Artifact identity changed")
    raw = fetch(endpoint + "/zip", 2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Artifact digest mismatch")
    emit(json.dumps({"artifact": identity, "name": metadata["name"], "sha256": digest,
                     "source_head": HEAD, "scope": "existing failure evidence; no tests executed"}))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(x.file_size for x in entries) > 128 * 1024 * 1024:
            raise ValueError("Evidence archive exceeds reviewed bound")
        emit("FILES " + json.dumps([(x.filename, x.file_size) for x in entries if not x.is_dir()])[:10000])
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name
                    or entry.flag_bits & 1 or entry.file_size > 32 * 1024 * 1024):
                raise ValueError("Unexpected evidence member")
            if entry.is_dir() or path.suffix.lower() not in (".txt", ".log", ".json", ".xml"):
                continue
            content = archive.read(entry).decode("utf-8-sig", errors="replace")
            if path.suffix.lower() == ".json":
                emit("JSON " + name + "\n" + content[:18000])
                continue
            lines = content.splitlines()
            summary = [line for line in lines if re.search(
                r"(?:^|[.])FAIL: tests/|^Required suites complete:|^FAILED tests/|^ERROR tests/", line)]
            emit("ALL_VERDICTS " + name + "\n" + "\n".join(line[:650] for line in summary)[:36000])
            markers = [i for i, line in enumerate(lines) if re.search(
                r"^(?:FAILED |ERROR |FAIL: |Required suites complete:)|"
                r"Traceback \(most recent call last\)|"
                r"^(?:AssertionError|AttributeError|NameError|RuntimeError|TypeError|ValueError|ImportError|PermissionError|FileNotFoundError):|"
                r"^E\s+|={3,} (?:FAILURES|ERRORS|short test summary)", line)]
            if not markers:
                continue
            emit("TEXT " + name + "\n" + "\n".join(lines[i][:1000] for i in markers)[:14000])
            chosen = set()
            for index in markers:
                if lines[index].startswith(("FAIL: ", "Required suites complete:")):
                    continue
                chosen.update(range(max(0, index - 12), min(len(lines), index + 7)))
            emit("CONTEXT " + name + "\n" + "\n".join(
                str(i + 1) + ": " + lines[i][:1000] for i in sorted(chosen))[:5000])
for job in ():
    endpoint = "https://api.github.com/repos/" + REPOSITORY + "/actions/jobs/" + str(job)
    metadata = json.loads(fetch(endpoint, 512 * 1024))
    if metadata["run_id"] != RUN or metadata["head_sha"] != HEAD:
        raise ValueError("Prior job identity differs")
    raw = fetch(endpoint + "/logs", 48 * 1024 * 1024).decode("utf-8-sig", errors="replace")
    lines = raw.splitlines()
    selected = set()
    for index, line in enumerate(lines):
        if re.search(r"clipboard|ModuleNotFoundError|ImportError|unrecognized arguments|can't open file", line, re.I):
            selected.update(range(max(0, index - 6), min(len(lines), index + 12)))
    emit("NATIVE_JOB_CONTEXT " + str(job) + "\n" + "\n".join(lines[i][:1800] for i in sorted(selected))[:26000])
emit(json.dumps({"summary_complete": True, "remaining_output_budget": remaining,
                 "source_run": RUN, "source_head": HEAD, "qualification": "diagnostic only"}))
