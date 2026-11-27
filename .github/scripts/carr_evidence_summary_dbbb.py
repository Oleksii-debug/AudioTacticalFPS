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
SOURCES = (
    (46031920263, "94ecfdd48c65d03f0b29abee9000fd4e2a1610ee", 15222511230,
     "1b8d4313d87515f4d7900870c6d974d97acdab50b407877145f2f63bba068bde"),
    (46048596823, "142de6b6028cc86f9a5bf70c42e60adc40f96b77", 15225545937,
     "37b5be532c41622c85bade0028b83f5c56bc6da41886c3484ea205cb88d04269"),
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
remaining = 220000
def emit(text):
    global remaining
    if remaining <= 0:
        return
    accepted = str(text)[:remaining]
    print(accepted, flush=True)
    remaining -= len(accepted) + 1
for run, head, identity, digest in SOURCES:
    endpoint = "https://api.github.com/repos/" + REPOSITORY + "/actions/artifacts/" + str(identity)
    metadata = json.loads(fetch(endpoint, 128 * 1024))
    if (metadata["id"] != identity or metadata["workflow_run"]["id"] != run
            or metadata["workflow_run"]["head_sha"] != head
            or metadata.get("expired") or metadata["digest"] != "sha256:" + digest):
        raise ValueError("Artifact identity changed")
    raw = fetch(endpoint + "/zip", 2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Artifact digest mismatch")
    emit(json.dumps({"artifact": identity, "name": metadata["name"], "sha256": digest,
                    "source_head": head, "scope": "existing public failure evidence; no tests executed"}))
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
            lines = content.splitlines()
            if identity == 15225545937:
                if "reading-production" in name:
                    emit("CURRENT_WINDOWS_SOURCE_VERDICTS " + name + "\n" + "\n".join(
                        line[:800] for line in lines if re.search(
                        r"PASS: tests/|FAIL: tests/|Required suites complete:|TIMEOUT|TimeoutExpired|ERROR tests/|FAILED tests/", line))[:22000])
                    emit("CURRENT_WINDOWS_SOURCE_TAIL " + name + "\n" + content[-70000:])
                elif "book-audio" in name and path.suffix.lower() == ".json":
                    emit("CURRENT_BOOK_RECEIPT " + name + "\n" + content[:18000])
                continue
            terms = ("test_stopped_child_reports_verified_fresh_commit", "after-commit")
            hits = [i for i, line in enumerate(lines) if any(term in line for term in terms)]
            if not hits:
                continue
            chosen = set()
            for index in hits:
                chosen.update(range(max(0, index - 45), min(len(lines), index + 95)))
            emit("IMPORT_COMMIT_FAILURE_CONTEXT " + name + "\n" + "\n".join(
                str(i + 1) + ": " + lines[i][:1200] for i in sorted(chosen))[:65000])
emit(json.dumps({"summary_complete": True, "remaining_output_budget": remaining,
                 "qualification": "diagnostic only; immutable prior source heads"}))
