from dataclasses import replace
from email.message import EmailMessage
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from threading import Thread
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

import pytest

from carr_library.conversion import ConversionService, ConversionBudget, ConversionError, ConversionUnavailable


@pytest.fixture
def service(tmp_path):
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    return ConversionService(outputs, input_roots=(inputs,)), inputs, outputs


def write_zip(path, members):
    with ZipFile(path, 'w') as archive:
        for name, content in members.items():
            archive.writestr(name, content, compress_type=ZIP_STORED if name == 'mimetype' else ZIP_DEFLATED)


def converted(service, source):
    converter, _, outputs = service
    before = source.read_bytes()
    result = converter.extract(source, source.stem + '.txt')
    assert source.read_bytes() == before
    assert result.report['source_sha256'] == sha256(before).hexdigest()
    assert result.report['verified_for_quotation'] is False
    assert result.report['original_modified'] is False
    assert result.report['printed_pages_reconstructed'] is False
    assert result.report['text_sha256'] == sha256(result.path.read_bytes()).hexdigest()
    assert json.loads(result.report_path.read_text(encoding='utf-8')) == result.report
    return result.path.read_text(encoding='utf-8'), result.report


def test_exact_unicode_txt_and_source_hash(service):
    converter, inputs, outputs = service
    source = inputs / 'Український.txt'
    raw = 'Точний текст.\r\nĽudská reč. English text.'
    source.write_bytes(raw.encode('utf-8'))
    result = converter.extract(source, 'reading.txt')
    assert result.path.read_bytes() == raw.encode('utf-8')
    assert result.report['verified_for_quotation'] is False
    assert result.report['sections'][0]['physical_index'] is None


def test_decode_requires_explicit_legacy_encoding(service):
    converter, inputs, outputs = service
    source = inputs / 'legacy.txt'
    source.write_bytes('Український текст'.encode('cp1251'))
    with pytest.raises(ConversionError, match='encoding|decoding'):
        converter.extract(source, 'no.txt')
    assert not (outputs / 'no.txt').exists()
    result = converter.extract(source, 'yes.txt', encoding='cp1251')
    assert result.path.read_text(encoding='utf-8') == 'Український текст'


def test_html_scripts_frames_styles_entities_and_alt_are_safe(service):
    _, inputs, _ = service
    source = inputs / 'web.html'
    source.write_text('<html><head><title>Metadata</title><style>secret CSS</style></head><body>'
                      '<h1>Heading &amp; text</h1><p>Текст <b>bold</b>.</p>'
                      '<script>fetch("https://example.com/private")</script>'
                      '<iframe src="file:///private">hidden iframe</iframe>'
                      '<img src="https://example.com/picture" alt="A figure"></body></html>', encoding='utf-8')
    result, report = converted(service, source)
    assert 'Heading & text' in result and 'Текст bold.' in result
    assert 'Image description: A figure' in result
    assert all(value not in result for value in ('secret CSS', 'fetch(', 'hidden iframe', 'Metadata'))


def test_eml_chooses_body_and_never_reads_attachment(service):
    _, inputs, _ = service
    message = EmailMessage()
    message['Subject'] = 'Дослідження'
    message.set_content('Plain body. Точний текст.')
    message.add_alternative('<p>HTML alternative</p>', subtype='html')
    message.add_attachment(b'attachment must not be read', maintype='application', subtype='octet-stream', filename='private.txt')
    source = inputs / 'email.eml'
    source.write_bytes(message.as_bytes())
    result, report = converted(service, source)
    assert 'Plain body.' in result and 'Точний текст.' in result
    assert 'attachment' not in result and 'HTML alternative' not in result
    assert report['metadata']['subject'] == 'Дослідження'


def test_real_docx_reuses_reviewed_parser(service):
    from docx import Document
    _, inputs, _ = service
    source = inputs / 'document.docx'
    doc = Document()
    doc.add_heading('Chapter', level=1)
    doc.add_paragraph('Exact Ukrainian: Точний текст.')
    doc.save(source)
    result, report = converted(service, source)
    assert 'Chapter' in result and 'Точний текст.' in result
    assert report['engine'] == 'existing-reviewed-docx-adapter'
    assert all(s['physical_index'] is None for s in report['sections'])


def test_pdf_physical_page_positions_are_real_and_unverified(service):
    from reportlab.pdfgen import canvas
    _, inputs, _ = service
    source = inputs / 'pages.pdf'
    pdf = canvas.Canvas(str(source))
    pdf.drawString(60, 700, 'Page one source text')
    pdf.showPage()
    pdf.drawString(60, 700, 'Second physical page')
    pdf.save()
    result, report = converted(service, source)
    assert 'Page one' in result and 'Second physical' in result
    assert [s['physical_index'] for s in report['sections']] == [1, 2]
    assert all(s['printed_label'] is None for s in report['sections'])


@pytest.mark.parametrize('suffix,kind,body', [
    ('.odt', 'text', '<text:h>Heading</text:h><text:p>Hello<text:s text:c="2"/>world</text:p>'),
    ('.odp', 'presentation', '<draw:page draw:name="slide"><draw:frame><draw:text-box><text:p>Slide text</text:p></draw:text-box></draw:frame></draw:page>'),
    ('.ods', 'spreadsheet', '<table:table table:name="Budget"><table:table-row><table:table-cell office:value-type="string"><text:p>Stored value</text:p></table:table-cell></table:table-row><table:table-row table:number-rows-repeated="1048576"><table:table-cell table:number-columns-repeated="16384"/></table:table-row></table:table>'),
])
def test_safe_opendocument_text_spreadsheet_and_slides(service, suffix, kind, body):
    _, inputs, _ = service
    source = inputs / ('open' + suffix)
    content = ('<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
               'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" office:version="1.2">'
               '<office:body><office:' + kind + '>' + body + '</office:' + kind + '></office:body></office:document-content>')
    write_zip(source, {'mimetype': 'application/vnd.oasis.opendocument.' + kind, 'content.xml': content})
    result, report = converted(service, source)
    expected = {'.odt': 'Hello  world', '.odp': 'Slide text', '.ods': 'Stored value'}[suffix]
    assert expected in result
    assert report['engine'] == 'safe-opendocument-xml/1'


def test_actual_xlsx_cached_cells_do_not_evaluate_formulas(service):
    from openpyxl import Workbook
    _, inputs, _ = service
    source = inputs / 'workbook.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Дані'
    sheet['A1'] = 'Текст клітинки'
    sheet['B1'] = 42
    sheet['C1'] = '=HYPERLINK("https://example.com/private","external")'
    hidden = workbook.create_sheet('Hidden')
    hidden.sheet_state = 'hidden'
    hidden['A1'] = 'Do not expose hidden sheet'
    workbook.save(source)
    result, report = converted(service, source)
    assert 'A1: Текст клітинки' in result and 'B1: 42' in result
    assert '[Formula without a stored result]' in result
    assert 'HYPERLINK' not in result and 'Do not expose' not in result


def test_presentation_order_uses_relationship_order(service):
    _, inputs, _ = service
    source = inputs / 'slides.pptx'
    # Valid complete synthetic presentation, generated with python-pptx 1.0.2.
    # Fixture order is slide2 then slide1; archive filename sorting is wrong.
    source.write_bytes((Path(__file__).parent / 'fixtures' / 'ordered_slides.pptx').read_bytes())
    result, report = converted(service, source)
    assert result.index('Earlier') < result.index('Later')
    assert len(report['sections']) == 2


@pytest.mark.parametrize('suffix', ['.fb2', '.fb3'])
def test_fictionbook_text_retains_unicode(service, suffix):
    _, inputs, _ = service
    source = inputs / ('book' + suffix)
    if suffix == '.fb2':
        source.write_text('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section><p>Читання <emphasis>книги</emphasis>.</p></section></body></FictionBook>', encoding='utf-8')
    else:
        write_zip(source, {'[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/fb3/body.xml" ContentType="application/fb3-body+xml"/></Types>',
            'fb3/body.xml': '<fb3-body xmlns="http://www.fictionbook.org/FictionBook3/body"><section><p>Читання <em>книги</em>.</p></section></fb3-body>'})
    result, report = converted(service, source)
    assert 'Читання книги.' in result


def test_existing_epub_spine_reader_is_reused(service):
    _, inputs, _ = service
    source = inputs / 'book.epub'
    write_zip(source, {'mimetype': 'application/epub+zip',
        'META-INF/container.xml': '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        'OEBPS/book.opf': '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">synthetic</dc:identifier><dc:title>Book</dc:title><dc:language>en</dc:language></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>',
        'OEBPS/chapter.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head><body><h1>Heading</h1><p>Ordered EPUB body.</p></body></html>'})
    result, report = converted(service, source)
    assert 'Ordered EPUB body.' in result
    assert report['engine'] == 'existing-reviewed-epub-adapter'


def test_archives_reject_traversal_entities_and_active_code(service):
    converter, inputs, outputs = service
    cases = {
        'traversal.odt': {'../outside': 'bad', 'mimetype': 'application/vnd.oasis.opendocument.text', 'content.xml': '<document/>'},
        'macro.xlsx': {'xl/vbaProject.bin': b'bad'},
    }
    for name, files in cases.items():
        source = inputs / name
        write_zip(source, files)
        with pytest.raises(ConversionError):
            converter.extract(source, name + '.txt')
    source = inputs / 'entity.fb2'
    source.write_text('<!DOCTYPE foo [<!ENTITY private SYSTEM "file:///private">]><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><p>&private;</p></body></FictionBook>')
    with pytest.raises(ConversionError):
        converter.extract(source, 'entity.txt')
    assert not list(outputs.iterdir())


def test_cancellation_budget_and_no_overwrite_keep_sources(service):
    converter, inputs, outputs = service
    source = inputs / 'source.txt'
    source.write_text('original', encoding='utf-8')
    with pytest.raises(InterruptedError):
        converter.extract(source, 'never.txt', cancelled=lambda: True)
    assert not list(outputs.iterdir())
    converter.extract(source, 'reading.txt')
    before = (outputs / 'reading.txt').read_bytes()
    with pytest.raises(FileExistsError):
        converter.extract(source, 'reading.txt')
    assert (outputs / 'reading.txt').read_bytes() == before
    tight = ConversionService(outputs, input_roots=(inputs,), budget=replace(ConversionBudget(), max_source_bytes=2))
    with pytest.raises(ConversionError, match='budget'):
        tight.extract(source, 'too-large.txt')


@pytest.mark.parametrize('suffix', ['.azw', '.azw3', '.chm', '.doc', '.xls', '.wpd', '.mobi', '.lit', '.tcr'])
def test_unverified_legacy_formats_are_not_falsely_supported(service, suffix):
    converter, inputs, outputs = service
    source = inputs / ('legacy' + suffix)
    source.write_bytes(b'not a real legacy document')
    with pytest.raises(ConversionUnavailable):
        converter.extract(source, 'never.txt')
    assert not list(outputs.iterdir())


def test_read_and_write_scope_cannot_escape_or_follow_links(service, tmp_path):
    converter, inputs, outputs = service
    outside = tmp_path / 'private.txt'
    outside.write_text('private')
    with pytest.raises(PermissionError):
        converter.extract(outside, 'never.txt')
    source = inputs / 'allowed.txt'
    source.write_text('allowed')
    with pytest.raises(ValueError):
        converter.extract(source, '../escape.txt')
    linked = inputs / 'link.txt'
    linked.symlink_to(outside)
    with pytest.raises(ValueError):
        converter.extract(linked, 'never.txt')


def test_actual_installed_pandoc_rtf_or_explicit_unavailable(service):
    converter, inputs, outputs = service
    source = inputs / 'rich.rtf'
    source.write_bytes(br'{\rtf1\ansi First paragraph.\par Second \b bold\b0\u8212? text.}')
    if shutil.which('pandoc') is None:
        with pytest.raises(ConversionUnavailable):
            converter.extract(source, 'rich.txt')
        return
    result, report = converted(service, source)
    assert 'First paragraph.' in result and 'Second bold' in result
    assert report['engine'] == 'installed-pandoc-sandbox'


def test_actual_pandoc_does_not_fetch_iframe_or_execute_filters(service, tmp_path):
    converter, inputs, outputs = service
    source = inputs / 'untrusted.html'
    if shutil.which('pandoc') is None:
        source.write_text('<p>Text</p>')
        with pytest.raises(ConversionUnavailable):
            converter.extract(source, 'reading.txt', adapter='pandoc')
        return
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'SECRET_RESPONSE')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    private = tmp_path / 'private.txt'
    private.write_text('SECRET_LOCAL')
    source.write_text(f'<p>Visible text</p><iframe src="http://127.0.0.1:{server.server_port}/private"></iframe>'
                      f'<iframe src="{private.as_uri()}"></iframe><script>shell()</script>')
    try:
        result = converter.extract(source, 'reading.txt', adapter='pandoc')
        text = result.path.read_text()
        assert 'Visible text' in text and 'SECRET' not in text
        assert not requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_actual_local_image_ocr_requires_selected_languages(service):
    from PIL import Image, ImageDraw, ImageFont
    converter, inputs, outputs = service
    source = inputs / 'synthetic.png'
    image = Image.new('RGB', (1100, 180), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    draw.text((30, 45), 'SYNTHETIC LOCAL OCR', fill='black', font=font)
    image.save(source)
    with pytest.raises(ConversionUnavailable, match='languages'):
        converter.extract(source, 'no-language.txt')
    from carr_library.extract.ocr_runtime import discover_ocr_runtime
    if discover_ocr_runtime('eng') is None:
        with pytest.raises(ConversionUnavailable):
            converter.extract(source, 'image.txt', ocr_languages=('eng',))
        return
    result = converter.extract(source, 'image.txt', ocr_languages=('eng',))
    assert 'SYNTHETIC LOCAL OCR' in result.path.read_text(encoding='utf-8')
    assert result.report['verified_for_quotation'] is False
    assert result.report['metadata']['languages'] == ['eng']
    assert result.report['metadata']['image_frames'] == 1
    assert result.report['metadata']['image_pixels'] == 1100 * 180


@pytest.mark.parametrize('limit', [
    {'max_pages': 1}, {'max_image_pixels': 500}, {'max_total_image_pixels': 1000},
])
def test_image_worker_bounds_all_tiff_frames_before_ocr(service, monkeypatch, limit):
    from PIL import Image
    from carr_library.extract.ocr_runtime import OcrRuntime
    import carr_library.extract.ocr_runtime as discovery
    converter, inputs, outputs = service
    source = inputs / 'frames.tiff'
    first, second = Image.new('RGB', (20, 20), 'white'), Image.new('RGB', (30, 30), 'white')
    first.save(source, save_all=True, append_images=[second])
    original = source.read_bytes()
    # The isolated metadata reader must reject this before invoking any OCR.
    monkeypatch.setattr(discovery, 'discover_ocr_runtime', lambda *_args, **_kwargs: OcrRuntime('never-executed'))
    bounded = ConversionService(outputs, input_roots=(inputs,), budget=replace(ConversionBudget(), **limit))
    with pytest.raises(ConversionError, match='budget'):
        bounded.extract(source, 'frames.txt', ocr_languages=('eng',))
    assert source.read_bytes() == original
    assert not (outputs / 'frames.txt').exists()
    assert not (outputs / 'frames.txt.provenance.json').exists()


def test_provenance_publication_failure_rolls_back_new_derivative(service, monkeypatch):
    converter, inputs, outputs = service
    source = inputs / 'original.txt'
    source.write_text('Keep this source unchanged.', encoding='utf-8')
    original_link = os.link
    def fail_receipt(incoming, destination, *args, **kwargs):
        if str(destination).endswith('.provenance.json'):
            raise OSError('simulated disk full')
        return original_link(incoming, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'link', fail_receipt)
    with pytest.raises(OSError, match='disk full'):
        converter.extract(source, 'reading.txt')
    assert not list(outputs.iterdir())
    assert source.read_text() == 'Keep this source unchanged.'


def test_source_change_between_snapshot_and_publication_is_rejected(service):
    converter, inputs, outputs = service
    source = inputs / 'original.txt'
    source.write_text('original', encoding='utf-8')
    changed = False
    def mutate_during_child():
        nonlocal changed
        if not changed and next(outputs.glob('.pending-conversion-*/request.json'), None) is not None:
            source.write_text('modified', encoding='utf-8')
            changed = True
        return False
    with pytest.raises(ConversionError, match='source changed'):
        converter.extract(source, 'reading.txt', cancelled=mutate_during_child)
    assert changed
    assert not list(outputs.iterdir())
