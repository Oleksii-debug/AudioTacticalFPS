import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont
from carr_library.extract.orientation import Orientation, candidate_image, detect_orientation


@pytest.fixture
def scan():
    text = ('The archive contains letters and historical notes.\n'
            'Researchers compare editions and original pages.\n'
            'A railway bridge connects the eastern villages.\n'
            'Every quotation retains its original punctuation.\n'
            'The library contains independent collections.\n'
            'A researcher can review the evidence carefully.\n'
            'Printed numbers may differ from physical pages.\n'
            'Documents are preserved in their original form.\n'
            'The source provides evidence for each statement.\n'
            'Different authors may use the same expression.')
    image = Image.new('RGB', (1800, 1150), 'white')
    ImageDraw.Draw(image).multiline_text((60, 60), text, font=ImageFont.truetype('DejaVuSans.ttf', 48), fill='black', spacing=24)
    return image, text


def test_low_confidence_and_invalid_osd(scan, monkeypatch):
    p = pytest.importorskip('pytesseract')
    image, _ = scan
    for raw, status in [('Rotate: 90\nOrientation confidence: 0.03', 'uncertain'),
                        ('Rotate: 45\nOrientation confidence: 100', 'invalid_osd'),
                        ('Rotate: 90\nOrientation confidence: nan', 'invalid_osd'),
                        ('Rotate: 90\nRotate: 270\nOrientation confidence: 25', 'invalid_osd')]:
        monkeypatch.setattr(p, 'image_to_osd', lambda *a, **k: raw)
        result = detect_orientation(image)
        assert result.status == status and result.clockwise is None and result.raw_osd == raw
        with pytest.raises(ValueError):
            candidate_image(image, result)


def test_limits_and_engine_failure(scan, monkeypatch):
    p = pytest.importorskip('pytesseract')
    image, _ = scan
    assert detect_orientation(image, max_pixels=1).status == 'render_limit'
    with pytest.raises(TypeError):
        detect_orientation('not-an-image')
    def missing(*args, **kwargs):
        raise p.TesseractNotFoundError()
    monkeypatch.setattr(p, 'image_to_osd', missing)
    assert detect_orientation(image).status == 'engine_missing'
    def timeout(*args, **kwargs):
        raise RuntimeError('timeout')
    monkeypatch.setattr(p, 'image_to_osd', timeout)
    assert detect_orientation(image).status == 'osd_failed_or_timed_out'


@pytest.mark.parametrize('angle', [90, 180, 270])
def test_right_angle_mapping(angle):
    image = Image.new('RGB', (3, 2))
    image.putpixel((0, 0), (255, 255, 255))
    original = image.tobytes()
    rotated = candidate_image(image, Orientation('candidate', angle, 20))
    corner = {90: (1, 0), 180: (2, 1), 270: (0, 2)}[angle]
    assert rotated.getpixel(corner) == (255, 255, 255)
    assert image.tobytes() == original


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local Tesseract missing')
@pytest.mark.parametrize('angle', [90, 180, 270])
def test_real_osd_and_ocr(scan, angle):
    p = pytest.importorskip('pytesseract')
    distance = pytest.importorskip('rapidfuzz.distance.Levenshtein').distance
    image, text = scan
    # PIL rotates counterclockwise; upstream correction is clockwise.
    sideways = image.rotate(angle, expand=True)
    evidence = detect_orientation(sideways)
    print('angle', angle, 'OSD', evidence.status, evidence.confidence)
    assert evidence.status == 'candidate' and evidence.clockwise == angle
    corrected = candidate_image(sideways, evidence)
    assert corrected.size == image.size and corrected.tobytes() == image.tobytes()
    raw = p.image_to_string(corrected, lang='eng', config='--psm 3 --dpi 300', timeout=30)
    expected, actual = ' '.join(text.split()), ' '.join(raw.split())
    cer = distance(expected, actual) / len(expected)
    wer = distance(expected.split(), actual.split()) / len(expected.split())
    print('CER', cer, 'WER', wer)
    assert cer <= .01 and wer <= .02


@pytest.mark.skipif(not shutil.which('tesseract'), reason='local Tesseract missing')
def test_real_ukrainian_scan():
    p = pytest.importorskip('pytesseract')
    distance = pytest.importorskip('rapidfuzz.distance.Levenshtein').distance
    if 'ukr' not in p.get_languages(config=''):
        pytest.skip('Ukrainian traineddata unavailable')
    text = ('Бібліотека зберігає книжки та наукові статті.\n'
            'Дослідники порівнюють різні видання і сторінки.\n'
            'Залізничний міст сполучає віддалені села.\n'
            'Кожна цитата зберігає початкову пунктуацію.\n'
            'Документи містять незалежні колекції джерел.\n'
            'Дослідник може уважно перевірити докази.\n'
            'Друковані номери відрізняються від фізичних.\n'
            'Тексти зберігаються у своєму первинному вигляді.\n'
            'Джерело надає докази для кожного твердження.\n'
            'Різні автори можуть уживати однакові вислови.')
    image = Image.new('RGB', (1800, 1150), 'white')
    ImageDraw.Draw(image).multiline_text((60, 60), text, font=ImageFont.truetype('DejaVuSans.ttf', 48), fill='black', spacing=24)
    sideways = image.rotate(90, expand=True)
    evidence = detect_orientation(sideways)
    assert evidence.status == 'candidate' and evidence.clockwise == 90
    raw = p.image_to_string(candidate_image(sideways, evidence), lang='ukr', config='--psm 3 --dpi 300', timeout=30)
    expected, actual = ' '.join(text.split()), ' '.join(raw.split())
    cer = distance(expected, actual) / len(expected)
    wer = distance(expected.split(), actual.split()) / len(expected.split())
    print('ukr 90 OSD', evidence.confidence, 'CER', cer, 'WER', wer)
    assert cer <= .01 and wer <= .02
