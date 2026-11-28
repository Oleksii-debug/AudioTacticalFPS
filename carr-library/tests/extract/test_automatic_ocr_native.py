"""Real source import helpers, pinned detector and actual installed Tesseract.

These cases are required where selected by CI: absence of the English runtime
is a failure, never a skipped or synthetic successful recognition.
"""
from hashlib import sha256
import os
from pathlib import Path
import textwrap

import pytest
from PIL import Image, ImageDraw, ImageFont

from carr_library.extract import language_detection as lid, ocr_regions, page_ocr
from carr_library.extract.page_ocr import Config, ExtendedConfig
from carr_library.extract.raster_pages import extract_raster_page


TEXT = ('The research library preserves the original document and its exact words. '
        'A reader compares every proposed quotation with the printed page before approving it. '
        'The original source remains unchanged during language detection and regional review.')


def source(tmp_path, suffix):
    image = Image.new('RGB', (1600, 650), 'white')
    font = ImageFont.load_default(size=40)
    drawing = ImageDraw.Draw(image)
    drawing.multiline_text((55, 55), '\n'.join(textwrap.wrap(TEXT, 62)),
                           font=font, fill='black', spacing=25)
    path = tmp_path / ('English source.' + suffix)
    if suffix == 'png':
        image.save(path)
    else:
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.utils import ImageReader
        canvas = Canvas(str(path), pagesize=(640, 360))
        canvas.drawImage(ImageReader(image), 0, 100, width=640, height=260)
        canvas.save()
    image.close()
    return path


@pytest.fixture
def actual_runtime(monkeypatch):
    # Full Windows CI supplies a sealed engine directory; ordinary Linux uses
    # its explicitly installed local Tesseract.
    if os.environ.get('CARR_CI_OCR_DIR'):
        monkeypatch.setenv('CARR_OCR_DIR', os.environ['CARR_CI_OCR_DIR'])
    runtime = page_ocr.discover_ocr_runtime('eng')
    assert runtime is not None, 'This acceptance case requires actual English Tesseract.'
    return runtime


@pytest.mark.parametrize('suffix', ['pdf', 'png'])
def test_actual_english_ocr_detects_language_and_keeps_region_and_source_evidence(
        tmp_path, actual_runtime, suffix):
    path = source(tmp_path, suffix)
    original = path.read_bytes()
    run = page_ocr.extract_page if suffix == 'pdf' else extract_raster_page
    result = run(path, 1, ExtendedConfig(languages='eng', dpi=150, timeout=120, automatic_languages=True))
    assert path.read_bytes() == original
    assert result['status'] == 'review_required' and result['printed_label_verified'] is False
    provenance = result['provenance']
    assert provenance['asset_sha256'] == sha256(original).hexdigest()
    assert provenance['ocr_text'] == result['raw_text']
    assert 'research library' in result['raw_text'].lower()
    observation = provenance['language_detection']
    assert observation['model_sha256'] == lid.MODEL_SHA256
    assert observation['document']['language'] == 'en', observation
    accepted = [row for row in observation['regions'] if row['status'] == 'ocr_candidate']
    assert accepted, observation
    assert all(row['selected_model'] == 'eng' and row['raw_text'].strip()
               and row['raw_tsv'] and row['crop_rgb_sha256'] for row in accepted)
    assert 'automatic_language_candidates_require_review' in result['warnings']
    assert 'ocr_not_verified' in result['warnings']


def test_actual_manual_override_never_loads_or_executes_language_detector(
        tmp_path, actual_runtime, monkeypatch):
    path = source(tmp_path, 'png')
    def forbidden(*args, **kwargs):
        pytest.fail('Explicit manual OCR must not load the language detector.')
    monkeypatch.setattr(ocr_regions, 'Detector', forbidden)
    result = extract_raster_page(path, 1, Config(languages='eng', timeout=120))
    assert 'research library' in result['raw_text'].lower()
    assert 'language_detection' not in result['provenance']
    assert result['status'] == 'review_required'


def test_actual_ocr_with_missing_detector_keeps_the_baseline_candidate(
        tmp_path, actual_runtime, monkeypatch):
    path = source(tmp_path, 'png')
    def unavailable(**kwargs):
        raise lid.DetectorUnavailable('Deliberately unavailable for the boundary test.')
    monkeypatch.setattr(ocr_regions, 'Detector', unavailable)
    result = extract_raster_page(path, 1, ExtendedConfig(languages='eng', timeout=120, automatic_languages=True))
    assert 'research library' in result['raw_text'].lower()
    assert result['raw_text'] == result['provenance']['ocr_text']
    assert result['provenance']['language_detection']['status'] == 'detector_unavailable'
    assert result['status'] == 'review_required'
