"""Catalog hints from the existing PDF/DOCX parsers, never verified metadata.

Pass only a bounded immutable intake snapshot, inside the approved parser
sandbox (or a trusted synthetic test). Never auto-import untrusted documents.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO


class MetadataUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class MetadataPreview:
    asset_sha256: str
    origin: str
    title: str | None
    author: str | None
    language: str | None
    verification: str = field(default="unverified", init=False)


def _hint(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return str(value)


def preview_metadata(snapshot):
    """Read embedded hints only; never assign identity, rights or printed labels.

    Uses the PR20 AssetSnapshot interface without reopening a filesystem path.
    The adapter does not provide a parser sandbox; production must supply one.
    """
    data = snapshot.data
    if (not isinstance(data, bytes) or not 0 < len(data) <= 64 * 1024 * 1024
            or snapshot.size != len(data) or sha256(data).hexdigest() != snapshot.digest):
        raise MetadataUnavailable("invalid asset snapshot")
    if snapshot.mime not in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        raise MetadataUnavailable("metadata preview unsupported for this format")
    try:
        if snapshot.mime == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise MetadataUnavailable("encrypted PDF")
            info = reader.metadata or {}
            origin = "pdf_document_info"
            values = (info.get("/Title"), info.get("/Author"), None)
        else:
            from docx import Document
            info = Document(BytesIO(data)).core_properties
            origin = "docx_core_properties"
            values = (info.title, info.author, info.language)
    except MetadataUnavailable:
        raise
    except Exception:
        # Do not expose original document data or parser exception paths.
        raise MetadataUnavailable("embedded metadata could not be read") from None
    return MetadataPreview(snapshot.digest, origin, *map(_hint, values))
