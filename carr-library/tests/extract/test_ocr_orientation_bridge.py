from hashlib import sha256
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont
from carr_library.extract.page_ocr import Config, extract_page
from carr_library.extract import ocr_orientation_bridge as bridge
from carr_library.extract.orientation import Orientation

TEXT = ('The archive contains letters and historical notes.\n'
        'Researchers compare editions and original pages.\n'
        'A railway bridge connects the eastern villages.\n'
        'Every quotation retains its original punctuation.\n'
        'The library contains independent collections.\n'
        'A researcher can review the evidence carefully.\n'
        'Printed numbers may differ from physical pages.\n'
        'Documents are preserved in their original form.\n'
        'The source provides evidence for each statement.\n'
        'Different authors may use the same expression.')


@pytest.fixture
def rotated_pdf(tmp_path):
    pages = []
    for angle in (90, 180, 270):
        im = Image.new('RGB', (1800, 1150), 'white')
        ImageDraw.Draw(im).multiline_text((60, 60), TEXT+f'\nPhysical marker page {len(pages)+1}.',
                                         font=ImageFont.truetype('DejaVuSans.ttf', 48), fill='black', spacing=24)
        pages.append(im.rotate(angle, expand=True))
    path = tmp_path/'scans.pdf'
    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=240)
    return path


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local Tesseract missing')
@pytest.mark.parametrize('index,angle', [(1, 90), (2, 180), (3, 270)])
def test_actual_pdf_osd_ocr(rotated_pdf, index, angle):
    pytest.importorskip('pytesseract')
    distance = pytest.importorskip('rapidfuzz.distance.Levenshtein').distance
    original = sha256(rotated_pdf.read_bytes()).hexdigest()
    baseline = extract_page(rotated_pdf, index, Config(dpi=240))
    result = extract_page(rotated_pdf, index, Config(dpi=240, orientation=True))
    p = result['provenance']; o = p['orientation']
    assert result['physical_index'] == index and result['printed_label'] is None
    assert result['status'] == 'review_required' and not result['printed_label_verified']
    assert o['status'] == 'ocr_candidate' and o['clockwise'] == angle
    assert result['raw_text'] == baseline['raw_text'] and result['blocks'] == baseline['blocks']
    assert p['ocr_text'] == baseline['provenance']['ocr_text']
    assert o['source_size'] == p['render_size'] and o['dpi'] == 240
    assert o['transformed_size'] == (o['source_size'][::-1] if angle in (90, 270) else o['source_size'])
    assert o['coordinate_frame'] != p['ocr_coordinate_frame'] and o['raw_tsv']
    assert o['source_rgb_sha256'] != o['transformed_rgb_sha256']
    assert sha256(rotated_pdf.read_bytes()).hexdigest() == original == p['asset_sha256']
    expected = ' '.join((TEXT+f'\nPhysical marker page {index}.').split())
    cer = distance(expected, ' '.join(o['raw_text'].split())) / len(expected)
    print('page', index, 'angle', angle, 'CER', cer, 'confidence', o['confidence'])
    assert cer <= .01 and f'Physical marker page {index}.' in o['raw_text']


@pytest.mark.parametrize('status', ['uncertain', 'osd_failed_or_timed_out'])
def test_no_guess(status, monkeypatch):
    pytest.importorskip('pytesseract')
    monkeypatch.setattr(bridge.shutil, 'which', lambda _: '/test/tesseract')
    monkeypatch.setattr(bridge, 'detect_orientation', lambda *a, **k: Orientation(status, raw_osd='evidence'))
    def unused(*args):
        pytest.fail('no rotated OCR expected')
    o = bridge.orientation_candidate(Image.new('RGB', (20, 10)), Config(dpi=240, orientation=True),
                                     '/test/tesseract', lambda: 1, unused, unused)
    assert o['status'] == status and o['raw_osd'] == 'evidence' and 'raw_text' not in o


def test_exhausted_budget(monkeypatch):
    pytest.importorskip('pytesseract')
    monkeypatch.setattr(bridge.shutil, 'which', lambda _: '/test/tesseract')
    def exhausted():
        raise subprocess.TimeoutExpired('tesseract', 1)
    def unused(*args):
        pytest.fail('no process expected')
    o = bridge.orientation_candidate(Image.new('RGB', (20, 10)), Config(orientation=True),
                                     '/test/tesseract', exhausted, unused, unused)
    assert o['status'] == 'orientation_failed_or_timed_out' and 'raw_text' not in o


def test_config_opt_in():
    assert Config().orientation is False
    with pytest.raises(ValueError):
        Config(orientation='yes')


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local Tesseract missing')
def test_page_fallback_retains_baseline(rotated_pdf, monkeypatch):
    pytest.importorskip('pytesseract')
    baseline = extract_page(rotated_pdf, 1, Config(dpi=240))
    monkeypatch.setattr(bridge, 'detect_orientation', lambda *a, **k: Orientation('engine_missing'))
    result = extract_page(rotated_pdf, 1, Config(dpi=240, orientation=True))
    assert result['provenance']['orientation']['status'] == 'engine_missing'
    assert result['raw_text'] == baseline['raw_text'] and result['blocks'] == baseline['blocks']
    assert result['provenance']['config']['orientation'] is True
    assert result['status'] == 'review_required'


def test_second_ocr_timeout_retains_osd(monkeypatch):
    pytest.importorskip('pytesseract')
    monkeypatch.setattr(bridge.shutil, 'which', lambda _: '/test/tesseract')
    monkeypatch.setattr(bridge, 'detect_orientation', lambda *a, **k: Orientation('candidate', 90, 20, 'evidence'))
    def timeout(*args):
        raise subprocess.TimeoutExpired('tesseract', 1)
    o = bridge.orientation_candidate(Image.new('RGB', (20, 10)), Config(orientation=True),
                                     '/test/tesseract', lambda: 1, timeout, timeout)
    assert o['status'] == 'orientation_failed_or_timed_out' and o['raw_osd'] == 'evidence'
    assert o['transformed_size'] == (10, 20) and 'raw_text' not in o
