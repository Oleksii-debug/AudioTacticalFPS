"""Read exact completed synthetic benchmark artifacts; never execute their contents."""
import base64
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = "Oleksii-debug/AudioTacticalFPS"
RUN = 46071323514
HEAD = "87939e06609e133774c5b7361f345d028f0514db"
BRANCH = "carr/source-pages-windows50k-evaluation-20261127"
SOURCES = (
    ("source", 15234155805, 40334, "bec43ecf0d2e65fa31fbd12939f05b51e765fe042ca7336a3a4ebf325adb7c42"),
    ("installed", 15233482059, 40617, "37b513e16ab299f9c4154774d632c6ec5b09146687e49b991695b82e044148a4"),
)
PREFIX = "experiment-windows50000/"
EXPECTED = {
    "application-source.json", "installed-packages.json", "prior-trial.json",
    "tested-commit.txt", "benchmark-contract.xml", "benchmark-contract.txt", "measurement.txt",
    PREFIX + "run.json", PREFIX + "source-pages.json", PREFIX + "benchmark-harness.py",
    *(PREFIX + "50000/" + name + ".json" for name in ("corpus", "build", "phrase", "thematic")),
}
MAX_MEMBERS = 32
MAX_MEMBER_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect())

def bounded(response, limit):
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Evidence read exceeded its reviewed byte bound")
    return data

def api(url, limit):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "api.github.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment or not parsed.path.startswith("/repos/" + REPOSITORY + "/")):
        raise ValueError("Unexpected GitHub API endpoint")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "CARR-source-pages-receipts",
               "Authorization": "Bearer " + os.environ["GH_TOKEN"]}
    with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return bounded(response, limit)

def archive_bytes(endpoint, size):
    try:
        return api(endpoint + "/zip", size)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code != 302:
                raise ValueError("The immutable artifact download did not redirect as expected") from None
            location = exc.headers.get("Location", "")
        finally:
            exc.close()
    parsed = urllib.parse.urlsplit(location)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)
            or not re.fullmatch(r"productionresultssa[0-9]+\.blob\.core\.windows\.net", parsed.hostname or "")):
        raise ValueError("Unexpected signed artifact storage endpoint")
    # No Authorization header and no second redirect are admitted.
    with opener.open(urllib.request.Request(location, headers={
            "User-Agent": "CARR-source-pages-receipts"}), timeout=30) as response:
        return bounded(response, size)

def decoded_json(raw):
    def invalid(value):
        raise ValueError("Non-finite value in a retained JSON receipt")
    return json.loads(raw.decode("utf-8-sig"), parse_constant=invalid)

def line(kind, value):
    return kind + " " + json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))

def main():
    run = decoded_json(api("https://api.github.com/repos/" + REPOSITORY + "/actions/runs/" + str(RUN), 256 * 1024))
    if (run["id"] != RUN or run["head_sha"] != HEAD or run["head_branch"] != BRANCH
            or run["run_attempt"] != 1 or run["status"] != "completed" or run["conclusion"] != "success"
            or run["repository"]["full_name"] != REPOSITORY):
        raise ValueError("Completed benchmark run identity changed")
    output = [line("CARR_BENCHMARK_READER", {
        "run": RUN, "head": HEAD, "attempt": 1, "python": sys.version,
        "scope": "read-only retrieval of completed public synthetic measurements; no application execution"})]
    for mode, identity, size, digest in SOURCES:
        endpoint = "https://api.github.com/repos/" + REPOSITORY + "/actions/artifacts/" + str(identity)
        metadata = decoded_json(api(endpoint, 128 * 1024))
        name = "carr-source-pages-windows50000-" + mode + "-" + HEAD + "-1"
        if (metadata["id"] != identity or metadata["name"] != name or metadata["size_in_bytes"] != size
                or metadata["workflow_run"]["id"] != RUN or metadata["workflow_run"]["head_sha"] != HEAD
                or metadata["workflow_run"]["head_branch"] != BRANCH
                or metadata.get("expired") or metadata["digest"] != "sha256:" + digest):
            raise ValueError("Artifact identity changed")
        raw = archive_bytes(endpoint, size)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Artifact size or digest mismatch")
        output.append(line("CARR_BENCHMARK_ARTIFACT", metadata))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_MEMBERS or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Evidence archive exceeds its reviewed inventory bound")
            files, seen = {}, set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name
                        or name.rstrip("/") != path.as_posix() or ":" in name
                        or entry.flag_bits & 1 or entry.file_size < 0 or entry.file_size > MAX_MEMBER_BYTES
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or name.casefold() in seen):
                    raise ValueError("Unexpected evidence member")
                seen.add(name.casefold())
                if not entry.is_dir():
                    files[name] = entry
            if set(files) != EXPECTED:
                raise ValueError("The exact retained benchmark file inventory changed")
            for name in sorted(files):
                entry = files[name]
                with archive.open(entry) as stream:
                    member = stream.read(entry.file_size + 1)
                if len(member) != entry.file_size:
                    raise ValueError("Evidence member length changed")
                member_digest = hashlib.sha256(member).hexdigest()
                output.append(line("CARR_BENCHMARK_MEMBER", {
                    "artifact": identity, "mode": mode, "path": name,
                    "bytes": len(member), "sha256": member_digest}))
                # Small base64 rows preserve original BOM/CRLF and every receipt byte.
                for index, start in enumerate(range(0, len(member), 3072)):
                    output.append(line("CARR_BENCHMARK_BYTES", {
                        "artifact": identity, "path": name, "part": index,
                        "base64": base64.b64encode(member[start:start + 3072]).decode("ascii")}))
                if name.endswith(".json"):
                    decoded_json(member)
                    output.append("CARR_BENCHMARK_JSON_BEGIN " + json.dumps({"artifact": identity, "path": name}))
                    output.append(member.decode("utf-8"))
                    output.append("CARR_BENCHMARK_JSON_END " + json.dumps({"artifact": identity, "path": name}))
    output.append(line("CARR_BENCHMARK_COMPLETE", {"run": RUN, "head": HEAD,
        "artifacts": 2, "files": 2 * len(EXPECTED), "all_bytes_retained": True,
        "performance_acceptance": "NOT_ASSESSED"}))
    if sum(len(item.encode("utf-8")) + 1 for item in output) > MAX_OUTPUT_BYTES:
        raise ValueError("Complete evidence exceeds its reviewed output bound")
    for item in output:
        print(item, flush=True)

if __name__ == "__main__":
    main()
