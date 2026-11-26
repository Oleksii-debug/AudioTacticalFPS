"""QA-005 residual regressions using synthetic PDF pages and real PDFium."""
from contextlib import closing
from hashlib import sha256

import pytest
import pypdfium2 as pdfium
from reportlab.pdfgen.canvas import Canvas
from carr_library.extract.page_ocr import Config, extract_page


@pytest.mark.parametrize('variant', ['white', 'covered'])
def test_render_equivalent_to_visible_only(tmp_path, variant):
    def create(path, extra):
        c = Canvas(str(path), pagesize=(300, 300))
        c.drawString(30, 250, 'Visible source')
        if extra:
            if variant == 'white':
                c.setFillColorRGB(1, 1, 1)
            c.drawString(30, 100, 'Synthetic unprinted claim')
            if variant == 'covered':
                c.setFillColorRGB(1, 1, 1)
                c.rect(20, 80, 260, 40, fill=1, stroke=0)
        c.showPage()
        c.save()

    def pixels(path):
        with pdfium.PdfDocument(path) as doc, closing(doc[0]) as page, closing(page.render(scale=2)) as bitmap:
            return bitmap.to_pil().convert('RGB').tobytes()

    baseline, path = tmp_path/'visible.pdf', tmp_path/'variant.pdf'
    create(baseline, False)
    create(path, True)
    before = path.read_bytes()
    assert pixels(baseline) == pixels(path)
    result = extract_page(path, 1)
    assert 'Synthetic unprinted claim' in result['raw_text']
    assert result['status'] == 'review_required'
    assert result['provenance']['method'] != 'native'
    assert result['provenance']['asset_sha256'] == sha256(before).hexdigest()
    assert result['physical_index'] == 1 and not result['printed_label_verified']
    assert path.read_bytes() == before


def test_multiple_visible_objects(tmp_path):
    path = tmp_path/'two.pdf'
    c = Canvas(str(path), pagesize=(300, 300))
    c.drawString(30, 250, 'Visible source')
    c.drawString(30, 100, 'Second visible line')
    c.showPage()
    c.save()
    r = extract_page(path, 1)
    assert r['status'] == 'candidate'
    assert r['raw_text'] == 'Visible source\nSecond visible line\n'


def test_visibility_work_budget(tmp_path):
    path = tmp_path/'budget.pdf'
    c = Canvas(str(path), pagesize=(300, 300))
    for i in range(130):
        c.drawString(30, 250, 'Visible source %d' % i)
    c.showPage()
    c.save()
    r = extract_page(path, 1, Config())
    assert r['status'] == 'review_required'
    assert 'native_visibility_work_limit' in r['warnings']
