"""External extraction integration tests on generated fixtures only."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from docx import Document
from openpyxl import Workbook
from .universal_library import ResearchLibrary
from .external_reuse import import_external

def test_reused_extractors_and_evidence():
    with TemporaryDirectory() as folder:
        root = Path(folder)
        lib = ResearchLibrary(root / "test.db")
        w = lib.workspace("Research")
        other = lib.workspace("Other")
        topic = lib.topic(w, "Evidence")
        doc = Document()
        doc.add_paragraph("The railway connects two towns.")
        doc.save(root / "book.docx")
        (root / "page.html").write_text("<p>The railway crosses the river.</p><script>forbidden</script>", encoding="utf-8")
        (root / "notes.md").write_text("The railway is important.", encoding="utf-8")
        (root / "data.csv").write_text("railway,bridge\n", encoding="utf-8")
        wb = Workbook()
        wb.active["A1"] = "railway station"
        wb.save(root / "data.xlsx")
        for path in [root / p for p in ("book.docx", "page.html", "notes.md", "data.csv", "data.xlsx")]:
            import_external(lib, w, path, path.name, "Fixture author", "1")
        hits = lib.search(w, "railway")
        assert len(hits) == 5
        assert not lib.search(other, "railway")
        assert not lib.search(w, "forbidden")
        for hit in hits:
            assert hit["printed"] is None
            assert hashlib.sha256((root / hit["title"]).read_bytes()).hexdigest() == hit["source_digest"]
            lib.save(w, topic, hit, "fixture")
        assert "railway" in lib.export_topic(w, topic)
        lib.close()

def test_invalid_inputs():
    with TemporaryDirectory() as folder:
        root = Path(folder)
        lib = ResearchLibrary(root / "test.db")
        w = lib.workspace("Research")
        for name, payload in [("bad.docx", b"invalid"), ("bad.md", b"\xff"), ("bad.exe", b"no")]:
            path = root / name
            path.write_bytes(payload)
            with pytest.raises((ValueError, UnicodeError, OSError, __import__("zipfile").BadZipFile)):
                import_external(lib, w, path, name, "Fixture author", "1")
        assert not lib.search(w, "invalid")
        lib.close()
