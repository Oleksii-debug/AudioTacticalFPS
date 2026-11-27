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
SOURCES = [[46059259121,"8cb61be3dfd9be2447737dafacb428d850be774d",15228227464,"1dd577683a9c242f376f27284d80c44b37529f47780564020614d511cd718844"],[46059259121,"8cb61be3dfd9be2447737dafacb428d850be774d",15227738360,"53a20719a1e5d968ee787a0f0a89b35169d49ea554ec7b8ffa4d7add5ccadee1"],[46059259121,"8cb61be3dfd9be2447737dafacb428d850be774d",15228013635,"46eda90e4d8832b5d48dc8fba05a2f932be3f1beb6810b78066b2ef02a3239fe"],[46059259121,"8cb61be3dfd9be2447737dafacb428d850be774d",15228407289,"d2d08628cdb1794ad063466fc74987f7fc5f620534b5900aa1527e5011257c61"]]
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
remaining = 400000
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
                    "source_head": head, "scope": "existing synthetic measurement receipts; no tests executed"}))
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
            if entry.is_dir() or path.suffix.lower() != ".json":
                continue
            if ("windows-2022" not in metadata["name"]
                    and path.name != "source-pages.json"):
                continue
            if "windows-2022" in metadata["name"] and "experiment" not in path.parts:
                continue
            raw_member = archive.read(entry)
            value = json.loads(raw_member.decode("utf-8-sig"))
            record = json.dumps({"artifact": identity, "path": name,
                                 "member_sha256": hashlib.sha256(raw_member).hexdigest(),
                                 "value": value}, ensure_ascii=True)
            if len(record) + 64 > remaining:
                raise ValueError("Measurement receipt exceeds output budget")
            emit("MEASUREMENT_RECEIPT " + record)

emit(json.dumps({"summary_complete": True, "remaining_output_budget": remaining,
                 "qualification": "diagnostic only; exact previous source head"}))
