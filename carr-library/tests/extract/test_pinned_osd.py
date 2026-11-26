"""Real local Tesseract OSD/LSTM compatibility, synthetic pages only."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import time

import pytest
from PIL import Image, ImageDraw, ImageFont
from rapidfuzz.distance.Levenshtein import distance
from carr_library.extract import ocr_orientation_bridge as bridge
from carr_library.extract.orientation import parse_osd
from carr_library.extract.pinned_tessdata import MODELS, OSD_MODEL, POLICY, stage_pinned_models

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


def config():
    return SimpleNamespace(model_policy=POLICY, dpi=240, max_pixels=20_000_000, languages='eng')


@pytest.fixture
def runner():
    exe = shutil.which('tesseract')
    assert exe, 'real Tesseract required'
    deadline = time.monotonic() + 40
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(exe, 40)
        return left
    def run(args):
        return subprocess.run(args, check=True, capture_output=True, timeout=remaining())
    return exe, remaining, run


@pytest.mark.parametrize('angle', [90, 180, 270])
def test_real_pinned_bridge(runner, tmp_path, angle):
    exe, remaining, run = runner
    report = run([exe, '--list-langs']).stdout.decode('utf-8')
    args, baseline_model = stage_pinned_models(report, 'eng', tmp_path/'verified language', remaining)
    assert baseline_model['model_sha256'] == {'eng': MODELS['eng'][1]}
    image = Image.new('RGB', (1800, 1150), 'white')
    ImageDraw.Draw(image).multiline_text((60,60), TEXT, font=ImageFont.truetype('DejaVuSans.ttf', 48), fill='black', spacing=24)
    image = image.rotate(angle, expand=True)
    before = sha256(image.tobytes()).hexdigest()
    def recognize(raster, name):
        inp, out = tmp_path/(name+'.png'), tmp_path/name
        raster.save(inp, dpi=(240,240))
        run([exe, str(inp), str(out), '-l', 'eng', '--dpi', '240', '--psm', '3', *args,
             '-c', 'tessedit_create_txt=1', '-c', 'tessedit_create_tsv=1'])
        return out.with_suffix('.txt').read_bytes().decode(), out.with_suffix('.tsv').read_bytes().decode()
    baseline = recognize(image, 'baseline')
    started = time.monotonic()
    result = bridge.orientation_candidate(image, config(), exe, remaining, run, recognize)
    elapsed = time.monotonic() - started
    assert result['status'] == 'ocr_candidate' and result['clockwise'] == angle
    assert result['engine'] == 'Tesseract OSD via native CLI'
    assert result['osd_model']['model_sha256'] == {'osd': OSD_MODEL[1]}
    assert result['osd_model']['engine_mode'] == 'legacy_osd'
    assert result['traineddata_sha256_observed'] == {}
    assert result['raw_tsv'] and result['coordinate_frame'] == 'orientation_candidate_pixels_top_left'
    assert result['source_rgb_sha256'] == before == sha256(image.tobytes()).hexdigest()
    assert (tmp_path/'baseline.txt').read_bytes().decode() == baseline[0]
    assert (tmp_path/'baseline.tsv').read_bytes().decode() == baseline[1]
    expected = ' '.join(TEXT.split())
    cer = distance(expected, ' '.join(result['raw_text'].split())) / len(expected)
    print(f'angle={angle} CER={cer:.6f} OSD+OCR={elapsed:.3f}s confidence={result["confidence"]}')
    assert cer <= .01


@pytest.mark.parametrize('kind', ['missing', 'corrupt'])
def test_osd_failure_never_recognizes_or_falls_back(runner, tmp_path, monkeypatch, kind):
    exe, remaining, run = runner
    report = run([exe, '--list-langs']).stdout.decode('utf-8')
    if kind == 'corrupt':
        import re
        source = Path(re.search(r'"([^"\r\n]+)"', report)[1])/'osd.traineddata'
        data = bytearray(source.read_bytes()); data[-1] ^= 1
        (tmp_path/'osd.traineddata').write_bytes(data)
    monkeypatch.setenv('TESSDATA_PREFIX', str(tmp_path))
    def guarded(args):
        assert args == [exe, '--list-langs'], 'must not invoke any unverified inference'
        return run(args)
    def forbidden(*args, **kwargs):
        pytest.fail('no fallback or rotated recognition allowed')
    monkeypatch.setattr(bridge, 'detect_orientation', forbidden)
    image = Image.new('RGB', (20, 20), 'white')
    result = bridge.orientation_candidate(image, config(), exe, remaining, guarded, forbidden)
    assert result['status'] == 'pinned_osd_unavailable_or_invalid'
    assert 'osd_model' not in result and 'raw_text' not in result


def test_osd_cannot_enter_lstm_mode(runner, tmp_path):
    exe, remaining, run = runner
    report = run([exe, '--list-langs']).stdout.decode('utf-8')
    with pytest.raises(ValueError, match='unsupported'):
        stage_pinned_models(report, 'osd', tmp_path/'invalid', remaining)
    with pytest.raises(ValueError, match='unsupported'):
        stage_pinned_models(report, 'eng', tmp_path/'invalid', remaining, osd=True)


@pytest.mark.parametrize('raw', ['Rotate: 45\nOrientation confidence: 20',
                                 'Rotate: 90\nRotate: 180\nOrientation confidence: 20',
                                 'Rotate: 90\nOrientation confidence: nan'])
def test_invalid_osd(raw):
    assert parse_osd(raw).status == 'invalid_osd'


def test_pinned_pdf_keeps_baseline_and_page(tmp_path, monkeypatch, runner):
    from carr_library.extract.page_ocr import Config, extract_page
    exe, remaining, run = runner
    image = Image.new('RGB', (1800, 1150), 'white')
    ImageDraw.Draw(image).multiline_text((60,60), TEXT, font=ImageFont.truetype('DejaVuSans.ttf', 48), fill='black', spacing=24)
    path = tmp_path/'pages.pdf'
    Image.new('RGB', (1800, 1150), 'white').save(path, save_all=True, append_images=[image.rotate(90, expand=True)], resolution=240)
    before = sha256(path.read_bytes()).hexdigest()
    base = extract_page(path, 2, Config(dpi=240, model_policy=POLICY))
    result = extract_page(path, 2, Config(dpi=240, model_policy=POLICY, orientation=True))
    o = result['provenance']['orientation']
    assert o['status'] == 'ocr_candidate' and o['clockwise'] == 90
    assert result['raw_text'] == base['raw_text'] and result['blocks'] == base['blocks']
    assert result['provenance']['asset_sha256'] == before == sha256(path.read_bytes()).hexdigest()
    assert result['physical_index'] == 2 and result['printed_label'] is None
    assert result['status'] == 'review_required' and not result['printed_label_verified']
    assert result['provenance']['ocr_model'] == base['provenance']['ocr_model']
    # Missing OSD may not prevent already verified baseline language inference.
    import re
    report = run([exe, '--list-langs']).stdout.decode('utf-8')
    local = Path(re.search(r'"([^"\r\n]+)"', report)[1])
    language_only = tmp_path/'language only'; language_only.mkdir()
    shutil.copyfile(local/'eng.traineddata', language_only/'eng.traineddata')
    monkeypatch.setenv('TESSDATA_PREFIX', str(language_only))
    fallback = extract_page(path, 2, Config(dpi=240, model_policy=POLICY, orientation=True))
    assert fallback['provenance']['orientation']['status'] == 'pinned_osd_unavailable_or_invalid'
    assert fallback['raw_text'] == base['raw_text'] and fallback['blocks'] == base['blocks']
