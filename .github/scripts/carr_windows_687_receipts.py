"""Read immutable public Windows receipts; never extract or execute artifact files."""
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
RUN = 46093366008
HEAD = "8bcbdb74328d7f6bb71ea8987cb5305064b8eb0e"
BRANCH = "carr/v3-integrated-final-dbbb-20261126"
SOURCES = (
    ("bundle", 15242339416, 181346,
     "924dde88988090e7f7137c2c54a94fde232197c252da4eb17a8c5bd6f6fabe63",
     "carr-windows-pre-source-" + HEAD + "-1"),
    ("endpoint", 15241719856, 1002,
     "d87984ddd5f310e380140a529e583c18c3497f7a3e028bfeac6e03e879bff6ff",
     "carr-windows-audio-endpoint-" + HEAD + "-1"),
)
MAX_MEMBERS = 512
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
BUNDLE_PREFIXES = ("bundle-", "full-bundle-", "core-bundle-", "offline-bundle-")
ENDPOINT_RECEIPTS = frozenset(("audio-endpoint.json", "endpoint-evaluation.json", "audio-services.json",
                               "recording-access-diagnostic.json"))
STANDARD_RECEIPTS = frozenset((
    "native/uninstall.log",
    "native/uninstall-process.log",
    "native/uninstall-poststate.json",
    "native/repair.log",
    "native/repair-process.log",
    "native/repaired-native-check.log",
    "native/install.log",
    "native/install-process.log",
    "native/modified-uninstall.log",
    "native/modified-uninstall-process.log",
    "native/busy-uninstall.log",
    "native/busy-uninstall-process.log",
    "native/shortcuts.json",
    "native/result.json",
    "setup-build.json",
    "native-standard-installation.txt",
    "native-standard-verdict.json",
))
MAIN_RECEIPTS = frozenset((
    "native-delivery-lifecycle.txt",
    "native-reader-controls.json",
    "native-reader-controls.txt",
    "native-owner-voice-controls.txt",
    "native-translation-source.txt",
    "native-draft-timer.json",
    "native-draft-timer.txt",
    "native-save-all-recovery.json",
    "native-save-all-recovery.txt",
    "native-reader-keymap.json",
    "native-reader-keymap.txt",
    "native-audio-runtime-setup.json",
    "native-audio-runtime-setup.txt",
    "native-sapi-speech.json",
    "native-audio-preview.json",
))
ERROR_LINE = re.compile(r"Traceback|AssertionError|TimeoutError|RuntimeError|ValueError|"
                        r"PermissionError|OSError|FileNotFoundError|FAIL(?:ED)?\b|"
                        r"\bERROR\b|Exception|access violation", re.I)

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
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "CARR-Windows-687-receipts",
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
            "User-Agent": "CARR-Windows-687-receipts"}), timeout=30) as response:
        return bounded(response, size)

def decoded_json(raw):
    def invalid(value):
        raise ValueError("Non-finite value in a retained JSON receipt")
    return json.loads(raw.decode("utf-8-sig"), parse_constant=invalid)

def line(kind, value):
    return kind + " " + json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def wanted(name, mode):
    # Standard setup admits only these exact public receipt paths.
    if mode == "standard":
        return name in STANDARD_RECEIPTS
    # Other modes consume only explicitly reviewed top-level JSON/text receipts.
    path = PurePosixPath(name)
    if len(path.parts) != 1 or path.suffix not in (".json", ".txt", ".log"):
        return False
    if mode == "recording":
        return "recording" in path.name
    return (path.name.startswith(BUNDLE_PREFIXES) or path.name in ENDPOINT_RECEIPTS
            or path.name in MAIN_RECEIPTS)


def text_content(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16-bom"
    return raw.decode("utf-8-sig"), "utf-8-sig"


def excerpt(text):
    if len(text) <= 32 * 1024:
        return text, True
    lines = text.splitlines()
    found = []
    for index, value in enumerate(lines):
        if ERROR_LINE.search(value):
            lower, upper = max(0, index - 8), min(len(lines), index + 18)
            found.append("LINES " + str(lower + 1) + ".." + str(upper)
                         + "\n" + "\n".join(lines[lower:upper]))
    context = "\n".join(found)[-16 * 1024:]
    return ("ERROR CONTEXT (bounded)\n" + context
            + "\nFINAL TEXT (bounded)\n" + text[-16 * 1024:]), False


def main():
    run = decoded_json(api("https://api.github.com/repos/" + REPOSITORY
                           + "/actions/runs/" + str(RUN), 256 * 1024))
    if (run["id"] != RUN or run["head_sha"] != HEAD or run["head_branch"] != BRANCH
            or run["run_attempt"] != 1 or run["status"] not in ("in_progress", "completed")
            or run["repository"]["full_name"] != REPOSITORY
            or run["path"] != ".github/workflows/carr-windows.yml"):
        raise ValueError("Source run identity changed")
    # The application job may still run. Only already-uploaded immutable
    # artifacts are read; no live job log or unfinished stream is requested.
    output_bytes = 0
    def emit(value):
        nonlocal output_bytes
        output_bytes += len(value.encode("utf-8")) + 1
        if output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError("Evidence exceeds its reviewed output bound")
        print(value, flush=True)

    emit(line("CARR_WINDOWS_READER", {
        "run": RUN, "head": HEAD, "attempt": 1, "python": sys.version,
        "source_run_status": run["status"], "source_run_conclusion": run["conclusion"],
        "scope": "read-only immutable public bundle/capture receipts; no application execution"}))
    selected_total = 0
    for mode, identity, size, digest, expected_name in SOURCES:
        endpoint = "https://api.github.com/repos/" + REPOSITORY + "/actions/artifacts/" + str(identity)
        metadata = decoded_json(api(endpoint, 128 * 1024))
        if (metadata["id"] != identity or metadata["name"] != expected_name
                or metadata["size_in_bytes"] != size or metadata["workflow_run"]["id"] != RUN
                or metadata["workflow_run"]["head_sha"] != HEAD
                or metadata["workflow_run"]["head_branch"] != BRANCH
                or metadata.get("expired") or metadata["digest"] != "sha256:" + digest):
            raise ValueError("Artifact identity changed")
        raw = archive_bytes(endpoint, size)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Artifact size or digest mismatch")
        emit(line("CARR_WINDOWS_ARTIFACT", {
            "id": identity, "name": expected_name, "bytes": size, "sha256": digest,
            "created_at": metadata["created_at"], "updated_at": metadata["updated_at"],
            "run": RUN, "head": HEAD, "mode": mode}))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_MEMBERS or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Evidence archive exceeds its reviewed inventory bound")
            files, seen = {}, set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (not name or len(name) > 240 or path.is_absolute() or ".." in path.parts
                        or "\\" in name or "\x00" in name or ":" in name
                        or name.rstrip("/") != path.as_posix() or entry.flag_bits & 1
                        or entry.file_size < 0 or entry.file_size > MAX_MEMBER_BYTES
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or name.casefold() in seen):
                    raise ValueError("Unexpected evidence member")
                seen.add(name.casefold())
                if not entry.is_dir():
                    files[name] = entry
            # Inventory precedes any selected member content. Unselected files
            # are never decompressed, extracted, imported, or executed.
            emit(line("CARR_WINDOWS_INVENTORY", {
                "artifact": identity, "files": [
                    {"path": name, "bytes": item.file_size, "compressed_bytes": item.compress_size,
                     "selected": wanted(name, mode)}
                    for name, item in sorted(files.items())]}))
            selected = [name for name in sorted(files) if wanted(name, mode)]
            if not selected or len(selected) > 128:
                raise ValueError("Expected bounded bundle/capture receipt selection is absent or excessive")
            selected_total += len(selected)
            for name in selected:
                entry = files[name]
                with archive.open(entry) as stream:
                    member = stream.read(entry.file_size + 1)
                if len(member) != entry.file_size:
                    raise ValueError("Evidence member length changed")
                member_digest = hashlib.sha256(member).hexdigest()
                text, encoding = text_content(member)
                info = {"artifact": identity, "mode": mode, "path": name,
                        "bytes": len(member), "sha256": member_digest, "encoding": encoding}
                emit(line("CARR_WINDOWS_MEMBER", info))
                parsed = None
                if name.endswith(".json"):
                    try:
                        parsed = decoded_json(member if encoding == "utf-8-sig" else text.encode("utf-8"))
                    except ValueError:
                        pass  # Several retained .json files are native console transcripts.
                if isinstance(parsed, dict):
                    checks = parsed.get("checks")
                    summary = {key: parsed[key] for key in
                        ("status", "phase", "source_commit", "installed", "failure", "error", "errors")
                        if key in parsed}
                    if isinstance(checks, list):
                        summary["check_count"] = len(checks)
                        summary["last_checks"] = checks[-8:]
                    emit(line("CARR_WINDOWS_JSON_SUMMARY", {**info, "summary": summary}))
                    failed = (str(parsed.get("status", "")).upper() in ("FAIL", "FAILED", "ERROR")
                              or bool(parsed.get("failure") or parsed.get("error") or parsed.get("errors")))
                    if (not failed and name not in ENDPOINT_RECEIPTS and name not in STANDARD_RECEIPTS
                            and name not in MAIN_RECEIPTS):
                        continue
                snippet, complete = excerpt(text)
                emit(line("CARR_WINDOWS_TEXT", {
                    **info, "complete_text": complete, "text": snippet}))
                # Preserve exact bytes for small failure receipts or console
                # transcripts; longer logs are explicitly partial excerpts.
                if len(member) <= 32 * 1024:
                    for index, start in enumerate(range(0, len(member), 3072)):
                        emit(line("CARR_WINDOWS_BYTES", {
                            "artifact": identity, "path": name, "part": index,
                            "base64": base64.b64encode(member[start:start + 3072]).decode("ascii")}))
    emit(line("CARR_WINDOWS_COMPLETE", {
        "run": RUN, "head": HEAD, "artifacts": len(SOURCES), "selected_files": selected_total,
        "zip_digests_verified": True, "application_acceptance": "NOT_ASSESSED",
        "long_logs": "bounded excerpts; full member hashes retained",
        "private_assets": "NOT_READ"}))


if __name__ == "__main__":
    main()
