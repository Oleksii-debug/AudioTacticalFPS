"""Synthetic fixtures, actual local Tesseract LSTM inference; no private books."""
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from PIL import Image, ImageDraw, ImageFont
from rapidfuzz.distance.Levenshtein import distance

from carr_library.extract.pinned_tessdata import MODELS, POLICY, stage_pinned_models


@pytest.fixture
def engine():
    exe = shutil.which('tesseract')
    if exe is None:
        pytest.fail('real Tesseract required for pinned model acceptance')
    report = subprocess.run([exe, '--list-langs'], check=True, capture_output=True, timeout=5).stdout.decode()
    return exe, report


@pytest.mark.parametrize('lang,text', [
    ('eng', 'The archive contains original historical documents.\nA railway connects distant cities.'),
    ('ukr', 'Архів містить оригінальні історичні документи.\nЗалізниця з’єднує віддалені міста.'),
    ('slk', 'Archív obsahuje pôvodné historické dokumenty.\nŽeleznica spája vzdialené mestá.'),
])
def test_real_pinned_inference(engine, tmp_path, lang, text):
    exe, report = engine
    deadline = time.monotonic() + 30
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('deadline')
        return seconds
    args, provenance = stage_pinned_models(report, lang, tmp_path/'verified models', remaining)
    assert provenance['policy'] == POLICY
    model = tmp_path/'verified models'/(lang+'.traineddata')
    assert sha256(model.read_bytes()).hexdigest() == MODELS[lang][1]
    image = Image.new('RGB', (1800, 350), 'white')
    ImageDraw.Draw(image).multiline_text((35,35), text, font=ImageFont.truetype('DejaVuSans.ttf', 45), fill='black', spacing=22)
    path = tmp_path/'synthetic.png'
    image.save(path, dpi=(300,300))
    before = sha256(path.read_bytes()).hexdigest()
    started = time.monotonic()
    out = subprocess.run([exe, str(path), 'stdout', '-l', lang, '--psm', '6', '--dpi', '300', *args], check=True, capture_output=True, timeout=remaining()).stdout.decode('utf-8')
    elapsed = time.monotonic() - started
    expected = ' '.join(text.split())
    actual = ' '.join(out.split())
    cer = distance(expected, actual) / len(expected)
    print(f'{lang}: CER={cer:.6f}, inference={elapsed:.3f}s')
    assert cer <= .03
    assert sha256(path.read_bytes()).hexdigest() == before


def test_wrong_digest_same_size(engine, tmp_path):
    _, report = engine
    import re
    source = Path(re.search(r'"([^"\r\n]+)"', report)[1])
    bad = tmp_path/'bad'; bad.mkdir()
    data = bytearray((source/'eng.traineddata').read_bytes()); data[-1] ^= 1
    (bad/'eng.traineddata').write_bytes(data)
    with pytest.raises(ValueError, match='digest mismatch'):
        stage_pinned_models(f'List of available languages in "{bad}" (1):', 'eng', tmp_path/'staged', lambda: 10)


def test_missing_or_unknown(tmp_path):
    report = f'List of available languages in "{tmp_path}" (0):'
    with pytest.raises(ValueError, match='missing'):
        stage_pinned_models(report, 'eng', tmp_path/'missing', lambda: 10)
    for languages in ('fra', 'eng+eng', '../eng', ''):
        with pytest.raises(ValueError, match='unsupported'):
            stage_pinned_models(report, languages, tmp_path/'unknown', lambda: 10)
    with pytest.raises(ValueError, match='identify'):
        stage_pinned_models('unexpected report', 'eng', tmp_path/'unknown', lambda: 10)


def test_multiple_and_deadline(engine, tmp_path):
    _, report = engine
    args, provenance = stage_pinned_models(report, 'eng+ukr+slk', tmp_path/'all', lambda: 10)
    assert args[-2:] == ['--oem', '1']
    assert set(provenance['model_sha256']) == set(MODELS)
    def expired():
        raise TimeoutError('deadline')
    with pytest.raises(TimeoutError):
        stage_pinned_models(report, 'eng', tmp_path/'expired', expired)


def test_pdf_adapter(engine, tmp_path):
    from carr_library.extract.page_ocr import Config, extract_page
    text = 'The archive contains original historical documents.\nA railway connects distant cities.'
    image = Image.new('RGB', (1800, 600), 'white')
    ImageDraw.Draw(image).multiline_text((35,35), text, font=ImageFont.truetype('DejaVuSans.ttf', 45), fill='black', spacing=22)
    path = tmp_path/'scan.pdf'
    image.save(path, resolution=300)
    before = sha256(path.read_bytes()).hexdigest()
    config = Config(model_policy=POLICY)
    result = extract_page(path, 1, config)
    assert result['status'] == 'review_required'
    assert result['physical_index'] == 1 and result['printed_label'] is None
    assert result['provenance']['ocr_model']['model_sha256'] == {'eng': MODELS['eng'][1]}
    assert result['provenance']['config']['model_policy'] == POLICY
    assert result['provenance']['asset_sha256'] == before == sha256(path.read_bytes()).hexdigest()
    assert 'A railway connects distant cities.' in result['raw_text']
    assert result['blocks']
    assert Config().model_policy != config.model_policy
    assert Config(model_policy=POLICY, orientation=True).orientation is True


def test_pdf_adapter_refuses_wrong_model(engine, tmp_path, monkeypatch):
    from carr_library.extract.page_ocr import Config, extract_page
    source = tmp_path/'wrong'; source.mkdir()
    (source/'eng.traineddata').write_bytes(b'bad model')
    monkeypatch.setenv('TESSDATA_PREFIX', str(source))
    image = Image.new('RGB', (900, 300), 'white')
    ImageDraw.Draw(image).text((20, 20), 'Synthetic source', font=ImageFont.truetype('DejaVuSans.ttf', 45), fill='black')
    path = tmp_path/'scan.pdf'; image.save(path, resolution=300)
    result = extract_page(path, 1, Config(model_policy=POLICY))
    assert result['status'] == 'review_required'
    assert 'pinned_ocr_model_unavailable_or_invalid' in result['warnings']
    assert not result['raw_text'] and not result['blocks']
    assert 'ocr_model' not in result['provenance']
