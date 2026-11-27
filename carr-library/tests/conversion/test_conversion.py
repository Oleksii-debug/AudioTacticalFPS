"""Actual supervised readers and publication boundaries for reading copies."""
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED

import pytest

from carr_library.conversion import ConversionBudget, ConversionError, ConversionService, ConversionUnavailable
from carr_library.conversion import service as service_module
from carr_library.conversion.capabilities import capabilities
from carr_library.extract.document_formats import SUPPORTED_SUFFIXES
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import _invoke


def selected(tmp_path, raw, suffix='.txt', **options):
    source = tmp_path / ('Київ-Žilina-東京' + suffix)
    source.write_bytes(raw)
    output = tmp_path / 'reading'
    output.mkdir()
    return source, output, ConversionService(output, input_roots=(source,), **options)


def assert_report(result, source):
    raw = source.read_bytes()
    assert result.report == json.loads(result.report_path.read_text(encoding='utf-8'))
    assert result.report['schema'] == 'carr-reading-derivative/1'
    assert result.report['source_sha256'] == sha256(raw).hexdigest()
    assert result.report['source_size'] == len(raw)
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert result.report['verified_for_quotation'] is False
    assert result.report['printed_pages_reconstructed'] is False
    text = result.path.read_bytes().decode('utf-8')
    for segment in result.report['segments']:
        start, end = segment['output_characters']
        assert 0 <= start <= end <= len(text)
        assert sha256(text[start:end].encode()).hexdigest() == segment['text_sha256']
        assert segment['printed_label'] is None
        assert segment['printed_label_verified'] is False


def test_actual_worker_keeps_utf8_crlf_formfeed_and_original(tmp_path):
    raw = b'\xef\xbb\xbf' + 'Перший\r\n  Žilina  \f東京\n'.encode()
    source, output, converter = selected(tmp_path, raw)
    result = converter.extract(source, 'reading.txt')
    assert source.read_bytes() == raw
    assert result.path.read_bytes() == raw[3:]
    assert len(result.report['segments']) == 2
    assert_report(result, source)
    assert sorted(path.name for path in output.iterdir()) == ['reading.txt', 'reading.txt.provenance.json']


@pytest.mark.parametrize(('suffix', 'raw', 'expected'), [
    ('.html', '<h1>Київ</h1><p>Žilina &amp; 東京.</p><script>do_not_run()</script>'.encode(), 'Київ\n\nŽilina & 東京.'),
    ('.md', '# Heading\n\nLiteral **text** and [link](https://example.invalid).\n'.encode(), '# Heading\n\nLiteral **text** and [link](https://example.invalid).\n'),
    ('.eml', b'Subject: Reading\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nBody without attachment.\r\n', 'Body without attachment.\r\n'),
], ids=['html-static', 'markdown-literal', 'email-body'])
def test_current_text_adapters_execute_in_supervised_worker(tmp_path, suffix, raw, expected):
    source, _, converter = selected(tmp_path, raw, suffix)
    result = converter.extract(source, 'reading.txt')
    assert result.path.read_bytes().decode('utf-8') == expected
    assert source.read_bytes() == raw
    assert_report(result, source)



def test_actual_fb2_reuses_existing_bounded_xml_reader(tmp_path):
    raw = ('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
           '<body><section><p>Перша</p><p>Žilina</p></section></body></FictionBook>').encode()
    source, _, converter = selected(tmp_path, raw, '.fb2')
    result = converter.extract(source, 'reading.txt')
    assert result.path.read_text(encoding='utf-8') == 'Перша\n\nŽilina'
    assert_report(result, source)


def test_actual_docx_uses_ordered_body_and_simple_tables(tmp_path):
    from docx import Document
    document = Document()
    document.add_paragraph('First paragraph.')
    row = document.add_table(rows=1, cols=2).rows[0]
    row.cells[0].text = 'Left'
    row.cells[1].text = 'Right'
    document.add_paragraph('Last paragraph.')
    stream = BytesIO()
    document.save(stream)
    source, _, converter = selected(tmp_path, stream.getvalue(), '.docx')
    result = converter.extract(source, 'reading.txt')
    assert result.path.read_text(encoding='utf-8') == 'First paragraph.\n\nLeft\n\nRight\n\nLast paragraph.'
    assert [row['logical_locator'] for row in result.report['segments']] == [[1], [2, 1, 1, 1], [2, 1, 2, 1], [3]]
    assert_report(result, source)


def epub_bytes():
    stream = BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_STORED) as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>')
        archive.writestr('EPUB/content.opf',
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Reading</dc:title>'
            '<dc:language>uk</dc:language></metadata><manifest>'
            '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>')
        for name, text in [('two', 'Друга'), ('one', 'Перша')]:
            archive.writestr('EPUB/' + name + '.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Reading</title></head>'
                '<body><p>' + text + '</p></body></html>')
    return stream.getvalue()


def test_actual_epub_uses_spine_instead_of_zip_order(tmp_path):
    source, _, converter = selected(tmp_path, epub_bytes(), '.epub')
    result = converter.extract(source, 'reading.txt')
    assert result.path.read_text(encoding='utf-8') == 'Перша\n\nДруга'
    assert_report(result, source)


def native_pdf_bytes():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for text in ('First page.', 'Second page.'):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({
            NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'),
            NameObject('/BaseFont'): NameObject('/Helvetica'),
        })
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font}),
        })
        content = DecodedStreamObject()
        content.set_data(('BT /F1 12 Tf 40 200 Td (' + text + ') Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = content
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_actual_pdf_keeps_physical_page_order_without_claiming_printed_labels(tmp_path):
    source, _, converter = selected(tmp_path, native_pdf_bytes(), '.pdf')
    result = converter.extract(source, 'reading.txt')
    parts = result.path.read_text(encoding='utf-8').split('\n\f\n')
    assert [value.strip() for value in parts] == ['First page.', 'Second page.']
    assert [row['physical_index'] for row in result.report['segments']] == [1, 2]
    assert_report(result, source)


@pytest.mark.parametrize(('budget', 'raw'), [
    (ConversionBudget(max_source_bytes=5), b'too long'),
    (ConversionBudget(max_text_bytes=3), b'four'),
    (ConversionBudget(max_records=1), b'one\ftwo'),
], ids=['input-bytes', 'output-bytes', 'record-count'])
def test_real_resource_rejections_publish_nothing(tmp_path, budget, raw):
    source, output, converter = selected(tmp_path, raw, budget=budget)
    with pytest.raises(ConversionError):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes() == raw
    assert list(output.iterdir()) == []


def test_actual_pdf_page_budget_is_checked_before_publication(tmp_path):
    source, output, converter = selected(tmp_path, native_pdf_bytes(), '.pdf',
                                         budget=ConversionBudget(max_pages=1))
    with pytest.raises(ConversionError, match='page budget'):
        converter.extract(source, 'reading.txt')
    assert list(output.iterdir()) == []


def test_cancel_during_real_private_source_copy_keeps_original(tmp_path):
    raw = b'original\n' * (400_000)
    source, output, converter = selected(tmp_path, raw)
    observed = []

    def cancelled():
        snapshots = list(output.glob('.carr-conversion-*/source.txt'))
        copied = bool(snapshots and snapshots[0].stat().st_size >= 1024 * 1024)
        observed.append(copied)
        return copied

    with pytest.raises(InterruptedError):
        converter.extract(source, 'reading.txt', cancelled=cancelled)
    assert any(observed)
    assert source.read_bytes() == raw
    assert list(output.iterdir()) == []


def test_source_change_after_actual_worker_rejects_publication(tmp_path, monkeypatch):
    source, output, converter = selected(tmp_path, b'original content')
    original = service_module._invoke

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        source.write_bytes(b'changed content!')
        return result

    monkeypatch.setattr(service_module, '_invoke', changed)
    with pytest.raises((ConversionError, RuntimeError, ValueError), match='changed|replaced'):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes() == b'changed content!'
    assert list(output.iterdir()) == []


def test_only_explicit_inputs_and_output_root_are_authorized(tmp_path):
    source, output, converter = selected(tmp_path, b'original')
    neighbor = tmp_path / 'neighbor.txt'
    neighbor.write_bytes(b'not selected')
    with pytest.raises(PermissionError):
        converter.extract(neighbor, 'reading.txt')
    with pytest.raises((PermissionError, ValueError)):
        converter.extract(source, '../escape.txt')
    assert not (tmp_path / 'escape.txt').exists()
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('component', ['CON.txt', 'unsafe:stream.txt', 'trailing .txt.'],
                         ids=['device-name', 'alternate-stream', 'trailing-dot'])
def test_portable_filename_rejection_precedes_worker(tmp_path, component, monkeypatch):
    source, output, converter = selected(tmp_path, b'original')
    monkeypatch.setattr(service_module, '_invoke', lambda *a, **k: pytest.fail('invalid target reached worker'))
    with pytest.raises((ValueError, PermissionError)):
        converter.extract(source, component)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize('existing', ['reading.txt', 'reading.txt.provenance.json'], ids=['text', 'report'])
def test_existing_output_or_report_never_changes(tmp_path, existing, monkeypatch):
    source, output, converter = selected(tmp_path, b'original')
    target = output / existing
    target.write_bytes(b'owned by user')
    monkeypatch.setattr(service_module, '_invoke', lambda *a, **k: pytest.fail('existing target reached worker'))
    with pytest.raises(FileExistsError):
        converter.extract(source, 'reading.txt')
    assert target.read_bytes() == b'owned by user'
    assert list(output.iterdir()) == [target]


def test_receipt_collision_rolls_back_only_new_own_text(tmp_path, monkeypatch):
    source, output, converter = selected(tmp_path, b'original')
    original = service_module.publish_noreplace

    def raced(stage, target):
        if target.name.endswith('.provenance.json'):
            target.write_bytes(b'other writer')
        return original(stage, target)

    monkeypatch.setattr(service_module, 'publish_noreplace', raced)
    with pytest.raises(FileExistsError):
        converter.extract(source, 'reading.txt')
    assert not (output / 'reading.txt').exists()
    assert (output / 'reading.txt.provenance.json').read_bytes() == b'other writer'


def test_post_move_sync_failure_preserves_matching_pair_but_is_not_success(tmp_path, monkeypatch):
    source, output, converter = selected(tmp_path, b'original')
    original = service_module.publish_noreplace

    def failed_after_move(stage, target):
        original(stage, target)
        if target.name.endswith('.provenance.json'):
            raise OSError('synthetic directory sync failure after receipt move')

    monkeypatch.setattr(service_module, 'publish_noreplace', failed_after_move)
    with pytest.raises(OSError, match='directory sync'):
        converter.extract(source, 'reading.txt')
    report = json.loads((output / 'reading.txt.provenance.json').read_text(encoding='utf-8'))
    assert sha256((output / 'reading.txt').read_bytes()).hexdigest() == report['text_sha256']
    assert source.read_bytes() == b'original'


@pytest.mark.parametrize('changed', ['none', 'replacement', 'same-stat-edit'])
def test_first_post_move_sync_failure_removes_only_unchanged_own_text(tmp_path, monkeypatch, changed):
    source, output, converter = selected(tmp_path, b'original')
    original = service_module.publish_noreplace

    def failed_after_first_move(stage, target):
        original(stage, target)
        assert target.name == 'reading.txt'
        if changed == 'replacement':
            replacement = output / 'other-writer.txt'
            replacement.write_bytes(b'other writer')
            os.replace(replacement, target)
        elif changed == 'same-stat-edit':
            before = target.stat()
            target.write_bytes(b'changed!')
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        raise OSError('synthetic directory sync failure after text move')

    monkeypatch.setattr(service_module, 'publish_noreplace', failed_after_first_move)
    with pytest.raises(OSError, match='directory sync failure after text move'):
        converter.extract(source, 'reading.txt')
    assert source.read_bytes() == b'original'
    assert not (output / 'reading.txt.provenance.json').exists()
    if changed == 'none':
        assert list(output.iterdir()) == []
        monkeypatch.setattr(service_module, 'publish_noreplace', original)
        # The same intended destination is usable after the failed attempt.
        assert_report(converter.extract(source, 'reading.txt'), source)
    else:
        expected = b'other writer' if changed == 'replacement' else b'changed!'
        assert (output / 'reading.txt').read_bytes() == expected
        assert [path.name for path in output.iterdir()] == ['reading.txt']


def test_actual_batch_preserves_completed_first_file_when_later_reader_rejects(tmp_path):
    good = tmp_path / 'good.txt'
    bad = tmp_path / 'bad.txt'
    good.write_bytes(b'first survives')
    bad.write_bytes(b'\xffinvalid UTF8')
    output = tmp_path / 'reading'
    output.mkdir()
    converter = ConversionService(output, input_roots=(good, bad))
    completed = []
    with pytest.raises(ConversionError):
        converter.extract_many((good, bad), progress=lambda number, result: completed.append(result))
    assert len(completed) == 1
    assert_report(completed[0], good)
    assert len(list(output.iterdir())) == 2
    assert bad.read_bytes() == b'\xffinvalid UTF8'


def test_worker_rejects_wrong_action_and_extra_request_keys(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'original')
    message = dict(action='convert', source=str(source), output=str(tmp_path / 'reading.txt'),
        report=str(tmp_path / 'details.json'), sha256=sha256(source.read_bytes()).hexdigest(),
        source_size=8, budget=asdict(ConversionBudget()), ocr_config=asdict(Config()), timeout=120)
    for invalid in (dict(message, action='other'), dict(message, extra='unexpected')):
        result = _invoke(invalid, timeout=30)
        assert result['state'] == 'failed'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['source.txt']


def test_capability_inventory_matches_current_registry_and_never_enables_legacy(tmp_path):
    rows = {row['extension']: row for row in capabilities()}
    assert {suffix for suffix, row in rows.items() if row['qualification'] != 'not_integrated'} == set(SUPPORTED_SUFFIXES)
    for suffix in ('.doc', '.rtf', '.djvu', '.azw3', '.mobi', '.png', '.tiff'):
        assert rows[suffix]['available'] is False
    source, output, converter = selected(tmp_path, b'legacy', '.doc')
    with pytest.raises(ConversionUnavailable):
        converter.extract(source, 'reading.txt')
    assert list(output.iterdir()) == []


def test_capability_rejects_reader_version_the_existing_docx_parser_rejects(monkeypatch):
    import importlib
    module = importlib.import_module('carr_library.conversion.capabilities')
    original = module.version
    monkeypatch.setattr(module, 'version', lambda name: '1.1.0' if name == 'python-docx' else original(name))
    row = next(row for row in module.capabilities() if row['extension'] == '.docx')
    assert row['available'] is False
    assert any('python-docx==1.2.0' in required for required in row['missing'])


def test_unicode_batch_name_fits_portable_component_budget(tmp_path):
    source = tmp_path / ('界' * 80 + '.txt')
    source.write_bytes('Exact source.'.encode())
    output = tmp_path / 'reading'
    output.mkdir()
    results = ConversionService(output, input_roots=(source,)).extract_many((source,))
    assert len(results) == 1
    assert len(results[0].report_path.name.encode('utf-8')) < 255
    assert_report(results[0], source)
