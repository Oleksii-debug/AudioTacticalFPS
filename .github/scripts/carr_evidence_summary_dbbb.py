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
SOURCES = [[46059057339,"45b5963842f03de629974ce24057d2ec2e7b93b7",15228897142,"9465079c2c560e95b90f6972c73506797e80274a5fd34ac73b0312ee30026f50"]]
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
                    "source_head": head, "scope": "existing synthetic native evidence; no tests executed"}))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(x.file_size for x in entries) > 128 * 1024 * 1024:
            raise ValueError("Evidence archive exceeds reviewed bound")
        emit("FILES " + json.dumps([(x.filename, x.file_size) for x in entries if not x.is_dir()])[:10000])
        priority = ("reader-player", "ebook-daisy", "bundle", "reader-navigation")
        entries.sort(key=lambda entry: (next((i for i, term in enumerate(priority)
            if term in entry.filename.lower()), len(priority)), entry.filename))
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name
                    or entry.flag_bits & 1 or entry.file_size > 32 * 1024 * 1024):
                raise ValueError("Unexpected evidence member")
            if entry.is_dir() or path.suffix.lower() not in (".txt", ".log", ".json"):
                continue
            content = archive.read(entry).decode("utf-8-sig", errors="replace")
            if path.suffix.lower() == ".json":
                if any(term in name for term in priority):
                    emit("NATIVE_RECEIPT " + name + "\n" + content[:22000])
                else:
                    try:
                        value = json.loads(content)
                    except ValueError:
                        continue
                    if type(value) is dict:
                        emit("OTHER_NATIVE_RESULT " + name + "\n" + json.dumps({
                            key: value[key] for key in ("status", "result", "passed", "failed_stage",
                            "error", "errors", "manual_nvda", "speech_inference") if key in value})[:2500])
            elif any(term in name for term in priority):
                emit("NATIVE_TEXT " + name + "\n" + content[-50000:])
emit(json.dumps({"summary_complete": True, "remaining_output_budget": remaining,
                 "qualification": "diagnostic only; exact previous source head"}))
