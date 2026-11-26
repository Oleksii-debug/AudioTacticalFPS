from dataclasses import FrozenInstanceError
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from docx import Document

from carr_library.catalog.metadata_preview import MetadataUnavailable, preview_metadata

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def snapshot(data, mime):
    return SimpleNamespace(data=data, mime=mime, size=len(data), digest=sha256(data).hexdigest())


def pdf(metadata=None, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if metadata:
        writer.add_metadata(metadata)
    if encrypted:
        writer.encrypt("synthetic password")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def docx(title, author, language):
    document = Document()
    document.core_properties.title = title
    document.core_properties.author = author
    document.core_properties.language = language
    document.add_paragraph("Synthetic fixture")
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def test_same_title_different_authors_and_sources():
    a = snapshot(pdf({"/Title": "Дослідження", "/Author": "Автор А"}), PDF)
    b = snapshot(pdf({"/Title": "Дослідження", "/Author": "Autor B"}), PDF)
    p, q = preview_metadata(a), preview_metadata(b)
    assert p.title == q.title == "Дослідження"
    assert (p.author, q.author) == ("Автор А", "Autor B")
    assert p.asset_sha256 == a.digest and q.asset_sha256 == b.digest
    assert p.asset_sha256 != q.asset_sha256
    assert p.language is q.language is None
    assert p.verification == q.verification == "unverified"
    with pytest.raises(FrozenInstanceError):
        p.verification = "verified"


def test_missing_metadata_is_not_inferred():
    result = preview_metadata(snapshot(pdf(), PDF))
    assert result.title is result.author is result.language is None
    assert result.verification == "unverified"


def test_docx_unicode_hints_and_original_bytes():
    asset = snapshot(docx("Výskum", "Автор", "uk-UA"), DOCX)
    before = asset.data
    result = preview_metadata(asset)
    assert (result.title, result.author, result.language) == ("Výskum", "Автор", "uk-UA")
    assert result.origin == "docx_core_properties"
    assert result.asset_sha256 == asset.digest and asset.data == before
    assert result.verification == "unverified"


@pytest.mark.parametrize("title", ["   ", "a\nb", "x" * 4097])
def test_unsafe_or_empty_hint_is_unknown(title):
    result = preview_metadata(snapshot(pdf({"/Title": title}), PDF))
    assert result.title is None


@pytest.mark.parametrize("mime,data", [(PDF, b"damaged PDF"), (DOCX, b"damaged ZIP"), (PDF, pdf(encrypted=True)), ("text/plain", b"text")])
def test_reject_unsupported_damaged_encrypted(mime, data):
    with pytest.raises(MetadataUnavailable):
        preview_metadata(snapshot(data, mime))


@pytest.mark.parametrize("field,value", [("digest", "0" * 64), ("size", 1), ("data", bytearray(b"x"))])
def test_snapshot_integrity(field, value):
    asset = snapshot(pdf(), PDF)
    setattr(asset, field, value)
    with pytest.raises(MetadataUnavailable, match="invalid asset snapshot"):
        preview_metadata(asset)
