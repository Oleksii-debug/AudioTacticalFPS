"""Synthetic evidence: agreement between two PDF parsers does not attest glyphs."""
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import subprocess
import os
from carr_library.extract.ocr_runtime import discover_ocr_runtime, verify_ocr_bundle

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import pypdfium2 as pdfium
from reportlab.pdfgen.canvas import Canvas


def fixture(tampered):
    buf = BytesIO()
    canvas = Canvas(buf, pagesize=(300, 200), invariant=True)
    canvas.setFont('Helvetica', 24)
    canvas.drawString(20, 100, 'AAAA')
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(buf.getvalue()))
    if tampered:
        font = writer.pages[0]['/Resources']['/Font']['/F1'].get_object()
        stream = DecodedStreamObject()
        stream.set_data(b'''/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Synthetic def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
1 beginbfchar
<41> <005A>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end end''')
        font[NameObject('/ToUnicode')] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def inspect(data):
    native = PdfReader(BytesIO(data)).pages[0].extract_text()
    with pdfium.PdfDocument(data) as doc:
        with closing(doc[0]) as page:
            with closing(page.get_textpage()) as textpage:
                second = textpage.get_text_bounded()
            with closing(page.render(scale=2)) as bitmap:
                rendered = sha256(bitmap.to_pil().convert('RGB').tobytes()).hexdigest()
    return native, second, rendered


def test_both_parsers_accept_false_mapping():
    assert inspect(fixture(False))[:2] == ('AAAA\n', 'AAAA')
    assert inspect(fixture(True))[:2] == ('ZZZZ\n', 'ZZZZ')


def test_changed_mapping_does_not_change_rendered_pixels():
    good, bad = fixture(False), fixture(True)
    assert sha256(good).digest() != sha256(bad).digest()
    assert inspect(good)[2] == inspect(bad)[2]


def test_real_ocr_detects_contradiction(tmp_path):
    path = tmp_path / 'page.png'
    with pdfium.PdfDocument(fixture(True)) as doc:
        with closing(doc[0]) as page, closing(page.render(scale=4)) as bitmap:
            bitmap.to_pil().save(path)
    # Installed acceptance selects the sealed bundle rather than the earlier
    # source-test runtime retained in CARR_CI_OCR_DIR.
    runtime_root = os.environ.get('CARR_OCR_DIR') or os.environ.get('CARR_CI_OCR_DIR')
    runtime = verify_ocr_bundle(runtime_root) if runtime_root else discover_ocr_runtime('eng')
    assert runtime is not None, 'Install the verified local OCR runtime before required source tests.'
    result = subprocess.run(runtime.command([runtime.executable, str(path), 'stdout', '-l', 'eng', '--psm', '7']),
                            capture_output=True, check=True, timeout=20, cwd=runtime.root)
    runtime.assert_unchanged()
    assert result.stdout.decode('utf-8').strip() == 'AAAA'
    assert inspect(fixture(True))[0].strip() != result.stdout.decode('utf-8').strip()
