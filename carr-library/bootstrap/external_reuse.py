"""Integrate reviewed upstream extractors into the research library candidate.
Non-paginated formats get no invented printed page labels. PDFs keep the existing
page-preserving pypdf path; whole-document OCR is not a safe citation authority.
Only explicitly supplied, trusted local files. No network or subprocess.
"""
from pathlib import Path
from zipfile import ZipFile
import tempfile
from .vendor.cairn_extractors.hasher import hash_file
from .vendor.cairn_extractors.docx_extractor import DocxExtractor
from .vendor.cairn_extractors.html_extractor import HtmlExtractor
from .vendor.cairn_extractors.txt_extractor import TxtExtractor
from .vendor.cairn_extractors.csv_extractor import CsvExtractor
from .vendor.cairn_extractors.xlsx_extractor import XlsxExtractor

MAX_BYTES = 64 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
MAX_ENTRIES = 10000
EXTRACTORS = {
    ".docx": DocxExtractor, ".html": HtmlExtractor, ".htm": HtmlExtractor,
    ".xhtml": HtmlExtractor, ".md": TxtExtractor, ".rst": TxtExtractor,
    ".csv": CsvExtractor, ".tsv": CsvExtractor, ".xlsx": XlsxExtractor,
}

def _check_zip(path):
    with ZipFile(path) as archive:
        info = archive.infolist()
        if len(info) > MAX_ENTRIES or sum(x.file_size for x in info) > MAX_UNPACKED:
            raise ValueError("oversized office archive")
        if any(x.flag_bits & 1 for x in info):
            raise ValueError("encrypted office archive")
        for member in info:
            name = member.filename.replace(chr(92), "/")
            if name.startswith("/") or ".." in name.split("/") or ":" in name:
                raise ValueError("unsafe archive member")

def import_external(library, workspace, path, title, author, edition):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("invalid or oversized source")
    suffix = path.suffix.lower()
    if suffix in (".pdf", ".txt"):
        return library.import_file(workspace, path, title, author, edition)
    extractor = EXTRACTORS.get(suffix)
    if extractor is None:
        raise ValueError("unsupported source format")
    # Read a bounded snapshot, use the same immutable bytes for hash and extraction.
    with path.open("rb") as file:
        payload = file.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("oversized source")
    with tempfile.TemporaryDirectory() as folder:
        snapshot = Path(folder) / ("input" + suffix)
        snapshot.write_bytes(payload)
        if suffix in (".docx", ".xlsx"):
            _check_zip(snapshot)
        if suffix in (".html", ".htm", ".xhtml", ".md", ".rst", ".csv", ".tsv"):
            payload.decode("utf-8-sig", errors="strict")
        result = extractor().extract(snapshot)
        if not result.success or not result.text or not result.text.strip():
            raise ValueError("source extraction failed or returned no text")
        if "\ufffd" in result.text:
            raise ValueError("uncertain decoding")
        # Single logical text segment, not physical pagination. No printed label.
        return library.add_book(workspace, title, author, edition, [(None, result.text)], hash_file(snapshot))
