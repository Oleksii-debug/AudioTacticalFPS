"""Real PDFium regression fixtures: partial visibility is not verification."""
from contextlib import closing
from hashlib import sha256

import pypdfium2 as pdfium
import pytest
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from carr_library.extract.page_ocr import extract_page


def pixels(path):
    with pdfium.PdfDocument(path) as doc, closing(doc[0]) as page, closing(page.render(scale=2)) as bmp:
        return bmp.to_pil().convert('RGB').tobytes()


@pytest.mark.parametrize('variant', ['partial_cover', 'partial_clip'])
def test_hidden_suffix(tmp_path, variant):
    prefix = 'Visible prefix '
    hidden = 'HIDDEN CLAIM'
    start = 30 if variant == 'partial_cover' else 300 - stringWidth(prefix, 'Helvetica', 12) + 3
    edge = start + stringWidth(prefix, 'Helvetica', 12)
    def create(path, extra):
        c = Canvas(str(path), pagesize=(300, 300))
        c.drawString(start, 200, prefix + (hidden if extra else ''))
        if variant == 'partial_cover':
            c.setFillColorRGB(1, 1, 1)
            c.rect(edge, 190, 300-edge, 25, fill=1, stroke=0)
        c.showPage(); c.save()
    baseline, path = tmp_path/'baseline.pdf', tmp_path/'test.pdf'
    create(baseline, False); create(path, True)
    assert sha256(pixels(baseline)).digest() == sha256(pixels(path)).digest()
    original = sha256(path.read_bytes()).hexdigest()
    result = extract_page(path, 1)
    assert hidden in result['raw_text']
    assert result['status'] == 'review_required', result['warnings']
    assert result['provenance']['asset_sha256'] == original
    assert sha256(path.read_bytes()).hexdigest() == original
    assert result['physical_index'] == 1 and result['printed_label'] is None


@pytest.mark.parametrize('variant', ['ordinary', 'background', 'later_disjoint'])
def test_visible_control(tmp_path, variant):
    path = tmp_path/'visible.pdf'
    c = Canvas(str(path), pagesize=(300, 300))
    if variant == 'background':
        c.setFillColorRGB(.9, .9, .9)
        c.rect(10, 180, 250, 50, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
    c.drawString(30, 200, 'Visible source')
    if variant == 'later_disjoint':
        c.rect(10, 10, 250, 50, fill=1, stroke=0)
    c.showPage(); c.save()
    result = extract_page(path, 1)
    assert result['status'] == 'candidate', result['warnings']
    assert result['raw_text'] == 'Visible source\n'


def test_object_budget(tmp_path):
    path = tmp_path/'complex.pdf'
    c = Canvas(str(path), pagesize=(300, 300))
    c.drawString(30, 200, 'Visible source')
    for i in range(260):
        c.rect(10, 10, 1, 1, fill=1, stroke=0)
    c.showPage(); c.save()
    result = extract_page(path, 1)
    assert result['status'] == 'review_required'
