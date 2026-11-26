"""Source text and block fidelity through actual PDFium/Tesseract extraction."""
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from carr_library.extract.page_ocr import extract_page


def scanned_page(tmp_path, text, *, color="black"):
    image = Image.new("RGB", (1200, 300), "white")
    font = ImageFont.truetype("DejaVuSans.ttf", 72)
    ImageDraw.Draw(image).text((30, 70), text, font=font, fill=color)
    path = tmp_path / "scan.pdf"
    canvas = Canvas(str(path), pagesize=(612, 792))
    canvas.drawImage(ImageReader(image), 50, 500, 500, 125)
    canvas.showPage()
    canvas.save()
    return path


@pytest.mark.skipif(not shutil.which("tesseract"), reason="local engine unavailable")
@pytest.mark.parametrize("text, words", [
    ('"Railway" bridge', ['"Railway"', 'bridge']),
    ('" railway bridge', ['"', 'railway', 'bridge']),
])
def test_literal_quotes_do_not_consume_ocr_rows(tmp_path, text, words):
    path = scanned_page(tmp_path, text)
    original = path.read_bytes()
    result = extract_page(path, 1)
    assert result["status"] == "review_required"
    assert result["raw_text"] == text + "\n"
    assert [block["text"] for block in result["blocks"]] == words
    assert all("\t" not in block["text"] for block in result["blocks"])
    assert result["provenance"]["asset_sha256"] == sha256(original).hexdigest()
    assert result["physical_index"] == 1 and result["printed_label"] is None
    assert path.read_bytes() == original


def test_faint_scan_is_not_published_as_a_blank_page(tmp_path):
    path = scanned_page(tmp_path, "Pale railway bridge", color=(250, 250, 250))
    result = extract_page(path, 1)
    assert result["provenance"]["native_text"] == ""
    assert result["status"] == "review_required"
    assert result["provenance"].get("method") != "render_blank"
    assert not result["printed_label_verified"]


TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


@pytest.mark.parametrize("invalid_row", [
    "5\t1\t1\t1\t1\t2\t-1\t20\t100\t30\t90\tbridge\n",
    "5\t1\t1\t1\t1\t2\t20\t20\t999999\t30\t90\tbridge\n",
    "5\t1\t1\t1\t1\t2\t20\t20\t100\t30\tnan\tbridge\n",
    "5\t1\t1\t1\t1\t2\t20\t20\t100\t30\t90\n",
    pytest.param("5\t1\t1\t1\t1\t2\t20\t20\t100\t30\t90\t" + "x" * 200_000 + "\n",
                 id="oversized-tsv-field"),
])
def test_invalid_layout_preserves_raw_text_without_partial_blocks(tmp_path, monkeypatch, invalid_row):
    path = scanned_page(tmp_path, "Railway bridge")
    tsv = TSV_HEADER + "5\t1\t1\t1\t1\t1\t20\t20\t100\t30\t90\tRailway\n" + invalid_row
    monkeypatch.setattr(shutil, "which", lambda _: "tesseract")

    def output(args, **kwargs):
        if "--version" in args:
            return SimpleNamespace(stdout=b"tesseract 5.5.0\n")
        base = Path(args[2])
        base.with_suffix(".txt").write_bytes(b"Railway bridge\n")
        base.with_suffix(".tsv").write_bytes(tsv.encode("utf-8"))
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(subprocess, "run", output)
    result = extract_page(path, 1)
    assert result["status"] == "review_required"
    assert result["raw_text"] == result["provenance"]["ocr_text"] == "Railway bridge\n"
    assert result["provenance"]["ocr_tsv"] == tsv
    assert "ocr_layout_invalid" in result["warnings"]
    assert result["blocks"] == []


def test_excessive_ocr_output_is_quarantined(tmp_path, monkeypatch):
    path = scanned_page(tmp_path, "Railway bridge")
    monkeypatch.setattr(shutil, "which", lambda _: "tesseract")

    def output(args, **kwargs):
        if "--version" in args:
            return SimpleNamespace(stdout=b"tesseract 5.5.0\n")
        base = Path(args[2])
        base.with_suffix(".txt").write_bytes(b"Railway bridge\n")
        with base.with_suffix(".tsv").open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(subprocess, "run", output)
    result = extract_page(path, 1)
    assert result["status"] == "review_required"
    assert "ocr_output_limit" in result["warnings"]
    assert result["blocks"] == []
