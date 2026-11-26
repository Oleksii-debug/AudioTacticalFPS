from pathlib import Path
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from carr_library.extract.page_ocr import Config, extract_page


@pytest.fixture
def mixed_pdf(tmp_path):
    path = tmp_path / 'mixed.pdf'
    c = Canvas(str(path), pagesize=(612, 792))
    c.drawString(72, 700, 'Native railway')
    c.showPage()
    image = Image.new('RGB', (1200, 300), 'white')
    font = ImageFont.truetype('DejaVuSans.ttf', 72)
    ImageDraw.Draw(image).text((30, 70), 'Railway bridge', font=font, fill='black')
    c.drawImage(ImageReader(image), 50, 500, 500, 125)
    c.showPage()
    c.showPage()
    c.drawString(72, 700, 'Native header')
    c.drawImage(ImageReader(image), 50, 500, 500, 125)
    c.showPage()
    c.save()
    return path


def test_native_and_blank(mixed_pdf):
    r = extract_page(mixed_pdf, 1)
    assert r['raw_text'] == 'Native railway\n'
    assert r['physical_index'] == 1 and r['printed_label'] is None
    assert r['provenance']['method'] == 'native'
    assert extract_page(mixed_pdf, 3)['status'] == 'blank'
    with pytest.raises(ValueError):
        extract_page(mixed_pdf, 0)


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local engine unavailable')
def test_real_ocr_and_mixed(mixed_pdf):
    r = extract_page(mixed_pdf, 2)
    assert r['raw_text'].strip() == 'Railway bridge'
    assert r['status'] == 'review_required' and r['blocks']
    assert r['provenance']['ocr_tsv'] and r['printed_label'] is None
    assert all(b['bbox'][2] > 0 and b['bbox'][3] > 0 for b in r['blocks'])
    m = extract_page(mixed_pdf, 4)
    assert 'mixed_native_and_image' in m['warnings']
    assert 'Native header' in m['raw_text']
    assert 'Railway bridge' in m['provenance']['ocr_text']


def test_missing_engine_and_limits(mixed_pdf, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    r = extract_page(mixed_pdf, 2)
    assert r['status'] == 'review_required' and 'ocr_engine_missing' in r['warnings']
    assert 'render_limit' in extract_page(mixed_pdf, 2, Config(max_pixels=1))['warnings']
    with pytest.raises(ValueError):
        Config(languages='eng --psm 1')


def test_ocr_timeout(mixed_pdf, monkeypatch):
    import subprocess
    monkeypatch.setattr(shutil, 'which', lambda _: 'tesseract')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('tesseract', 1)
    monkeypatch.setattr(subprocess, 'run', timeout)
    r = extract_page(mixed_pdf, 2)
    assert r['status'] == 'review_required' and 'ocr_failed_or_timed_out' in r['warnings']


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local engine unavailable')
def test_ukrainian_scan(tmp_path):
    import subprocess
    langs = subprocess.check_output(['tesseract', '--list-langs'], text=True).splitlines()
    if 'ukr' not in langs:
        pytest.skip('Ukrainian language data unavailable')
    p = tmp_path / 'ukr.pdf'
    image = Image.new('RGB', (1200, 300), 'white')
    font = ImageFont.truetype('DejaVuSans.ttf', 72)
    ImageDraw.Draw(image).text((30, 70), 'Міст через річку', font=font, fill='black')
    c = Canvas(str(p), pagesize=(612, 792))
    c.drawImage(ImageReader(image), 50, 500, 500, 125)
    c.showPage()
    c.save()
    r = extract_page(p, 1, Config(languages='ukr'))
    assert r['raw_text'].strip() == 'Міст через річку'
    assert r['printed_label'] is None and r['status'] == 'review_required'
