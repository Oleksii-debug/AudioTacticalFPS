"""Regression for CARR-QA-005; synthetic documents only."""
from hashlib import sha256

import pytest
from reportlab.pdfgen.canvas import Canvas
from carr_library.extract.page_ocr import extract_page


@pytest.mark.parametrize('variant', ['invisible', 'outside_page', 'mixed_hidden', 'white'])
def test_contradictory_native_layer(tmp_path, variant):
    path = tmp_path / 'synthetic.pdf'
    c = Canvas(str(path), pagesize=(300, 300))
    if variant == 'mixed_hidden':
        c.drawString(30, 250, 'Visible source')
    text = c.beginText(500 if variant == 'outside_page' else 30, 200)
    if variant in ('invisible', 'mixed_hidden'):
        text.setTextRenderMode(3)
    elif variant == 'white':
        text.setFillColorRGB(1, 1, 1)
    text.textLine('Synthetic unprinted claim')
    c.drawText(text)
    c.showPage()
    c.save()
    result = extract_page(path, 1)
    assert result['status'] == 'review_required'
    assert result['provenance'].get('method') != 'native'
    assert 'Synthetic unprinted claim' in result['provenance']['native_text']
    assert result['provenance']['asset_sha256'] == sha256(path.read_bytes()).hexdigest()
    assert not result['printed_label_verified']


def test_visible_native_stays_exact(tmp_path):
    path = tmp_path / 'visible.pdf'
    c = Canvas(str(path), pagesize=(300, 300))
    c.drawString(30, 200, 'Visible source')
    c.showPage()
    c.save()
    result = extract_page(path, 1)
    assert result['status'] == 'candidate'
    assert result['provenance']['method'] == 'native'
    assert result['raw_text'] == 'Visible source\n'
