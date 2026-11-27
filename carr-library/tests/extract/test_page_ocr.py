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
    # This fixture models no configured runtime as well as no PATH engine.
    monkeypatch.delenv('CARR_OCR_DIR', raising=False)
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


def _layout_words(words):
    from carr_library.extract.page_ocr import _TSV_FIELDS
    return '\t'.join(_TSV_FIELDS) + '\n' + ''.join(
        f'5\t1\t1\t1\t1\t{i}\t10\t20\t30\t40\t95\t{word}\n'
        for i, word in enumerate(words, 1))


@pytest.mark.parametrize('raw,words', [
    ('Railway bridge', ['Railway', 'bridge']),
    ('Міст\nчерез річку\f', ['Міст', 'через', 'річку']),
    ('日本語', ['日本', '語']),
    ('A—B, C!', ['A—B,', 'C!']),
    ('', []),
    ('a\u00a0b', ['a', 'b']),
])
def test_layout_text_consistency(raw, words):
    from carr_library.extract.page_ocr import _matching_ocr_blocks
    blocks = _matching_ocr_blocks(_layout_words(words), (100, 100), raw)
    assert [block['text'] for block in blocks] == words


@pytest.mark.parametrize('raw,words', [
    ('Railway bridge', ['Railway', 'station']),
    ('Railway bridge', ['Railway']),
    ('Railway', ['Railway', 'bridge']),
    ('Railway bridge', ['bridge', 'Railway']),
    ('A—B', ['A-B']),
    ('é', ['e\u0301']),
    ('A', []),
    ('', ['A']),
    ('A\x00', ['A']),
])
def test_layout_rejects_conflicting_text(raw, words):
    from carr_library.extract.page_ocr import _matching_ocr_blocks
    with pytest.raises(ValueError, match='disagree'):
        _matching_ocr_blocks(_layout_words(words), (100, 100), raw)


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local engine unavailable')
@pytest.mark.parametrize('index', [2, 4])
@pytest.mark.parametrize('automatic', [False, True])
def test_conflicting_layout_retains_raw_candidates(mixed_pdf, monkeypatch, index, automatic):
    import hashlib
    from carr_library.extract import page_ocr
    original_read = page_ocr._read_ocr_output
    original = hashlib.sha256(mixed_pdf.read_bytes()).hexdigest()

    def conflicting_read(path):
        actual = original_read(path)
        return _layout_words(['Unrelated', 'text']) if path.suffix == '.tsv' else actual

    monkeypatch.setattr(page_ocr, '_read_ocr_output', conflicting_read)
    result = extract_page(mixed_pdf, index, Config(automatic_languages=automatic))
    assert result['status'] == 'review_required'
    assert 'ocr_layout_invalid' in result['warnings']
    assert result['blocks'] == []
    assert 'Railway bridge' in result['provenance']['ocr_text']
    assert 'Unrelated' in result['provenance']['ocr_tsv']
    assert result['printed_label'] is None
    assert hashlib.sha256(mixed_pdf.read_bytes()).hexdigest() == original
    if automatic:
        detection = result['provenance']['language_detection']
        assert detection['status'] == 'language_review_failed_or_timed_out'
        assert detection['regions'] == []
        assert 'automatic_language_candidates_require_review' in result['warnings']
    if index == 4:
        assert 'Native header' in result['raw_text']
    else:
        assert 'Railway bridge' in result['raw_text']


def test_duplicate_ocr_word_identity_rejected():
    from carr_library.extract.page_ocr import _matching_ocr_blocks
    tsv = _layout_words(['alpha', 'alpha']).splitlines()
    cells = tsv[2].split('\t')
    cells[5] = '1'  # Same hierarchical word ID, different rectangle.
    cells[6:8] = ['50', '50']
    tsv[2] = '\t'.join(cells)
    with pytest.raises(ValueError, match='invalid OCR layout word'):
        _matching_ocr_blocks('\n'.join(tsv) + '\n', (100, 100), 'alpha alpha')


def test_whitespace_only_ocr_word_rejected():
    from carr_library.extract.page_ocr import _matching_ocr_blocks
    with pytest.raises(ValueError, match='invalid OCR layout word'):
        _matching_ocr_blocks(_layout_words(['   ']), (100, 100), '')


@pytest.mark.parametrize('different_line', [False, True])
def test_repeated_ocr_text_with_distinct_word_identity(different_line):
    from carr_library.extract.page_ocr import _matching_ocr_blocks
    tsv = _layout_words(['alpha', 'alpha']).splitlines()
    cells = tsv[2].split('\t')
    if different_line:
        cells[4:6] = ['2', '1']
    cells[6:8] = ['50', '50']
    tsv[2] = '\t'.join(cells)
    blocks = _matching_ocr_blocks('\n'.join(tsv) + '\n', (100, 100), 'alpha alpha')
    assert [item['text'] for item in blocks] == ['alpha', 'alpha']

