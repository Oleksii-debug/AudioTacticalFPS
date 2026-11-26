"""Internal clipping must not publish hidden native text as a visible candidate."""
from contextlib import closing
from hashlib import sha256

import pypdfium2 as pdfium
import pytest
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from carr_library.extract.page_ocr import extract_page


PREFIX = 'Visible railway prefix '
HIDDEN = 'HIDDEN INVENTED CLAIM'


def write_page(path, *, hidden=False, clipped=False):
    canvas = Canvas(str(path), pagesize=(600, 300))
    if clipped:
        clip = canvas.beginPath()
        clip.rect(0, 0, 30 + stringWidth(PREFIX, 'Helvetica', 12), 300)
        canvas.clipPath(clip, stroke=0, fill=0)
    canvas.drawString(30, 200, PREFIX + (HIDDEN if hidden else ''))
    canvas.showPage()
    canvas.save()


def rendered_bytes(path):
    with pdfium.PdfDocument(path) as document, closing(document[0]) as page:
        with closing(page.render(scale=2)) as bitmap:
            return bitmap.to_pil().convert('RGB').tobytes()


def test_internal_clip_hides_suffix_even_when_object_bounds_stay_inside_page(tmp_path):
    baseline, source = tmp_path / 'visible.pdf', tmp_path / 'clipped.pdf'
    write_page(baseline, clipped=True)
    write_page(source, hidden=True, clipped=True)
    original = source.read_bytes()
    # The suffix is absent from the rendered page, not merely low-confidence OCR.
    assert rendered_bytes(source) == rendered_bytes(baseline)
    result = extract_page(source, 1)
    assert result['status'] == 'review_required'
    assert result['warnings'] == ['native_clip_path_unverified']
    assert result['provenance']['method'] == 'native_visibility_review'
    assert result['raw_text'] == PREFIX + HIDDEN + '\n'
    assert result['provenance']['native_text'] == result['raw_text']
    assert result['physical_index'] == 1
    assert result['printed_label'] is None and not result['printed_label_verified']
    assert result['provenance']['asset_sha256'] == sha256(original).hexdigest()
    assert source.read_bytes() == original


def test_ordinary_visible_text_remains_a_native_candidate(tmp_path):
    source = tmp_path / 'ordinary.pdf'
    write_page(source, hidden=True)
    result = extract_page(source, 1)
    assert result['status'] == 'candidate'
    assert result['provenance']['method'] == 'native'
    assert result['raw_text'] == PREFIX + HIDDEN + '\n'
    assert result['warnings'] == []


@pytest.mark.parametrize('api', ['FPDFPageObj_GetClipPath', 'FPDFClipPath_CountPaths'])
def test_missing_clip_api_requires_review_instead_of_trusting_native_text(tmp_path, monkeypatch, api):
    source = tmp_path / 'clipped.pdf'
    write_page(source, hidden=True, clipped=True)
    monkeypatch.delattr(pdfium.raw, api)
    result = extract_page(source, 1)
    assert result['status'] == 'review_required'
    assert result['warnings'] == ['native_visibility_check_failed']
    assert result['raw_text'] == PREFIX + HIDDEN + '\n'
