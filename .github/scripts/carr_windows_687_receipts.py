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
import xml.etree.ElementTree as ET

REPOSITORY = "Oleksii-debug/AudioTacticalFPS"
RUN = 46119978375
HEAD = "943f585659c55481046266338f428e4a1043a9bd"
BRANCH = "carr/ci-endpoint-evaluation-20261127"
SOURCES = (
    ("junit-summary", 15256851282, 475789,
     "62b88af6bb0a2df3c90b9f4dfbcbe2de9e16e84642d008eb5b4bb98b110ddb65",
     "carr-windows-pre-source-" + HEAD + "-1"),
    ("endpoint", 15253625504, 3942,
     "d7eff7a55f29ba4d5e95b3a266267acdaadd4ba680634f2c7bcf1437f557ef98",
     "carr-windows-audio-endpoint-" + HEAD + "-1"),
    ("recording", 15253256509, 16780,
     "2db6ce9c98482128246425b098a39c2f417c3a8fd270277516b75523cdf5a293",
     "carr-windows-recording-" + HEAD + "-1"),
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
    "installed-scene-clip-editor.txt",
    "source-reader-navigation.json",
    "installed-reader-navigation.json",
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


# This single published JUnit member is parsed only for the post-pytest guards.
# Its digest is derived from the SHA-256-verified immutable archive, not supplied
# as a pre-observed member hash. No XML text or arbitrary testcase text is emitted.
JUNIT_MEMBER = "full-bundle-workflows.xml"
JUNIT_BYTES = 1495285
JUNIT_OBSERVED_TESTS = 9319
JUNIT_CASE_LIMIT = JUNIT_OBSERVED_TESTS + 256
JUNIT_ELEMENT_LIMIT = 4 * JUNIT_CASE_LIMIT + 64
JUNIT_DEPTH_LIMIT = 32
JUNIT_MODULES = {
    "test_import_metadata_shape": 58,
    "test_ocr_runtime": 3,
    "test_state_backup_portability": 6,
}
JUNIT_REQUIRED = frozenset((
    "test_mixed_document_batch_pause_reopen_and_backup_preserve_exact_sources",
    "test_cancelled_extended_import_can_resume_same_identity",
    "test_windows_native_dialog_async_lookup_focus_and_close",
    "test_real_application_lookup_save_apply_from_dialog_preserves_source",
    "test_real_metadata_apply_cancelled_after_writer_acquisition_does_not_commit",
    "test_actual_multilingual_ranker_and_complete_token_coverage",
    "test_actual_application_semantic_scope_and_original_quotes",
    "test_actual_semantic_snapshot_survives_concurrent_membership_revocation",
    "test_scanned_pdf_requires_explicit_review_for_each_exact_page_before_publication",
    "test_reviewed_import_rejects_changed_approval_without_publishing",
    "test_quarantined_batch_waits_for_explicit_review_and_refreshes_external_completion",
    "test_notebook_search_fresh_navigation_and_backup_restore",
    "test_academic_reference_roundtrip_preserves_source_evidence",
    "test_academic_import_is_atomic_and_exports_preserve_metadata",
)) | frozenset(
    "test_long_ascii_source_search_copy_and_context_use_exact_tail[" + mode + "-" + retrieval + "]"
    for mode in ("short", "thematic") for retrieval in ("thematic", "semantic")
) | frozenset(
    "test_application_exports_the_selected_topic_with_readable_title[" + format + "]"
    for format in ("docx", "html")
)
JUNIT_NATIVE_LOOKUP_PREFIX = "test_windows_native_dialog_async_lookup_focus_and_close"
JUNIT_NATIVE_LOOKUP = frozenset(JUNIT_NATIVE_LOOKUP_PREFIX + "[" + kind + "]"
                              for kind in ("doi", "isbn"))
JUNIT_OCR = frozenset((
    "test_empty_directory_inventory_has_a_bound",
    "test_directory_inventory_checks_budget_between_entries",
    "test_unreadable_directory_rejects_entire_inventory",
))
JUNIT_STATE = frozenset((
    "test_state_inventory_rejects_unreadable_subtree[snapshot]",
    "test_state_inventory_rejects_unreadable_subtree[restore]",
    "test_state_restore_rejects_unlisted_file_before_scanning_later_subtree",
    "test_state_root_lookup_error_is_not_absence[PermissionError]",
    "test_state_root_lookup_error_is_not_absence[NotADirectoryError]",
    "test_state_root_missing_is_optional_only_when_requested",
))


def junit_summary(raw):
    if len(raw) != JUNIT_BYTES:
        raise ValueError("Pinned JUnit member size differs")
    text = raw.decode("utf-8-sig", errors="strict")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("DTD and entity declarations are forbidden in JUnit evidence")
    declaration = re.match(r"\s*<\?xml\b[^?]*\?>", text, re.I)
    if declaration:
        encoding = re.search(r"\bencoding\s*=\s*(['\"])(.*?)\1",
                             declaration.group(), re.I)
        if encoding and encoding.group(2).lower() not in ("utf-8", "utf8"):
            raise ValueError("JUnit evidence declares a non-UTF8 encoding")
    modules = {name: [] for name in JUNIT_MODULES}
    observed = set()
    native_lookup, native_unexpected, native_failed_or_skipped = [], 0, 0
    count = failed = errors = failed_or_error = skipped = depth = elements = 0
    for event, element in ET.iterparse(io.StringIO(text), events=("start", "end")):
        if event == "start":
            depth += 1
            elements += 1
            if depth > JUNIT_DEPTH_LIMIT or elements > JUNIT_ELEMENT_LIMIT:
                raise ValueError("JUnit evidence exceeds its fixed structural bound")
            if element.tag == "testcase":
                count += 1
                if count > JUNIT_CASE_LIMIT:
                    raise ValueError("JUnit evidence exceeds its fixed testcase bound")
            continue
        if element.tag == "testcase":
            name, classname = element.get("name", ""), element.get("classname", "")
            if not name or len(classname) > 512:
                raise ValueError("JUnit testcase identity is missing or excessive")
            if name in JUNIT_REQUIRED:
                observed.add(name)
            module = classname.rsplit(".", 1)[-1]
            if module in modules:
                if len(name) > 1024:
                    raise ValueError("Guarded JUnit testcase name is excessive")
                names = modules[module]
                if len(names) >= JUNIT_MODULES[module] + 16:
                    raise ValueError("Guarded JUnit module exceeds its diagnostic headroom")
                names.append(name)
            has_failure = element.find("failure") is not None
            has_error = element.find("error") is not None
            failed += has_failure
            errors += has_error
            failed_or_error += has_failure or has_error
            skipped += element.find("skipped") is not None
            if module == "test_native_acquisition" and name.startswith(JUNIT_NATIVE_LOOKUP_PREFIX):
                if name in JUNIT_NATIVE_LOOKUP:
                    if len(native_lookup) >= len(JUNIT_NATIVE_LOOKUP) + 16:
                        raise ValueError("Native lookup rows exceed diagnostic headroom")
                    native_lookup.append(name)
                else:
                    native_unexpected += 1
                native_failed_or_skipped += (has_failure or has_error
                                             or element.find("skipped") is not None)
        element.clear()
        depth -= 1
    if depth != 0 or count < 1:
        raise ValueError("JUnit evidence is empty or structurally incomplete")
    metadata = modules["test_import_metadata_shape"]
    ocr = modules["test_ocr_runtime"]
    state = modules["test_state_backup_portability"]
    missing = sorted(JUNIT_REQUIRED - observed)
    predicates = {
        "metadata_exact_58_unique": len(metadata) == 58 and len(set(metadata)) == 58,
        "ocr_exact_names": len(ocr) == len(JUNIT_OCR) and set(ocr) == JUNIT_OCR,
        "state_inventory_exact_names": len(state) == len(JUNIT_STATE) and set(state) == JUNIT_STATE,
        "required_names_present": not missing,
        "zero_failed_error_skipped": failed_or_error == skipped == 0,
    }
    return {
        "scope": "read-only reproduction of existing post-pytest JUnit predicates",
        "totals": {"testcases": count, "passed": count - failed_or_error - skipped,
                   "failed": failed, "errors": errors, "failed_or_error": failed_or_error,
                   "skipped": skipped},
        "guarded_modules": {
            name: {"count": len(names), "unique": len(set(names)), "names": sorted(names)}
            for name, names in modules.items()},
        "native_acquisition": {
            "module": "test_native_acquisition",
            "expected_names": sorted(JUNIT_NATIVE_LOOKUP),
            "actual_names": sorted(native_lookup), "count": len(native_lookup),
            "unique": len(set(native_lookup)), "unexpected_variant_count": native_unexpected,
            "failed_error_or_skipped_rows": native_failed_or_skipped,
            "exact_two_current_variants": (len(native_lookup) == len(JUNIT_NATIVE_LOOKUP)
                and set(native_lookup) == JUNIT_NATIVE_LOOKUP and native_unexpected == 0
                and native_failed_or_skipped == 0),
        },
        "missing_required_names": missing, "predicates": predicates,
        "all_guarded_predicates": all(predicates.values()),
        "application_acceptance": "NOT_ASSESSED", "raw_xml_emitted": False,
    }

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
    if mode == "junit-summary":
        return name == JUNIT_MEMBER
    # Admit only this already inventoried public JUnit text; never parse or execute it.
    if mode == "junit":
        return name == "full-bundle-workflows.xml"
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
                if mode == "junit-summary" and entry.file_size != JUNIT_BYTES:
                    raise ValueError("Pinned JUnit member size differs")
                with archive.open(entry) as stream:
                    member = stream.read(entry.file_size + 1)
                if len(member) != entry.file_size:
                    raise ValueError("Evidence member length changed")
                member_digest = hashlib.sha256(member).hexdigest()
                if mode == "junit-summary":
                    summary = junit_summary(member)
                    emit(line("CARR_WINDOWS_JUNIT_SUMMARY", {
                        "artifact": identity, "archive_sha256": digest, "path": name,
                        "bytes": len(member), "sha256": member_digest, "encoding": "utf-8-sig",
                        "run": RUN, "head": HEAD, "attempt": 1, **summary}))
                    continue
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
                if (len(member) <= 32 * 1024
                        or (mode == "junit" and name == "full-bundle-workflows.xml"
                            and len(member) <= 256 * 1024)):
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
