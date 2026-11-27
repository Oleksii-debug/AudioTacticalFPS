"""Required host acceptance: real selected Pandoc and local English Tesseract.

Run only in a gate that provisions these explicit optional host tools. Missing
tools fail this suite; they never become a skip or a fake successful conversion.
"""
from hashlib import sha256
from pathlib import Path
import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.extract.page_ocr import Config


def pandoc():
    value = os.environ.get('CARR_PANDOC') or shutil.which('pandoc')
    assert value, 'This acceptance gate must provision an explicitly selected Pandoc.'
    return Path(value).resolve(strict=True)


def raster(path, words, *, multiple=False):
    images = []
    try:
        for word in words:
            image = Image.new('RGB', (1200, 240), 'white')
            draw = ImageDraw.Draw(image)
            draw.text((45, 70), word, fill='black', font=ImageFont.load_default(size=52))
            images.append(image)
        images[0].save(path, format='TIFF' if multiple else 'PNG',
                       save_all=multiple, append_images=images[1:], dpi=(300, 300))
    finally:
        for image in images:
            image.close()


def test_real_selected_pandoc_rtf_keeps_original_and_bound_receipt(tmp_path):
    source = tmp_path / 'Обране джерело.rtf'
    source.write_bytes(b'{\\rtf1\\ansi Reading source\\par A second paragraph.}')
    before = source.read_bytes()
    output = tmp_path / 'Новий результат'
    output.mkdir()
    result = ConversionService(output, input_roots=(source,), pandoc=pandoc()).extract(source, 'reading.txt')
    assert 'Reading source' in result.path.read_text() and 'A second paragraph.' in result.path.read_text()
    assert source.read_bytes() == before
    assert result.report['source_sha256'] == sha256(before).hexdigest()
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['segments'][0]['logical_locator']['sandbox'] is True
    assert result.report['verified_for_quotation'] is False
    with pytest.raises(FileExistsError):
        ConversionService(output, input_roots=(source,), pandoc=pandoc()).extract(source, 'reading.txt')


def test_real_rtf_external_field_cannot_supply_unselected_local_content(tmp_path):
    secret = tmp_path / 'private.txt'
    secret.write_text('UNSELECTED_PRIVATE_CONTENT_1a2b3c')
    source = tmp_path / 'field.rtf'
    source.write_bytes(('{\\rtf1\\ansi Selected visible source.\\par '
        '{\\field{\\*\\fldinst INCLUDETEXT "' + secret.as_posix() + '"}{\\fldrslt Visible fallback}}}').encode())
    output = tmp_path / 'output'
    output.mkdir()
    result = ConversionService(output, input_roots=(source,), pandoc=pandoc()).extract(source, 'reading.txt')
    assert 'Selected visible source.' in result.path.read_text()
    assert secret.read_text() not in result.path.read_text()


def test_real_pandoc_output_limit_publishes_no_partial_copy(tmp_path):
    source = tmp_path / 'source.rtf'
    source.write_bytes(b'{\\rtf1\\ansi More than four bytes.}')
    output = tmp_path / 'output'
    output.mkdir()
    with pytest.raises(ConversionError, match='budget|size'):
        ConversionService(output, input_roots=(source,), pandoc=pandoc(),
                          budget=ConversionBudget(max_text_bytes=4)).extract(source, 'reading.txt')
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('multiple', [False, True], ids=['unicode-png', 'two-frame-tiff'])
def test_real_existing_ocr_bridge_reads_every_selected_image_frame(tmp_path, multiple):
    source = tmp_path / ('Оригінал.tiff' if multiple else 'Оригінал.png')
    words = ['FIRST READING PAGE', 'SECOND SOURCE PAGE'] if multiple else ['FIRST READING PAGE']
    raster(source, words, multiple=multiple)
    before = source.read_bytes()
    output = tmp_path / 'Читальна копія'
    output.mkdir()
    service = ConversionService(output, input_roots=(source,), image_ocr=True, ocr_config=Config(languages='eng'))
    result = service.extract(source, 'reading.txt')
    text = result.path.read_text()
    assert 'FIRST READING PAGE' in text
    if multiple:
        assert '\f' in text and 'SECOND SOURCE PAGE' in text
    assert len(result.report['segments']) == len(words)
    for index, segment in enumerate(result.report['segments'], 1):
        assert segment['logical_locator']['image_frame'] == index
        assert segment['physical_index'] is None and segment['printed_label'] is None
        assert segment['printed_label_verified'] is False and segment['status'] == 'review_required'
        assert segment['logical_locator']['ocr_config']['languages'] == 'eng'
    assert source.read_bytes() == before
    assert result.report['source_sha256'] == sha256(before).hexdigest()


def test_cancel_settles_existing_ocr_family_before_removing_its_private_files(tmp_path):
    source = tmp_path / 'source.tiff'
    raster(source, ['READING SOURCE PAGE'] * 8, multiple=True)
    before = source.read_bytes()
    output = tmp_path / 'output'
    output.mkdir()
    observed = []
    def cancelled():
        found = list(output.glob('.carr-conversion-*/raster-ocr-*/frame.png'))
        if found:
            observed.append(True)
        return bool(observed)
    with pytest.raises(InterruptedError):
        ConversionService(output, input_roots=(source,), image_ocr=True,
                          ocr_config=Config()).extract(source, 'reading.txt', cancelled=cancelled)
    assert observed and list(output.iterdir()) == []
    assert source.read_bytes() == before
