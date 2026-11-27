"""Actual retained EPUB source, XHTML mapping, jobs and protected publication.

The provider is the existing synthetic bounded transport. EPUBCheck and native
wx acceptance are separate required scripts; neither is inferred from pytest.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED
import zlib

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import translation_epub_layout as layout
from carr_library.assistant import translation_epub_layout_worker as worker
from carr_library.assistant import translation_layout as shared
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_documents import run_documents
from carr_library.assistant.translation_partition import partition_ranges
from carr_library.durable_state import encode, read_json
from carr_library.extract.epub_ordered import extract_epub, XHTML, OPF, DC
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations.broker import ResourceBroker
from carr_library.research.export_file import capture_export_destination

_spec = importlib.util.spec_from_file_location('epub_layout_transport',
    Path(__file__).with_name('test_translation_local.py'))
_transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_transport)
ready = _transport.ready


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
        payloads['OEBPS/' + href] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">'
            '<head><title>Untranslated original chapter ' + str(index) + '</title>'
            '<link rel="stylesheet" type="text/css" href="styles.css"/></head><body>'
            + body + '</body></html>').encode('utf-8')
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
def bound(tmp_path, ready):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit EPUB source')
    source = tmp_path / 'Selected original Ω.epub'
    original = epub_bytes()
    source.write_bytes(original)
    imported = app._import_file(workspace, source, title='Retained EPUB Ω', authors=('Synthetic source',),
        work_id='epub-layout-work', edition_id='epub-layout-edition')
    assert imported['state'] == 'complete' and imported['verified'] is False
    directory = app.root / 'state/translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    ProfileStore(app.root / 'state/providers').save(ready.profile)
    service.refresh_languages(ready.profile, authorization=_transport.discovery(ready.profile))
    return SimpleNamespace(app=app, workspace=workspace, edition='epub-layout-edition',
        source=source, original=original, service=service, profile=ready.profile, state=ready.state)


def prepared(case, captured):
    reviewed = layout.review_layout(case.service, case.profile, captured, 'en', 'uk', '0')
    saved = layout.prepare_layout(case.service, case.profile, captured, 'en', 'uk', '0',
        approved_review=reviewed['review_sha256'])
    assert len(saved['jobs']) == 1
    return saved['jobs']


def complete(case):
    captured = layout.capture_layout(case.app, case.workspace, case.edition)
    identities = prepared(case, captured)
    assert run_documents(case.service, identities)['status'] == 'complete'
    return captured, identities


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


def test_actual_capture_and_review_retain_whole_chapter_authority_without_provider_calls(bound, monkeypatch):
    calls, invocations = list(bound.state['calls']), []
    actual = shared.run_process
    def observe(args, **kwargs):
        invocations.append(args)
        assert Path(args[-2]).resolve() == Path(worker.__file__).resolve()
        assert args[1:5] == ['-I', '-B', '-X', 'utf8']
        return actual(args, **kwargs)
    monkeypatch.setattr(shared, 'run_process', observe)
    captured = layout.capture_layout(bound.app, bound.workspace, bound.edition)
    identities = prepared(bound, captured)
    assert len(invocations) >= 3 and bound.state['calls'] == calls
    assert len(captured.selections) == 1 and captured.source_partition['profile'] == worker.PROFILE
    job = bound.service.get(identities[0])
    assert len(job['source_binding']['anchor']['positions']) == 2
    assert job['source_binding']['anchor']['quotation_authority'] is False
    assert sum(bool(chunk.strip()) for chunk in job['chunks']) == 6
    assert ''.join(job['chunks']) == captured.selections[0].document.source_text
    assert layout.order_layout_jobs(bound.service, captured, identities) == tuple(identities)


def test_actual_translated_epub_keeps_dom_links_styles_media_and_original_source(bound, tmp_path):
    captured, identities = complete(bound)
    output = tmp_path / 'New translated Ω.epub'
    receipt = layout.export_layout(bound.service, captured, identities, output)
    assert receipt['status'] == 'complete' and receipt['format'] == 'epub'
    assert receipt['quotation_authority'] is False and len(receipt['jobs']) == 6
    assert receipt['output_sha256'] == sha256(output.read_bytes()).hexdigest()
    assert len({row['id'] for row in receipt['jobs']}) == 1
    with ZipFile(BytesIO(bound.original)) as before, ZipFile(output) as after:
        assert before.namelist() == after.namelist()
        changed = set(receipt['changed_members'])
        assert changed == {'OEBPS/content.opf', 'OEBPS/chapter-1.xhtml', 'OEBPS/chapter-2.xhtml'}
        for name in before.namelist():
            if name not in changed:
                assert before.read(name) == after.read(name)
        chapter = ET.fromstring(after.read('OEBPS/chapter-1.xhtml'))
        heading = chapter.find('.//' + XHTML + 'h1')
        assert heading.get('id') == 'heading' and heading.get('class') == 'chapter'
        assert heading.find(XHTML + 'a').get('href') == '#table'
        assert heading.get('lang') == 'uk'
        assert heading.find('.//' + XHTML + 'strong').text.startswith('DERIVED: Selected original Ω')
        assert heading.find('.//' + XHTML + 'em') is not None
        assert chapter.find('.//' + XHTML + 'table').get('class') == 'retained-table'
        assert all(''.join(cell.itertext()).startswith('DERIVED: ')
                   for cell in chapter.findall('.//' + XHTML + 'td'))
        assert chapter.find('.//' + XHTML + 'img').get('alt') == 'Original image alternative'
        package = ET.fromstring(after.read('OEBPS/content.opf'))
        assert [row.text for row in package.findall('.//' + DC + 'language')] == ['uk', 'en']
    assert bound.source.read_bytes() == bound.original
    assert bound.app.assets.resolve(captured.asset_sha256).read_bytes() == bound.original
    assert ResourceBroker.for_app(bound.app.root).snapshot()['leases'] == []


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


def test_literal_translated_markup_is_escaped_and_never_becomes_an_xhtml_element(bound, tmp_path):
    captured = layout.capture_layout(bound.app, bound.workspace, bound.edition)
    identities = prepared(bound, captured)
    bound.state['raw'] = json.dumps({'translatedText': '<script>literal & text</script>'}).encode()
    assert run_documents(bound.service, identities)['status'] == 'complete'
    output = tmp_path / 'Literal EPUB.epub'
    layout.export_layout(bound.service, captured, identities, output)
    with ZipFile(output) as archive:
        chapter = ET.fromstring(archive.read('OEBPS/chapter-1.xhtml'))
        assert not chapter.findall('.//' + XHTML + 'script')
    assert '<script>literal & text</script>' in extract_epub(output.read_bytes())['records'][0]['raw_text']


def test_rendered_whitespace_hash_is_explicit_and_accepted_provider_receipt_stays_unchanged(bound, tmp_path):
    captured = layout.capture_layout(bound.app, bound.workspace, bound.edition)
    identities = prepared(bound, captured)
    value = '  Translated\n  normalized\ttext  '
    bound.state['raw'] = json.dumps({'translatedText': value}).encode()
    assert run_documents(bound.service, identities)['status'] == 'complete'
    before = deepcopy(bound.service.get(identities[0])['receipts'])
    result = layout.export_layout(bound.service, captured, identities, tmp_path / 'Normalized EPUB.epub')
    assert any(row['translation_sha256'] != row['xhtml_text_sha256'] for row in result['jobs'])
    assert bound.service.get(identities[0])['receipts'] == before
    assert result['jobs'][0]['xhtml_text_sha256'] == sha256(b'Translated normalized text').hexdigest()


def test_unreceipted_same_byte_competitor_remains_a_conflict(bound, tmp_path, monkeypatch):
    captured, identities = complete(bound)
    output = tmp_path / 'Competing EPUB.epub'
    actual, reached = layout.write_binary_export, []
    def compete(path, data, **kwargs):
        Path(path).write_bytes(data)
        reached.append(sha256(data).hexdigest())
        return actual(path, data, **kwargs)
    monkeypatch.setattr(layout, 'write_binary_export', compete)
    with pytest.raises(FileExistsError):
        layout.export_layout(bound.service, captured, identities, output)
    assert reached == [sha256(output.read_bytes()).hexdigest()]
    assert bound.source.read_bytes() == bound.original


def test_forged_dom_mapping_does_not_reuse_other_approved_receipts(bound, tmp_path):
    captured, identities = complete(bound)
    mapping = captured.mapping
    mapping['records'][0]['units'][0]['locator'][-1] += 1
    forged = replace(captured, mapping_json=canonical(mapping))
    calls = list(bound.state['calls'])
    with pytest.raises(ValueError, match='source partition'):
        layout.export_layout(bound.service, forged, identities, tmp_path / 'Forged EPUB.epub')
    assert not (tmp_path / 'Forged EPUB.epub').exists() and bound.state['calls'] == calls


def test_changed_actual_worker_snapshot_is_refused_before_parsing(bound, monkeypatch):
    actual, reached = shared.run_process, []
    def mutate(args, **kwargs):
        path = Path(args[-4])
        original = path.read_bytes()
        value = read_json(path)
        value['source_sha256'] = ('0' if value['source_sha256'][0] != '0' else '1') + value['source_sha256'][1:]
        changed = encode(value)
        assert len(changed) == len(original)
        path.write_bytes(changed)
        reached.append(True)
        return actual(args, **kwargs)
    monkeypatch.setattr(shared, 'run_process', mutate)
    with pytest.raises(ValueError, match='request changed'):
        layout.capture_layout(bound.app, bound.workspace, bound.edition)
    assert reached and not bound.service.jobs.identities()


def test_pause_keeps_accepted_blocks_and_resume_never_repeats_them(bound):
    captured = layout.capture_layout(bound.app, bound.workspace, bound.edition)
    identities = prepared(bound, captured)
    calls = len(bound.state['calls'])
    stopping = False
    def progress(done, total, phase):
        nonlocal stopping
        if done >= 1:
            stopping = True
    result = bound.service.resume(identities[0], cancelled=lambda: stopping, progress=progress)
    assert result['status'] == 'paused' and len(bound.state['calls']) - calls == 1
    first = deepcopy(bound.service.get(identities[0])['receipts'][0])
    assert run_documents(bound.service, identities)['status'] == 'complete'
    assert len(bound.state['calls']) - calls == 6
    assert bound.service.get(identities[0])['receipts'][0] == first


def test_actual_backup_restore_exports_completed_epub_without_credentials_or_old_paths(bound, tmp_path, monkeypatch):
    captured, identities = complete(bound)
    baseline = tmp_path / 'Baseline.epub'
    layout.export_layout(bound.service, captured, identities, baseline)
    expected = baseline.read_bytes()
    receipt = create_backup(bound.app, tmp_path / 'backup')
    bound.source.unlink()
    shutil.rmtree(bound.app.root)
    destination = restore_backup(tmp_path / 'backup', tmp_path / 'restored', receipt)
    app = LibraryApp(destination)
    ProfileStore(app.root / 'state/providers').remove(bound.profile.id)
    directory = app.root / 'state/translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    def forbidden(*args, **kwargs):
        pytest.fail('Completed EPUB export attempted a provider or credential call.')
    monkeypatch.setattr(service.gateway, 'request', forbidden)
    monkeypatch.setattr(service.gateway.vault, 'get', forbidden)
    fresh = layout.capture_layout(app, bound.workspace, bound.edition)
    with pytest.raises(ValueError, match='directory changed'):
        layout.export_layout(service, captured, identities, tmp_path / 'Old capture.epub')
    result = layout.export_layout(service, fresh, identities, tmp_path / 'Restored.epub')
    assert result['status'] == 'complete' and (tmp_path / 'Restored.epub').read_bytes() == expected


def test_seventy_source_chapters_keep_one_existing_job_without_raising_job_lock_limit(bound):
    source = bound.source.with_name('Seventy selected chapters.epub')
    source.write_bytes(epub_bytes(chapters=70))
    imported = bound.app._import_file(bound.workspace, source, title='Seventy chapters',
        work_id='many-chapter-work', edition_id='many-chapter-edition')
    assert imported['state'] == 'complete'
    calls = list(bound.state['calls'])
    captured = layout.capture_layout(bound.app, bound.workspace, 'many-chapter-edition')
    identities = prepared(bound, captured)
    assert len(identities) == 1 and len(captured.selections) == 1
    assert len(bound.service.get(identities[0])['source_binding']['anchor']['positions']) == 70
    assert captured.summary()['paragraphs'] == 210 and bound.state['calls'] == calls


def test_late_stop_after_actual_epub_publication_retains_completed_receipt(bound, tmp_path, monkeypatch):
    captured, identities = complete(bound)
    stopping = False
    actual = layout.write_binary_export
    def published(*args, **kwargs):
        nonlocal stopping
        result = actual(*args, **kwargs)
        stopping = True
        return result
    monkeypatch.setattr(layout, 'write_binary_export', published)
    result = layout.export_layout(bound.service, captured, identities, tmp_path / 'Completed.epub',
                                  cancelled=lambda: stopping)
    assert stopping and result['status'] == 'complete' and Path(result['path']).is_file()
