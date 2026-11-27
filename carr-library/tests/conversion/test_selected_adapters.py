"""Portable authority and parser bounds for explicitly selected adapters."""
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import time

import pytest
from PIL import Image

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService, ConversionUnavailable
from carr_library.conversion.adapters import selected_records
from carr_library.conversion.capabilities import capabilities
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import _invoke


def selected(tmp_path, suffix, data, **options):
    source = tmp_path / ('selected' + suffix)
    source.write_bytes(data)
    output = tmp_path / 'output'
    output.mkdir()
    return source, output, ConversionService(output, input_roots=(source,), **options)


@pytest.mark.parametrize('suffix', ['.rtf', '.png', '.tiff'])
def test_document_extension_never_activates_an_implicit_converter(tmp_path, suffix):
    source, output, service = selected(tmp_path, suffix, b'selected original')
    with pytest.raises(ConversionUnavailable, match='explicitly'):
        service.extract(source, 'reading.txt')
    assert source.read_bytes() == b'selected original'
    assert list(output.iterdir()) == []


def test_ocr_flag_requires_an_explicit_typed_model_choice(tmp_path):
    with pytest.raises(ValueError, match='explicit OCR configuration'):
        ConversionService(tmp_path, image_ocr=True)
    with pytest.raises(ValueError, match='explicit OCR configuration'):
        ConversionService(tmp_path, image_ocr=1, ocr_config=Config())


def test_inventory_distinguishes_optional_adapters_from_unintegrated_legacy():
    rows = {row['extension']: row for row in capabilities()}
    for suffix in ('.rtf', '.png', '.tiff'):
        assert rows[suffix]['qualification'] == 'explicit_selection_required'
        assert rows[suffix]['available'] is False
    assert rows['.doc']['qualification'] == 'not_integrated'


@pytest.mark.parametrize('changes', [
    {'max_image_pixels': 0}, {'max_total_image_pixels': True}, {'pandoc_memory_bytes': 1},
    {'max_image_pixels': 20_000_001}, {'max_total_image_pixels': 200_000_001},
], ids=['zero-pixels', 'boolean-total', 'tiny-memory', 'frame-limit', 'total-limit'])
def test_additional_budgets_are_typed_and_bounded(changes):
    with pytest.raises(ValueError):
        ConversionBudget(**changes)


def image_bytes(format='TIFF', sizes=((20, 10), (20, 10))):
    images = [Image.new('RGB', size, 'white') for size in sizes]
    try:
        stream = BytesIO()
        images[0].save(stream, format=format, save_all=len(images) > 1, append_images=images[1:])
        return stream.getvalue()
    finally:
        for image in images:
            image.close()


@pytest.mark.parametrize('budget,message', [
    (ConversionBudget(max_pages=1), 'frame count'),
    (ConversionBudget(max_image_pixels=100), 'pixel budget'),
    (ConversionBudget(max_total_image_pixels=300), 'pixel budget'),
], ids=['frame-count', 'single-frame', 'cumulative-frames'])
def test_all_headers_are_bounded_before_ocr_runtime_discovery(tmp_path, monkeypatch, budget, message):
    from carr_library.extract import ocr_runtime
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime',
                        lambda *a, **k: pytest.fail('Oversized image reached the OCR runtime.'))
    data = image_bytes()
    _, _, records, _ = selected_records(tmp_path / 'source.tiff', data, '.tiff', Config(),
        budget, time.monotonic() + 10, {'kind': 'raster-ocr'})
    with pytest.raises(ConversionError, match=message):
        list(records)


def test_raster_header_must_match_extension_before_engine_execution(tmp_path):
    source, output, service = selected(tmp_path, '.png', image_bytes(), image_ocr=True, ocr_config=Config())
    with pytest.raises(ConversionError, match='does not match'):
        service.extract(source, 'reading.txt')
    assert list(output.iterdir()) == []


def test_rtf_selected_executable_hash_is_rechecked_before_start(tmp_path):
    executable = tmp_path / 'converter'
    executable.write_bytes(b'changed bytes')
    selection = {'kind': 'pandoc-rtf', 'executable': {'path': str(executable),
        'bytes': executable.stat().st_size, 'sha256': '0' * 64}}
    with pytest.raises(ConversionError, match='converter bytes changed'):
        selected_records(tmp_path / 'source.rtf', b'{\\rtf1\\ansi words}', '.rtf', Config(),
                         ConversionBudget(), time.monotonic() + 10, selection)
    assert not (tmp_path / 'pandoc.txt').exists()


@pytest.mark.parametrize('selection', [None, {'kind': 'raster-ocr', 'command': 'unexpected'},
                                      {'kind': 'pandoc-rtf', 'executable': {}}],
                         ids=['absent', 'extra-command', 'wrong-kind'])
def test_worker_refuses_unapproved_image_adapter_payload(tmp_path, selection):
    data = image_bytes('PNG', ((20, 10),))
    source = tmp_path / 'source.png'
    source.write_bytes(data)
    message = dict(action='convert', source=str(source), output=str(tmp_path / 'reading.txt'),
        report=str(tmp_path / 'details.json'), sha256=sha256(data).hexdigest(), source_size=len(data),
        budget=asdict(ConversionBudget()), ocr_config=asdict(Config()), timeout=120,
        adapter=selection)
    result = _invoke(message, timeout=10)
    assert result['state'] == 'failed'
    assert not (tmp_path / 'reading.txt').exists()


def test_unselected_converter_path_cannot_come_from_source_metadata(tmp_path):
    with pytest.raises(ValueError, match='absolute local'):
        ConversionService(tmp_path, pandoc='pandoc')


@pytest.mark.parametrize('change', ['rewrite', 'replace', 'replace-parent'])
def test_queued_conversion_cannot_adopt_a_changed_selected_converter(tmp_path, monkeypatch, change):
    from carr_library.conversion import service as module
    tools = tmp_path / 'explicit-tools'
    tools.mkdir()
    executable = tools / 'pandoc'
    executable.write_bytes(b'Explicitly selected synthetic converter; never execute')
    source, output, service = selected(tmp_path, '.rtf', b'{\\rtf1\\ansi Original}',
                                       pandoc=executable)
    before = source.read_bytes()
    if change == 'rewrite':
        executable.write_bytes(b'Unreviewed replacement contents')
    elif change == 'replace':
        replacement = tools / 'replacement'
        replacement.write_bytes(executable.read_bytes())
        replacement.replace(executable)
    else:
        retained = tmp_path / 'retained-tool-folder'
        tools.rename(retained)
        tools.mkdir()
        (retained / 'pandoc').rename(executable)
    monkeypatch.setattr(module, '_invoke', lambda *args, **kwargs:
                        pytest.fail('A changed converter must be rejected before launching the worker'))
    with pytest.raises(ConversionError, match='selected Pandoc executable changed'):
        service.extract(source, 'reading.txt')
    assert source.read_bytes() == before
    assert list(output.iterdir()) == []
