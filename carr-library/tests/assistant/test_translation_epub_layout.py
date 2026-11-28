"""Real EPUB/XHTML layout workers and current source-bound translation/1 jobs."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED
import zlib

import pytest

from carr_library.assistant import translation_layout as layout
from carr_library.assistant import translation_epub_layout_worker as worker
from carr_library.assistant.models import canonical
from carr_library.assistant.providers import HTTPResult, HTTPTransport
from carr_library.durable_state import encode, read_json
from carr_library.extract.epub_ordered import extract_epub, XHTML, OPF, DC
from carr_library.operations.broker import ResourceBroker

_spec = importlib.util.spec_from_file_location('epub_current_gateway',
    Path(__file__).with_name('test_library_translation.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
setup = _base.setup


def png_bytes():
    def chunk(name, data):
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', zlib.crc32(name + data))
    return (b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(b'\x00\x22\x44\x66\xff')) + chunk(b'IEND', b''))


def epub_bytes(*, chapters=2, first_body=None, repeated=False, overlay=False):
    manifest, spine, navigation, payloads = [], [], [], {}
    for index in range(1, chapters + 1):
        href = 'chapter-' + str(index) + '.xhtml'
        manifest.append('<item id="chapter-' + str(index) + '" href="' + href
                        + '" media-type="application/xhtml+xml"'
                        + (' media-overlay="overlay"' if overlay and index == 1 else '') + '/>')
        spine.append('<itemref idref="chapter-' + str(index) + '"/>')
        navigation.append('<li><a href="' + href + '#heading">Original chapter ' + str(index) + '</a></li>')
        body = ('<h1 id="heading" class="chapter"><a href="#table"><strong>Selected original Ω '
            + str(index) + ' </strong><em>second style.</em></a></h1>'
            '<table id="table" class="retained-table"><tbody><tr>'
            '<td>Left selected cell ' + str(index) + '.</td><td>Right selected cell '
            + str(index) + '.</td></tr></tbody></table>'
            '<img src="image.png" alt="Original image alternative"/>')
        if index == 1 and first_body is not None:
            body = first_body
        # A substituted body removes the default heading, but the unchanged
        # navigation still targets #heading. Keep that anchor on the body at
        # text offset zero without adding prose or another translation unit.
        body_open = '<body id="heading">' if index == 1 and first_body is not None else '<body>'
        payloads['OEBPS/' + href] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">'
            '<head><title>Untranslated original chapter ' + str(index) + '</title>'
            '<link rel="stylesheet" type="text/css" href="styles.css"/></head>'
            + body_open + body + '</body></html>').encode('utf-8')
    if repeated:
        spine.append('<itemref idref="chapter-1"/>')
    if overlay:
        manifest.append('<item id="overlay" href="overlay.smil" media-type="application/smil+xml"/>')
        payloads['OEBPS/overlay.smil'] = b'<smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body/></smil>'
    package = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="pub-id">urn:uuid:11111111-2222-4333-8444-555555555555</dc:identifier>'
        '<dc:title>Original retained EPUB fixture</dc:title><dc:creator>Synthetic source</dc:creator>'
        '<dc:language>en</dc:language><meta property="dcterms:modified">2026-11-27T00:00:00Z</meta>'
        '</metadata><manifest>' + ''.join(manifest)
        + '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        '<item id="style" href="styles.css" media-type="text/css"/>'
        '<item id="image" href="image.png" media-type="image/png"/>'
        '</manifest><spine>' + ''.join(spine) + '</spine></package>')
    payloads['OEBPS/content.opf'] = package.encode('utf-8')
    payloads['OEBPS/nav.xhtml'] = (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">'
        '<head><title>Original navigation</title></head><body><nav epub:type="toc" id="toc">'
        '<h1>Original contents</h1><ol>' + ''.join(navigation)
        + '</ol></nav></body></html>').encode('utf-8')
    payloads['OEBPS/styles.css'] = b'.chapter { color: #123456; } .retained-table { border: 1px solid black; }'
    payloads['OEBPS/image.png'] = png_bytes()
    payloads['META-INF/container.xml'] = (
        b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
        b'<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        b'</rootfiles></container>')
    result = BytesIO()
    with ZipFile(result, 'w') as output:
        output.writestr('mimetype', b'application/epub+zip', compress_type=ZIP_STORED)
        for name, data in payloads.items():
            output.writestr(name, data, compress_type=ZIP_DEFLATED)
    return result.getvalue()


def replace_members(data, changes):
    result = BytesIO()
    with ZipFile(BytesIO(data)) as original, ZipFile(result, 'w') as output:
        for item in original.infolist():
            output.writestr(item, changes.pop(item.filename, original.read(item)))
        for name, raw in changes.items():
            output.writestr(name, raw)
    return result.getvalue()


@pytest.fixture
def bound(tmp_path, setup):
    source = tmp_path / 'Selected original Ω.epub'
    raw = epub_bytes()
    source.write_bytes(raw)
    result = setup.app._import_file(setup.workspace, source, title='Retained EPUB Ω',
        authors=('Synthetic source',), work_id='epub-layout-work', edition_id='epub-layout-edition')
    assert result['state'] == 'complete' and result['verified'] is False
    yield SimpleNamespace(**vars(setup), source=source, raw=raw, edition='epub-layout-edition')


def captured(case):
    return layout.capture_layout(case.app, case.workspace, case.edition, kind='epub')


def prepared(case, selected):
    review = layout.review_layout(case.service, case.profile, selected, 'en', 'uk', '1')
    return layout.prepare_layout(case.service, case.profile, selected, 'en', 'uk', '1',
                                 approved_review=review['review_sha256'])


def complete(case):
    selected = captured(case)
    identity = prepared(case, selected)
    assert case.service.resume(identity)['status'] == 'complete'
    return selected, identity


def response(case, monkeypatch, text):
    actual = HTTPTransport.request
    def request(self, profile, route, body, headers, *, method='POST', **kwargs):
        if method != 'POST':
            return actual(self, profile, route, body, headers, method=method, **kwargs)
        case.calls.append((method, json.loads(body)))
        raw = json.dumps({'data': {'translations': [{
            'translatedText': text, 'detectedSourceLanguage': 'en'}]}}, ensure_ascii=False).encode('utf-8')
        return HTTPResult('application/json', raw, 200)
    monkeypatch.setattr(HTTPTransport, 'request', request)


def test_mapping_uses_exact_canonical_spine_text_and_complete_nonoverlapping_xhtml_ranges():
    raw = epub_bytes()
    source = extract_epub(raw)
    mapped = worker.describe(raw)
    assert len(mapped['records']) == 2 and mapped['selected'] == 6
    for canonical_row, row in zip(source['records'], mapped['records']):
        assert row['text'] == canonical_row['raw_text']
        assert row['locator'] == canonical_row['logical_locator']
        assert row['sha256'] == canonical_row['text_sha256']
        previous = 0
        for unit in row['units']:
            assert not row['text'][previous:unit['start']].strip()
            assert sha256(row['text'][unit['start']:unit['end']].encode()).hexdigest() == unit['sha256']
            previous = unit['end']
        assert not row['text'][previous:].strip()
    assert mapped['records'][0]['units'][0]['inline_mode'] == 'one-whole-block-hyperlink'
    assert sha256(raw).hexdigest() == mapped['source_sha256']


def test_actual_epub_capture_and_review_bind_one_whole_original_without_external_calls(bound, monkeypatch):
    calls, vault = list(bound.calls), list(bound.vault_calls)
    invocations, original = [], layout.run_process
    def observe(args, **kwargs):
        invocations.append(args)
        assert Path(args[-2]).resolve() == Path(worker.__file__).resolve()
        assert args[1:5] == ['-I', '-B', '-X', 'utf8'] and kwargs['owned_tree'] is True
        assert len(ResourceBroker.for_app(bound.app.root).snapshot()['leases']) == 1
        return original(args, **kwargs)
    monkeypatch.setattr(layout, 'run_process', observe)
    selected = captured(bound)
    identity = prepared(bound, selected)
    job = bound.service.get(identity)
    assert len(invocations) == 3 and bound.calls == calls and bound.vault_calls == vault
    assert bound.service.jobs.identities() == (identity,)
    assert job['source_partition']['profile'] == worker.PROFILE
    source, = job['source_binding']['sources']
    assert len(source['anchor']['positions']) == 2 and source['selection'] == 'document'
    assert source['anchor']['quotation_authority'] is False
    assert sum(bool(chunk.strip()) for chunk in job['chunks']) == 6
    assert ''.join(job['chunks']) == selected.source_text
    assert not ResourceBroker.for_app(bound.app.root).snapshot()['leases']


def test_actual_epub_export_preserves_dom_links_styles_media_and_canonical_source(bound, tmp_path):
    selected, identity = complete(bound)
    calls, vault = list(bound.calls), list(bound.vault_calls)
    output = tmp_path / 'New translated Ω.epub'
    receipt = layout.export_layout(bound.service, identity, output)
    assert receipt['status'] == 'complete' and receipt['format'] == 'epub'
    assert receipt['quotation_authority'] is False and len(receipt['blocks']) == 6
    assert receipt['job_id'] == identity
    assert receipt['output_sha256'] == sha256(output.read_bytes()).hexdigest()
    with ZipFile(BytesIO(bound.raw)) as before, ZipFile(output) as after:
        assert before.namelist() == after.namelist()
        changed = set(receipt['changed_members'])
        assert changed == {'OEBPS/content.opf', 'OEBPS/chapter-1.xhtml', 'OEBPS/chapter-2.xhtml'}
        for name in before.namelist():
            if name not in changed:
                assert before.read(name) == after.read(name)
        chapter = ET.fromstring(after.read('OEBPS/chapter-1.xhtml'))
        heading = chapter.find('.//' + XHTML + 'h1')
        assert heading.get('id') == 'heading' and heading.get('class') == 'chapter'
        assert heading.find(XHTML + 'a').get('href') == '#table' and heading.get('lang') == 'uk'
        assert heading.find('.//' + XHTML + 'strong').text.startswith('Переклад: Selected original Ω')
        assert heading.find('.//' + XHTML + 'em') is not None
        assert chapter.find('.//' + XHTML + 'table').get('class') == 'retained-table'
        assert all(''.join(cell.itertext()).startswith('Переклад: ')
                   for cell in chapter.findall('.//' + XHTML + 'td'))
        assert chapter.find('.//' + XHTML + 'img').get('alt') == 'Original image alternative'
        package = ET.fromstring(after.read('OEBPS/content.opf'))
        assert [row.text for row in package.findall('.//' + DC + 'language')] == ['uk', 'en']
    assert bound.source.read_bytes() == bound.raw
    assert bound.app.assets.resolve(selected.mapping['source_sha256']).read_bytes() == bound.raw
    parsed = extract_epub(output.read_bytes())
    assert len(parsed['records']) == 2
    assert all('Переклад: ' in row['raw_text'] for row in parsed['records'])
    assert bound.calls == calls and bound.vault_calls == vault


@pytest.mark.parametrize('mode', ['repeated', 'overlay', 'script', 'hidden', 'mixed-link',
                                 'inline-break', 'inline-image', 'unmapped-text', 'signature'])
def test_unsupported_epub_layouts_fail_before_any_translation_admission(mode):
    options = {}
    if mode == 'repeated':
        options['repeated'] = True
    elif mode == 'overlay':
        options['overlay'] = True
    else:
        options['first_body'] = {
            'script': '<script>never_execute()</script><p>Original.</p>',
            'hidden': '<p hidden="">Original hidden.</p><p>Visible.</p>',
            'mixed-link': '<p>Original outside <a href="#target">linked span</a>.</p><p id="target">Target.</p>',
            'inline-break': '<p>Original<br/>second line.</p>',
            'inline-image': '<p>Original<img src="image.png" alt="Image"/>after image.</p>',
            'unmapped-text': 'Unwrapped body prose.<p>Wrapped original.</p>',
            'signature': '<p>Original.</p>',
        }[mode]
    data = epub_bytes(**options)
    if mode == 'signature':
        data = replace_members(data, {'META-INF/signatures.xml': b'<signatures/>'})
    digest = sha256(data).hexdigest()
    with pytest.raises(ValueError):
        worker.describe(data)
    assert sha256(data).hexdigest() == digest



@pytest.mark.parametrize('member,content', [
    ('OEBPS/chapter-1.xhtml', '<p><a href="javascript:alert(1)">Original.</a></p>'),
    ('OEBPS/chapter-1.xhtml', '<p><a href="JaVa&#x9;script:alert(1)">Original.</a></p>'),
    ('OEBPS/chapter-1.xhtml', '<p><a href="data:text/html,active">Original.</a></p>'),
    ('OEBPS/chapter-1.xhtml', '<meta http-equiv=" Refresh " content="0;url=https://example.org"/>'),
    ('OEBPS/nav.xhtml', '<meta http-equiv="refresh" content="0;url=https://example.org"/>'),
    ('OEBPS/chapter-1.xhtml', '<p><a href="vbscript:active()">Original.</a></p>'),
])
def test_actual_package_active_uri_or_refresh_is_refused_before_transfer(member, content):
    raw = epub_bytes(chapters=1, first_body='<p>Original.</p>')
    with ZipFile(BytesIO(raw)) as archive:
        original = archive.read(member).decode('utf-8')
    if content.startswith('<meta'):
        changed = original.replace('</head>', content + '</head>')
    elif member.endswith('nav.xhtml'):
        changed = original.replace('</nav>', content + '</nav>')
    else:
        changed = original.replace('<p>Original.</p>', content)
    raw = replace_members(raw, {member: changed.encode('utf-8')})
    # Generic import intentionally permits these addresses as inert source.
    # The formatting profile must close the narrower republishing boundary.
    assert extract_epub(raw)['records'][0]['raw_text'] == 'Original.'
    with pytest.raises(ValueError, match='URI|refresh'):
        worker.describe(raw)


@pytest.mark.parametrize('href', ['#target', 'chapter-2.xhtml#heading', 'https://example.org/source?q=1'])
def test_ordinary_package_and_https_links_remain_exact_without_fetching(href, monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('Formatting must not follow a preserved link.')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    raw = epub_bytes(first_body='<p id="target"><a href="' + href + '">Original.</a></p>')
    description = worker.describe(raw)
    replacements = [{'ordinal': unit['ordinal'], 'source_sha256': unit['sha256'],
                     'text': 'Translated block.'}
                    for row in description['records'] for unit in row['units']]
    output, _ = worker.render(raw, replacements, 'uk')
    with ZipFile(BytesIO(output)) as archive:
        document = ET.fromstring(archive.read('OEBPS/chapter-1.xhtml'))
    assert document.find('.//' + XHTML + 'a').get('href') == href


def test_preserved_image_only_figure_does_not_create_a_translation_request():
    raw = epub_bytes(chapters=1, first_body='<p>Original.</p><figure><img src="image.png" alt="Source alt"/></figure>')
    mapping = worker.describe(raw)
    assert mapping['selected'] == 1 and mapping['records'][0]['text'] == 'Original.'


def test_translated_markup_remains_literal_text_not_active_xhtml(bound, tmp_path, monkeypatch):
    selected = captured(bound)
    identity = prepared(bound, selected)
    response(bound, monkeypatch, '<script>literal & text</script>')
    assert bound.service.resume(identity)['status'] == 'complete'
    output = tmp_path / 'Literal.epub'
    layout.export_layout(bound.service, identity, output)
    with ZipFile(output) as archive:
        chapter = ET.fromstring(archive.read('OEBPS/chapter-1.xhtml'))
        assert not chapter.findall('.//' + XHTML + 'script')
    assert '<script>literal & text</script>' in extract_epub(output.read_bytes())['records'][0]['raw_text']


def test_rendered_whitespace_identity_does_not_rewrite_accepted_provider_receipts(bound, tmp_path, monkeypatch):
    selected = captured(bound)
    identity = prepared(bound, selected)
    response(bound, monkeypatch, '  Translated\n  normalized\ttext  ')
    assert bound.service.resume(identity)['status'] == 'complete'
    before = deepcopy(bound.service.get(identity)['receipts'])
    receipt = layout.export_layout(bound.service, identity, tmp_path / 'Normalized.epub')
    assert any(row['translation_sha256'] != row['rendered_text_sha256'] for row in receipt['blocks'])
    assert receipt['blocks'][0]['rendered_text_sha256'] == sha256(b'Translated normalized text').hexdigest()
    assert bound.service.get(identity)['receipts'] == before


def test_current_epub_mapping_is_revalidated_before_approving_requests(bound):
    selected = captured(bound)
    mapping = selected.mapping
    mapping['records'][0]['units'][0]['locator'][-1] += 1
    forged = replace(selected, mapping_json=canonical(mapping))
    calls = list(bound.calls)
    with pytest.raises(ValueError, match='mapping|layout changed'):
        layout.review_layout(bound.service, bound.profile, forged, 'en', 'uk', '1')
    assert not bound.service.jobs.identities() and bound.calls == calls


def test_changed_request_snapshot_is_refused_by_the_actual_epub_worker(bound, monkeypatch):
    actual, reached = layout.run_process, []
    def mutate(args, **kwargs):
        path = Path(args[-4])
        raw = path.read_bytes()
        value = read_json(path)
        value['source_sha256'] = ('0' if value['source_sha256'][0] != '0' else '1') + value['source_sha256'][1:]
        replacement = encode(value)
        assert len(replacement) == len(raw)
        path.write_bytes(replacement)
        reached.append(True)
        return actual(args, **kwargs)
    monkeypatch.setattr(layout, 'run_process', mutate)
    with pytest.raises(ValueError, match='request changed'):
        captured(bound)
    assert reached == [True] and not bound.service.jobs.identities()


def test_paused_epub_reuses_only_accepted_blocks_without_duplicate_calls(bound):
    selected = captured(bound)
    identity = prepared(bound, selected)
    calls, stopped = len(bound.calls), []
    result = bound.service.resume(identity, cancelled=lambda: bool(stopped),
        progress=lambda *args: stopped.append(True))
    assert result['status'] == 'paused' and len(bound.calls) - calls == 1
    accepted = deepcopy(bound.service.get(identity)['receipts'][0])
    assert bound.service.resume(identity)['status'] == 'complete'
    assert len(bound.calls) - calls == 6
    assert bound.service.get(identity)['receipts'][0] == accepted


def test_many_chapters_keep_one_existing_job_and_bounded_source_positions(bound):
    path = bound.source.with_name('Seventy chapters.epub')
    path.write_bytes(epub_bytes(chapters=70))
    result = bound.app._import_file(bound.workspace, path, title='Seventy source chapters',
        work_id='many-work', edition_id='many-edition')
    assert result['state'] == 'complete'
    calls = list(bound.calls)
    selected = layout.capture_layout(bound.app, bound.workspace, 'many-edition', kind='epub')
    identity = prepared(bound, selected)
    assert bound.service.jobs.identities() == (identity,)
    assert len(bound.service.get(identity)['source_binding']['sources'][0]['anchor']['positions']) == 70
    assert selected.summary()['text_blocks'] == 210 and bound.calls == calls


def test_committed_epub_receipt_survives_a_stop_after_the_atomic_publisher_return(bound, tmp_path, monkeypatch):
    _, identity = complete(bound)
    stopped, actual = [], layout.write_binary_export
    def published(*args, **kwargs):
        receipt = actual(*args, **kwargs)
        stopped.append(True)
        return receipt
    monkeypatch.setattr(layout, 'write_binary_export', published)
    output = tmp_path / 'Committed.epub'
    receipt = layout.export_layout(bound.service, identity, output, cancelled=lambda: bool(stopped))
    assert stopped == [True] and receipt['status'] == 'complete'
    assert receipt['output_sha256'] == sha256(output.read_bytes()).hexdigest()
