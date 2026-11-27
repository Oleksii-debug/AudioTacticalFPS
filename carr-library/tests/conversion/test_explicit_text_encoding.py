"""Explicit legacy TXT codecs through the actual isolated conversion worker.

All input bytes are synthetic. Codec selection is finite and explicit; these
cases do not qualify a native converter or change streamed UTF-8 import.
"""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion import service as service_module
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import _invoke


def selected(tmp_path, raw, *, suffix='.txt', budget=None):
    source = tmp_path / ('Київ-legacy' + suffix)
    source.write_bytes(raw)
    output = tmp_path / 'reading'
    output.mkdir()
    return source, output, ConversionService(output, input_roots=(source,), budget=budget)


def assert_reading(result, source, original, text, codec):
    assert source.read_bytes() == original
    assert result.path.read_bytes() == text.encode('utf-8')
    report = json.loads(result.report_path.read_text(encoding='utf-8'))
    assert report == result.report
    assert report['source_sha256'] == sha256(original).hexdigest()
    assert report['source_size'] == len(original)
    assert report['source_encoding'] == codec
    assert report['engine'] == 'explicit-text-encoding-v1'
    assert report['verified_for_quotation'] is False
    assert report['original_modified'] is False
    assert report['text_sha256'] == sha256(text.encode('utf-8')).hexdigest()
    for row in report['segments']:
        start, end = row['output_characters']
        assert sha256(text[start:end].encode('utf-8')).hexdigest() == row['text_sha256']


@pytest.mark.parametrize(('selection', 'codec', 'text'), [
    ('UTF_8', 'utf-8', 'Київ\r\n  first  \f東京\n'),
    ('utf-8-sig', 'utf-8-sig', 'Київ\r\n  first  \f東京\n'),
    ('utf-16', 'utf-16', 'Київ\r\n  first  \f東京\n'),
    ('utf-16-le', 'utf-16-le', 'Київ\r\n  first  \f東京\n'),
    ('utf-16-be', 'utf-16-be', 'Київ\r\n  first  \f東京\n'),
    ('Windows_1251', 'cp1251', 'Київ\r\n  перший  \fдругий\n'),
    ('windows-1252', 'cp1252', 'Café €\r\n  first  \fsecond\n'),
    ('iso-8859-1', 'iso-8859-1', 'Café\r\n  first  \fsecond\n'),
    ('ascii', 'ascii', 'First\r\n  exact  \fsecond\n'),
])
def test_explicit_selected_codec_round_trips_exact_source_and_receipt(tmp_path, selection, codec, text):
    original = text.encode(codec)
    source, _, converter = selected(tmp_path, original)
    result = converter.extract(source, 'reading.txt', encoding=selection)
    assert len(result.report['segments']) == 2
    assert_reading(result, source, original, text, codec)


@pytest.mark.parametrize('codec', ['cp1251', 'utf-16'])
def test_absent_selection_keeps_strict_utf8_default_without_guessing(tmp_path, codec):
    original = 'Київ'.encode(codec)
    source, output, converter = selected(tmp_path, original)
    with pytest.raises(ConversionError):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('selection', ['latin1', 'utf-7', 'shift_jis', 'utf-8//IGNORE', ' cp1251', '', True])
def test_unknown_or_ambiguous_selection_is_rejected_before_worker(tmp_path, monkeypatch, selection):
    source, output, converter = selected(tmp_path, b'Original text.')
    calls = []
    monkeypatch.setattr(service_module, '_invoke', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ConversionError, match='explicitly supported'):
        converter.extract(source, 'reading.txt', encoding=selection)
    assert calls == []
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('suffix', ['.html', '.md'])
def test_selected_codec_cannot_override_a_structured_reader(tmp_path, monkeypatch, suffix):
    source, output, converter = selected(tmp_path, b'<p>Original</p>', suffix=suffix)
    calls = []
    monkeypatch.setattr(service_module, '_invoke', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ConversionError, match='only to plain'):
        converter.extract(source, 'reading.txt', encoding='cp1251')
    assert calls == []
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('variant', ['null', 'alias', 'unknown', 'numeric', 'adapter', 'structured'])
def test_isolated_worker_rejects_invalid_codec_authority_without_output(tmp_path, variant):
    suffix = '.html' if variant == 'structured' else '.txt'
    source = tmp_path / ('source' + suffix)
    original = b'Original text.'
    source.write_bytes(original)
    message = dict(action='convert', source=str(source), output=str(tmp_path / 'reading.txt'),
        report=str(tmp_path / 'details.json'), sha256=sha256(original).hexdigest(),
        source_size=len(original), budget=asdict(ConversionBudget()),
        ocr_config=asdict(Config()), timeout=120, text_encoding='cp1251')
    if variant in ('null', 'alias', 'unknown', 'numeric'):
        message['text_encoding'] = {'null': None, 'alias': 'Windows-1251',
                                   'unknown': 'utf-7', 'numeric': 1251}[variant]
    elif variant == 'adapter':
        message['adapter'] = {'kind': 'raster-ocr'}
    result = _invoke(message, timeout=30)
    assert result['state'] == 'failed'
    assert source.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]


@pytest.mark.parametrize('original', [
    b'MZ executable', b'\x7fELF executable', b'%PDF-1.4 document',
    b'{\\rtf1 document}', b'PK\x03\x04 archive', b'Original\x00control',
])
def test_explicit_codec_does_not_accept_binary_signatures_or_controls(tmp_path, original):
    source, output, converter = selected(tmp_path, original)
    with pytest.raises(ConversionError):
        converter.extract(source, 'reading.txt', encoding='cp1252')
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(('codec', 'original'), [
    ('utf-16-le', b'A\x00B'), ('cp1252', b'undefined \x81'),
])
def test_selected_codec_decoding_is_strict_without_replacement(tmp_path, codec, original):
    source, output, converter = selected(tmp_path, original)
    with pytest.raises(ConversionError):
        converter.extract(source, 'reading.txt', encoding=codec)
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []


def test_utf16_record_bound_counts_actual_formfeeds_not_low_bytes(tmp_path):
    text = 'ЌЌ\fSecond'
    original = text.encode('utf-16-le')
    assert original.count(b'\f') == 3
    source, _, converter = selected(tmp_path, original, budget=ConversionBudget(max_records=2))
    result = converter.extract(source, 'reading.txt', encoding='utf-16-le')
    assert len(result.report['segments']) == 2
    assert_reading(result, source, original, text, 'utf-16-le')


def test_decoded_formfeeds_still_enforce_the_selected_record_budget(tmp_path):
    original = 'One\fTwo\fThree'.encode('utf-16-be')
    source, output, converter = selected(tmp_path, original, budget=ConversionBudget(max_records=2))
    with pytest.raises(ConversionError):
        converter.extract(source, 'reading.txt', encoding='utf-16-be')
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []


def test_explicit_codec_enforces_utf8_output_bytes_before_publication(tmp_path):
    original = 'Київ'.encode('utf-16-le')
    source, output, converter = selected(tmp_path, original, budget=ConversionBudget(max_text_bytes=7))
    with pytest.raises(ConversionError):
        converter.extract(source, 'reading.txt', encoding='utf-16-le')
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []


def test_batch_forwards_one_explicit_codec_and_preserves_each_original(tmp_path):
    sources = []
    originals = ['Перша\r\n'.encode('cp1251'), 'Друга\fкінець'.encode('cp1251')]
    for index, original in enumerate(originals):
        path = tmp_path / f'source-{index}.txt'
        path.write_bytes(original)
        sources.append(path)
    output = tmp_path / 'reading'
    output.mkdir()
    converter = ConversionService(output, input_roots=tuple(sources))
    progress = []
    results = converter.extract_many(sources, encoding='windows-1251',
                                    progress=lambda count, result: progress.append((count, result)))
    assert progress == list(enumerate(results, 1))
    assert len(results) == 2
    for source, original, result in zip(sources, originals, results):
        assert_reading(result, source, original, original.decode('cp1251'), 'cp1251')


@pytest.mark.parametrize('tamper', ['wrong', 'missing'])
def test_parent_rejects_a_worker_receipt_with_different_codec_authority(tmp_path, monkeypatch, tamper):
    original = 'Київ'.encode('cp1251')
    source, output, converter = selected(tmp_path, original)
    actual = service_module._invoke

    def changed(message, **options):
        result = actual(message, **options)
        assert result['state'] == 'complete'
        path = Path(message['report'])
        report = json.loads(path.read_text(encoding='utf-8'))
        if tamper == 'wrong':
            report['source_encoding'] = 'cp1252'
        else:
            report.pop('source_encoding')
        path.write_text(json.dumps(report), encoding='utf-8')
        return result

    monkeypatch.setattr(service_module, '_invoke', changed)
    with pytest.raises(ConversionError, match='invalid provenance'):
        converter.extract(source, 'reading.txt', encoding='cp1251')
    assert source.read_bytes() == original
    assert list(output.iterdir()) == []


def test_cancelled_explicit_conversion_does_not_start_a_worker(tmp_path, monkeypatch):
    source, output, converter = selected(tmp_path, 'Київ'.encode('cp1251'))
    calls = []
    monkeypatch.setattr(service_module, '_invoke', lambda *args, **kwargs: calls.append(args))
    with pytest.raises(InterruptedError):
        converter.extract(source, 'reading.txt', encoding='cp1251', cancelled=lambda: True)
    assert calls == []
    assert list(output.iterdir()) == []
