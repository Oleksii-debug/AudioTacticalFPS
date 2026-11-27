"""Inspect the exact official VB-CABLE Pack46 archive without executing its code.

Only this stdlib inspector, our read-only PowerShell helper, and an already
installed Microsoft-signed SDK SignTool are run. No installer, INF, SYS, package
DLL, certificate import, driver registration, policy change, or app test is run.
A completed inspection is not permission to install or an audio qualification.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
import zipfile

ARCHIVE_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip"
DOCUMENT_URLS = (
    "https://vb-audio.com/Cable/index.htm",
    "https://vb-audio.com/Services/licensing.htm",
    "https://shop.vb-audio.com/en/content/3-Terms-of-use",
)
REFERENCES = (
    *DOCUMENT_URLS,
    "https://vb-audio.com/Cable/VBCABLE_ReferenceManual.pdf",
    "https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool",
    "https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/signtool",
    "https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/driver-signing-offerings",
    "https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-authenticodesignature",
)
ALLOWED_HOSTS = {"download.vb-audio.com", "vb-audio.com", "shop.vb-audio.com"}
ARCHIVE_LIMIT = 16 * 1024 * 1024
MEMBER_LIMIT = 32 * 1024 * 1024
EXPANDED_LIMIT = 64 * 1024 * 1024
TEXT_LIMIT = 1024 * 1024
REPORT_LIMIT = 32 * 1024 * 1024
OUTPUT_LIMIT = 16 * 1024
CHUNK = 64 * 1024
MAX_MEMBERS = 256
MAX_SIGNATURE_FILES = 64
MAX_VERIFICATIONS = 128
DEADLINE_SECONDS = 600
SIGNATURE_EXTENSIONS = {".exe", ".sys", ".cat", ".inf"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".rtf", ".inf"}
KEYWORDS = (
    "licen", "terms", "copyright", "install", "uninstall", "reboot",
    "restart", "administrator", "silent", "unattended", "server", "arm64",
)
RESERVED = {"con", "prn", "aux", "nul", "clock$",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
            *(f"{prefix}{digit}" for prefix in ("com", "lpt") for digit in "¹²³")}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Inspection deadline exceeded.")


def stamp(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def plain(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Linked/reparse inspection path refused.")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        raise ValueError("Nonregular/shared inspection path refused.")
    return info


def checked_parents(path: Path) -> None:
    for parent in reversed(path.parents):
        plain(parent, directory=True)


def digest_file(path: Path, maximum: int, deadline: float) -> str:
    before = plain(path)
    if before.st_size > maximum:
        raise ValueError("File exceeds inspection bound.")
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError("File changed at inspection open.")
        while True:
            check(deadline)
            part = stream.read(min(CHUNK, before.st_size + 1 - count))
            if not part:
                break
            count += len(part)
            if count > before.st_size:
                raise ValueError("File grew during inspection.")
            digest.update(part)
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError("Opened file changed during inspection.")
    if count != before.st_size or stamp(plain(path)) != stamp(before):
        raise ValueError("File changed during inspection.")
    return digest.hexdigest()


def portable_name(value: str) -> str:
    if not value or len(value) > 512 or "\\" in value or "\x00" in value:
        raise ValueError("Nonportable archive member.")
    value = value[:-1] if value.endswith("/") else value
    if not value or PurePosixPath(value).is_absolute():
        raise ValueError("Absolute/empty archive member.")
    if len(value.split("/")) > 16:
        raise ValueError("Archive path depth exceeds bound.")
    for part in value.split("/"):
        if (part in ("", ".", "..") or len(part) > 200 or
                part != unicodedata.normalize("NFC", part) or
                part[-1] in " ." or any(unicodedata.category(c) == "Cc" for c in part) or
                any(c in '<>:"|?*' for c in part) or
                part.split(".", 1)[0].casefold() in RESERVED):
            raise ValueError("Nonportable archive member component.")
    return value


class VendorRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, archive: bool):
        self.archive = archive
        self.redirects: list[dict] = []

    def redirect_request(self, request, fp, code, message, headers, new_url):
        if len(self.redirects) >= 3:
            raise ValueError("Redirect bound exceeded.")
        validate_url(new_url, archive=self.archive)
        self.redirects.append({"from": request.full_url, "to": new_url, "code": code})
        return super().redirect_request(request, fp, code, message, headers, new_url)


def validate_url(value: str, *, archive: bool) -> None:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443) or
            parsed.fragment or (archive and value != ARCHIVE_URL)):
        raise ValueError("Download redirected outside the exact reviewed vendor scope.")


def download(url: str, maximum: int, deadline: float, target: Path | None = None):
    archive = url == ARCHIVE_URL
    validate_url(url, archive=archive)
    redirect = VendorRedirect(archive=archive)
    opener = urllib.request.build_opener(redirect)
    request = urllib.request.Request(url, headers={
        "User-Agent": "Resonance-Endpoint-Inspection/1",
        "Accept-Encoding": "identity",
    })
    output = target.open("xb") if target is not None else None
    blocks: list[bytes] = []
    count = 0
    digest = hashlib.sha256()
    try:
        check(deadline)
        with opener.open(request, timeout=min(30.0, deadline-time.monotonic())) as response:
            validate_url(response.url, archive=archive)
            if response.status != 200:
                raise ValueError("Vendor download was not HTTP 200.")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > maximum):
                raise ValueError("Vendor response length exceeds bound.")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Unexpected encoded vendor response.")
            while True:
                check(deadline)
                block = response.read(min(CHUNK, maximum + 1 - count))
                if not block:
                    break
                count += len(block)
                if count > maximum:
                    raise ValueError("Vendor response exceeds bound.")
                digest.update(block)
                if output is None:
                    blocks.append(block)
                else:
                    output.write(block)
            if length is not None and count != int(length):
                raise ValueError("Truncated vendor response.")
            metadata = {
                "requested_url": url, "final_url": response.url,
                "redirects": redirect.redirects, "size": count,
                "sha256": digest.hexdigest(),
                "content_type": response.headers.get("Content-Type"),
                "last_modified": response.headers.get("Last-Modified"),
                "etag": response.headers.get("ETag"),
                "retrieved_utc": utc(),
            }
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        return metadata, b"".join(blocks)
    finally:
        if output is not None:
            output.close()


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag.lower() in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def decoded(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16-bom"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-bom"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def document_evidence(raw: bytes, *, html: bool = False) -> dict:
    value, encoding = decoded(raw)
    if "\x00" in value:
        return {"status": "NOT_DECODED", "reason": "Unexpected NUL in document."}
    if html:
        parser = HTMLText()
        parser.feed(value)
        value = "".join(parser.parts)
    hits = []
    quote: list[str] = []
    for number, line in enumerate(value.splitlines(), 1):
        normalized = " ".join(line.split())
        matched = [word for word in KEYWORDS if word in normalized.casefold()]
        if not matched:
            continue
        if len(hits) < 64:
            hits.append({"line": number, "keywords": matched,
                         "line_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()})
        if len(quote) < 25:
            quote.extend(normalized.split()[:25-len(quote)])
    return {
        "status": "BOUNDED_TEXT_INSPECTION", "encoding": encoding,
        "keyword_hits": hits, "excerpt_maximum_25_words": " ".join(quote),
        "interpretation": "Lexical review candidates only; no inferred installer switches or legal permission.",
    }


def inf_evidence(raw: bytes) -> dict:
    value, encoding = decoded(raw)
    if "\x00" in value:
        raise ValueError("Malformed INF text.")
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in value.splitlines():
        # Data inspection only. This is not an INF interpreter or installer.
        quoted = False
        kept = []
        for char in raw_line:
            if char == '"':
                quoted = not quoted
            if char == ";" and not quoted:
                break
            kept.append(char)
        line = "".join(kept).strip()
        if not line:
            continue
        if len(line) > 4096:
            raise ValueError("INF line exceeds bound.")
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if len(sections) >= 256 and current not in sections:
                raise ValueError("INF section bound exceeded.")
            sections.setdefault(current, [])
        else:
            rows = sections.setdefault(current, [])
            if len(rows) >= 512:
                raise ValueError("INF section row bound exceeded.")
            rows.append(line)
    version = {}
    strings = {}
    for section, rows in sections.items():
        if section.casefold() == "version":
            for row in rows:
                if "=" in row:
                    key, value = row.split("=", 1)
                    version[key.strip()] = value.strip()
        if section.casefold() == "strings":
            for row in rows:
                if "=" in row:
                    key, value = row.split("=", 1)
                    strings[key.strip().casefold()] = value.strip().strip('"')
    catalogs = []
    for key, value in version.items():
        if key.casefold().startswith("catalogfile"):
            candidate = value.strip().strip('"')
            if candidate.startswith("%") and candidate.endswith("%"):
                candidate = strings.get(candidate[1:-1].casefold(), candidate)
            catalogs.append({"directive": key, "raw": value, "resolved_token": candidate})
    hardware = []
    sys_names = set()
    for section, rows in sections.items():
        for row in rows:
            if re.search(r"(?:ROOT|SWD|HDAUDIO|USB|PCI)\\", row, re.I):
                if len(hardware) >= 256:
                    raise ValueError("INF hardware ID bound exceeded.")
                hardware.append({"section": section, "row": row})
            sys_names.update(re.findall(r"(?i)([^\\/:;\"=\s,]+\.sys)\b", row))
    return {
        "encoding": encoding, "sections": list(sections), "version_fields": version,
        "version_rows": [row for section, rows in sections.items()
                         if section.casefold() == "version" for row in rows],
        "manufacturer_rows": next((rows for name, rows in sections.items()
                                   if name.casefold() == "manufacturer"), []),
        "catalog_directives": catalogs, "hardware_id_rows": hardware,
        "sys_basename_candidates": sorted(sys_names),
        "interpretation": "Lexical INF data only; decorations and membership do not prove Server/PnP installability.",
    }


def inspect_archive(path: Path, extraction: Path, deadline: float, report: dict) -> list[dict]:
    selected = []
    with zipfile.ZipFile(path, "r") as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_MEMBERS:
            raise ValueError("Archive member count outside bound.")
        explicit = set()
        nodes: dict[str, tuple[str, bool]] = {}
        expanded = 0
        signature_count = 0
        for member in members:
            check(deadline)
            name = portable_name(member.orig_filename)
            if member.filename != member.orig_filename:
                raise ValueError("Archive name truncation refused.")
            mode = member.external_attr >> 16
            directory = member.is_dir()
            if (member.flag_bits & 1 or member.compress_type not in
                    (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or
                    (member.external_attr & 0xFFFF) & 0x400):
                raise ValueError("Encrypted, linked, or unsupported archive member.")
            if directory and member.file_size:
                raise ValueError("Nonempty archive directory.")
            if member.file_size < 0 or member.file_size > MEMBER_LIMIT:
                raise ValueError("Archive member size outside bound.")
            expanded += member.file_size
            if expanded > EXPANDED_LIMIT:
                raise ValueError("Expanded archive size exceeds bound.")
            key = name.casefold()
            if key in explicit:
                raise ValueError("Duplicate/case-alias archive member.")
            explicit.add(key)
            parts = name.split("/")
            for index in range(1, len(parts) + 1):
                spelling = "/".join(parts[:index])
                is_directory = index < len(parts) or directory
                old = nodes.get(spelling.casefold())
                if old is not None and old != (spelling, is_directory):
                    raise ValueError("Archive ancestor/type/name collision.")
                nodes[spelling.casefold()] = (spelling, is_directory)
                if len(nodes) > 1024:
                    raise ValueError("Archive directory namespace exceeds bound.")
            if not directory and PurePosixPath(name).suffix.casefold() in SIGNATURE_EXTENSIONS:
                signature_count += 1
        if not 1 <= signature_count <= MAX_SIGNATURE_FILES:
            raise ValueError("Signature file count outside bound.")
        report["archive"]["declared_expanded_size"] = expanded
        report["archive"]["member_count"] = len(members)
        report["members"] = []
        report["package_documents"] = []
        report["inf"] = []
        for member in members:
            check(deadline)
            name = portable_name(member.filename)
            suffix = PurePosixPath(name).suffix.casefold()
            if member.is_dir():
                report["members"].append({"name": name, "kind": "directory", "size": 0})
                continue
            keep_text = suffix in TEXT_EXTENSIONS
            if keep_text and member.file_size > TEXT_LIMIT:
                raise ValueError("Package text/INF exceeds inspection bound.")
            target = extraction.joinpath(*name.split("/")) if suffix in SIGNATURE_EXTENSIONS else None
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
            stream_out = target.open("xb") if target is not None else None
            digest = hashlib.sha256()
            count = 0
            text_blocks = []
            try:
                with archive.open(member, "r") as stream:
                    while True:
                        check(deadline)
                        block = stream.read(min(CHUNK, member.file_size + 1 - count))
                        if not block:
                            break
                        count += len(block)
                        if count > member.file_size:
                            raise ValueError("Archive member exceeds declared size.")
                        digest.update(block)
                        if stream_out is not None:
                            stream_out.write(block)
                        if keep_text:
                            text_blocks.append(block)
                if count != member.file_size:
                    raise ValueError("Truncated archive member.")
                if stream_out is not None:
                    stream_out.flush()
                    os.fsync(stream_out.fileno())
            finally:
                if stream_out is not None:
                    stream_out.close()
            row = {"name": name, "kind": "file", "size": count,
                   "sha256": digest.hexdigest(), "crc32": f"{member.CRC:08x}",
                   "compressed_size": member.compress_size}
            report["members"].append(row)
            if target is not None:
                if digest_file(target, MEMBER_LIMIT, deadline) != row["sha256"]:
                    raise ValueError("Extracted signature member differs.")
                selected.append({**row, "path": str(target)})
            if keep_text:
                raw = b"".join(text_blocks)
                report["package_documents"].append({
                    "member": name, "sha256": row["sha256"],
                    **document_evidence(raw, html=suffix in (".html", ".htm")),
                })
                if suffix == ".inf":
                    report["inf"].append({"member": name, **inf_evidence(raw)})
            elif suffix == ".pdf":
                report["package_documents"].append({
                    "member": name, "sha256": row["sha256"], "size": count,
                    "status": "HASHED_ONLY_PDF_TEXT_NOT_DECODED",
                })
    return selected


def bounded_process(command: list[str], work: Path, tag: str, deadline: float,
                    *, timeout: float = 30.0, output_limit: int = OUTPUT_LIMIT) -> dict:
    check(deadline)
    stdout = work / (tag + ".stdout")
    stderr = work / (tag + ".stderr")
    start = time.monotonic()
    stop = min(deadline, start + timeout)
    process = None
    reason = None
    with stdout.open("xb") as out, stderr.open("xb") as err:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, shell=False,
                                       close_fds=True, cwd=work)
            while process.poll() is None:
                if time.monotonic() >= stop:
                    reason = "TIMEOUT"
                    break
                if stdout.stat().st_size + stderr.stat().st_size > output_limit:
                    reason = "OUTPUT_LIMIT"
                    break
                time.sleep(0.05)
            if reason is not None:
                process.kill()  # Popen's retained Windows process handle, never a bare PID.
            return_code = process.wait(timeout=10)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    if stdout.stat().st_size + stderr.stat().st_size > output_limit:
        reason = reason or "OUTPUT_LIMIT"
    def captured(path):
        with path.open("rb") as stream:
            raw = stream.read(output_limit + 1)
        # SignTool console encoding is recorded; replacement is explicit and bounded.
        return raw[:output_limit].decode("utf-8", errors="replace").replace(str(work), "<inspection-work>")
    return {"returncode": return_code, "termination_reason": reason,
            "seconds": round(time.monotonic() - start, 3),
            "stdout": captured(stdout), "stderr": captured(stderr),
            "output_decoding": "UTF-8 with replacement; original exit code is authoritative."}


def signature_checks(selected: list[dict], work: Path, deadline: float, report: dict) -> None:
    helper = Path(__file__).with_name("inspect_windows_signatures.ps1").absolute()
    checked_parents(helper)
    helper_sha = digest_file(helper, TEXT_LIMIT, deadline)
    powershell = Path(os.environ["ProgramFiles"]) / "PowerShell" / "7" / "pwsh.exe"
    checked_parents(powershell)
    plain(powershell)
    input_file = work / "signature-input.json"
    input_file.write_text(json.dumps({"schema": "resonance-vbcable-signature-input/1",
        "files": [{"member": row["name"], "path": row["path"]} for row in selected]},
        ensure_ascii=True), encoding="utf-8")
    metadata_file = work / "windows-metadata.json"
    result = bounded_process([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
        "-File", str(helper), "-InputFile", str(input_file), "-OutputFile", str(metadata_file)],
        work, "windows-metadata", deadline, timeout=150, output_limit=256 * 1024)
    report["metadata_helper"] = {"source_sha256": helper_sha, **result}
    if result["returncode"] != 0 or result["termination_reason"]:
        raise RuntimeError("Read-only Windows metadata helper did not complete.")
    if plain(metadata_file).st_size > 2 * 1024 * 1024:
        raise ValueError("Windows metadata exceeds bound.")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    report["windows"] = metadata
    if metadata.get("schema") != "resonance-windows-signature-metadata/1":
        raise ValueError("Unexpected Windows metadata schema.")
    if metadata.get("os", {}).get("product_type") not in (2, 3):
        raise RuntimeError("The observed OS is not Windows Server; Server policy inspection is incomplete.")
    signatures = metadata.get("authenticode", [])
    if (len(signatures) != len(selected) or
            {row["member"] for row in signatures} != {row["name"] for row in selected}):
        raise ValueError("Incomplete Authenticode member coverage.")
    tool = metadata.get("signtool")
    if not tool:
        raise RuntimeError("An existing valid Microsoft-signed x64 SDK SignTool was not found.")
    path = Path(tool["path"])
    sdk = Path(tool["sdk_root"])
    checked_parents(path)
    path.relative_to(sdk)
    if path.name.casefold() != "signtool.exe" or path.parent.name.casefold() != "x64":
        raise ValueError("Unexpected SDK tool path.")
    if digest_file(path, 64 * 1024 * 1024, deadline) != tool["sha256"]:
        raise ValueError("SDK SignTool changed after its Authenticode inspection.")
    version = metadata["os"]["version"].split(".")
    build = str(metadata["os"]["build_number"])
    if len(version) < 2 or not all(part.isdecimal() for part in (*version[:2], build)):
        raise ValueError("Actual Windows version cannot be bound.")
    os_target = f"2:{version[0]}.{version[1]}.{build}"
    report["signtool_os_target"] = os_target
    report["verification"] = []
    cats = [row for row in selected if Path(row["name"]).suffix.casefold() == ".cat"]
    members = [row for row in selected if Path(row["name"]).suffix.casefold() in (".inf", ".sys")]
    embedded = [row for row in selected if Path(row["name"]).suffix.casefold() in (".exe", ".sys", ".cat")]
    if not cats or not members or not any(Path(row["name"]).suffix.casefold() == ".exe" for row in selected):
        raise ValueError("Package lacks setup, catalog, or driver/INF inspection targets.")
    if len(embedded) + len(cats) * len(members) > MAX_VERIFICATIONS:
        raise ValueError("Catalog membership matrix exceeds verification bound.")
    checks = [(row, None) for row in embedded]
    checks.extend((member, catalog) for catalog in cats for member in members)
    for number, (member, catalog) in enumerate(checks, 1):
        check(deadline)
        if digest_file(path, 64 * 1024 * 1024, deadline) != tool["sha256"]:
            raise ValueError("SDK SignTool changed before use.")
        if digest_file(Path(member["path"]), MEMBER_LIMIT, deadline) != member["sha256"]:
            raise ValueError("Signature target changed before verification.")
        policy = "/pa" if Path(member["name"]).suffix.casefold() == ".exe" else "/kp"
        arguments = ["verify", policy, "/v", "/o", os_target]
        if catalog is None:
            arguments.append("/all")
        else:
            if digest_file(Path(catalog["path"]), MEMBER_LIMIT, deadline) != catalog["sha256"]:
                raise ValueError("Catalog changed before verification.")
            arguments += ["/c", catalog["path"]]
        arguments.append(member["path"])
        result = bounded_process([str(path), *arguments], work, f"verify-{number:03d}", deadline)
        report["verification"].append({
            "member": member["name"], "catalog": catalog["name"] if catalog else None,
            "policy": policy, "mode": "explicit-catalog-membership" if catalog else "embedded",
            "os_target": os_target, **result,
            "verdict": ("VERIFIED" if result["returncode"] == 0 and not result["termination_reason"]
                        else "WARNING" if result["returncode"] == 2 and not result["termination_reason"]
                        else "NOT_VERIFIED"),
        })
        if result["termination_reason"]:
            raise RuntimeError("A required signature inspection exceeded its bound.")
        if result["returncode"] not in (0, 1, 2):
            raise RuntimeError("SignTool did not return a documented inspection result.")
    for row in selected:
        if digest_file(Path(row["path"]), MEMBER_LIMIT, deadline) != row["sha256"]:
            raise ValueError("Package member changed during signature inspection.")
    report["catalog_matrix_notice"] = (
        "Every CAT is checked against every INF/SYS. Nonmatching architecture/catalog pairs may fail; "
        "no single success or all-pairs result establishes supported installation. Review INF mappings.")
    for inf in report["inf"]:
        inf["catalog_candidates"] = {}
        for directive in inf["catalog_directives"]:
            token = directive["resolved_token"].casefold()
            inf["catalog_candidates"][directive["directive"]] = [
                row["name"] for row in cats if PurePosixPath(row["name"]).name.casefold() == token]


def run(report_dir: Path, work: Path) -> int:
    deadline = time.monotonic() + DEADLINE_SECONDS
    started = utc()
    report = {
        "schema": "resonance-vbcable-pack46-inspection/1",
        "inspection_status": "INCOMPLETE",
        "started_utc": started, "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "inspection_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "archive": {"requested_url": ARCHIVE_URL, "identity_status": "TO_BE_MEASURED"},
        "references": list(REFERENCES),
        "reviewed_vendor_contract": {
            "review_date": "2026-11-27",
            "product_source": DOCUMENT_URLS[0],
            "installation": "Extract the package and run its setup as administrator.",
            "restart": "Vendor instructions require a restart after installation and uninstallation.",
            "unattended_switches": "NOT_ESTABLISHED; this inspector neither guesses nor executes them.",
            "server_support": "NOT_ESTABLISHED_BY_VENDOR_OS_LABEL; exact Server policy inspection is separate.",
            "license": "Evaluation guidance does not establish indefinite free professional CI or ephemeral-seat terms.",
            "manual": "The previously reviewed OCT2024 manual is not Pack46 member-identity evidence.",
        },
        "qualification": {
            "installation_approval": "NOT_APPROVED",
            "vendor_executable_or_driver_execution": "NOT_RUN",
            "driver_installation_or_loading": "NOT_RUN",
            "audio_endpoint_or_MCI_or_application_tests": "NOT_RUN",
            "server_HLK_or_product_support": "NOT_ESTABLISHED",
            "recurring_CI_license": "REQUIRES_REVIEW",
            "certificate_import_or_security_policy_changes": "NOT_PERFORMED",
            "binary_artifact_distribution": "NOT_PERFORMED",
        },
    }
    owned_work = False
    owned_report = False
    report_identity = None
    work_identity = None
    exit_code = 1
    try:
        if os.name != "nt":
            raise RuntimeError("This inspection requires the actual Windows runner.")
        runner_temp = Path(os.environ["RUNNER_TEMP"]).absolute()
        for path, kind in ((report_dir, "reports"), (work, "work")):
            checked_parents(path)
            if path.parent != runner_temp or not re.fullmatch(
                    rf"vbcable-pack46-{kind}-[0-9]+-[0-9]+", path.name):
                raise ValueError("Inspection paths must be unique direct children of RUNNER_TEMP.")
        if report_dir == work:
            raise ValueError("Work and report paths must be distinct.")
        report_dir.mkdir()
        owned_report = True
        report_stat = plain(report_dir, directory=True)
        report_identity = (report_stat.st_dev, report_stat.st_ino)
        work.mkdir()
        owned_work = True
        work_identity = (plain(work, directory=True).st_dev, plain(work, directory=True).st_ino)
        (work / ".inspection-owner.json").write_text(json.dumps({
            "schema": "resonance-vbcable-inspection-owner/1",
            "run_id": report["run_id"], "run_attempt": report["run_attempt"],
            "source_commit": report["source_commit"],
        }), encoding="utf-8")
        count = ctypes.WinDLL("winmm").waveOutGetNumDevs
        count.argtypes = []
        count.restype = ctypes.c_uint
        report["read_only_waveout_device_count"] = int(count())
        archive_path = work / "VBCABLE_Driver_Pack46.zip"
        metadata, _ = download(ARCHIVE_URL, ARCHIVE_LIMIT, deadline, archive_path)
        report["archive"] = {**metadata, "identity_status": "MEASURED_NOT_PREAPPROVED"}
        extraction = work / "members"
        extraction.mkdir()
        selected = inspect_archive(archive_path, extraction, deadline, report)
        report["official_documents"] = []
        for url in DOCUMENT_URLS:
            metadata, raw = download(url, TEXT_LIMIT, deadline)
            report["official_documents"].append({**metadata, **document_evidence(raw, html=True)})
        signature_checks(selected, work, deadline, report)
        if digest_file(archive_path, ARCHIVE_LIMIT, deadline) != report["archive"]["sha256"]:
            raise ValueError("Archive changed during inspection.")
        report["inspection_status"] = "COMPLETE_FINDINGS_REQUIRE_REVIEW"
        report["verification_summary"] = {
            name: sum(row["verdict"] == name for row in report["verification"])
            for name in ("VERIFIED", "WARNING", "NOT_VERIFIED")
        }
        exit_code = 0
    except Exception as error:
        report["error"] = {"type": type(error).__name__,
                          "message": str(error)[:2000].replace(str(work), "<inspection-work>"),
                          "traceback": traceback.format_exc()[-12000:].replace(str(work), "<inspection-work>")}
    finally:
        if owned_work:
            try:
                current = plain(work, directory=True)
                if (current.st_dev, current.st_ino) != work_identity:
                    raise ValueError("Owned inspection directory identity changed.")
                shutil.rmtree(work)
                report["temporary_payload_cleanup"] = "COMPLETE"
            except Exception as error:
                report["temporary_payload_cleanup"] = "FAILED"
                report["cleanup_error_type"] = type(error).__name__
                report["inspection_status"] = "INCOMPLETE_CLEANUP_FAILED"
                exit_code = 1
        report["finished_utc"] = utc()
        report["elapsed_seconds"] = round(time.monotonic() - (deadline - DEADLINE_SECONDS), 3)
        if owned_report:
            current_report = plain(report_dir, directory=True)
            if (current_report.st_dev, current_report.st_ino) != report_identity:
                raise ValueError("Owned report directory identity changed.")
            raw = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
            if len(raw) > REPORT_LIMIT:
                raise ValueError("Diagnostic report exceeded its fixed artifact bound.")
            with (report_dir / "inspection.json").open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            summary = (
                f"Inspection: {report['inspection_status']}\n"
                f"Source: {report.get('source_commit')}\n"
                f"Archive: {ARCHIVE_URL}\n"
                f"Measured SHA256: {report['archive'].get('sha256', 'NOT_MEASURED')}\n"
                f"Measured size: {report['archive'].get('size', 'NOT_MEASURED')}\n"
                "Vendor installation/execution, driver loading, endpoint and app tests: NOT_RUN\n"
                "A successful diagnostic job means inspection completed, not approval or audio qualification.\n"
                "Review all signatures, INF/Server support, license and reboot requirements before any next phase.\n"
            )
            with (report_dir / "SUMMARY.txt").open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(summary)
            sys.stdout.buffer.write(summary.encode("utf-8"))
            sys.stdout.buffer.flush()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    return run(args.report_dir.absolute(), args.work_dir.absolute())


if __name__ == "__main__":
    raise SystemExit(main())
