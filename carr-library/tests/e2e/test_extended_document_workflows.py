"""Real native-import, reader and recovery checks for additional document types.

Fixtures are small original synthetic documents. They deliberately vary archive
order from logical document order; expectations do not call the extractors.
These checks are not layout, quotation approval or screen-reader acceptance.
"""
from email.message import EmailMessage
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.worker import load_request
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.source_file import prepare_source_file, launch_source_file


FIRST = 'Librarymarker. Наукова бібліотека — štúdia.'
SECOND = 'Secondmarker. Ordered academic source.'
ODF = 'urn:oasis:names:tc:opendocument:xmlns:'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
SHEET = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
SLIDE = 'http://schemas.openxmlformats.org/presentationml/2006/main'
DRAWING = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def archive_bytes(entries, *, stored=()):
    result = BytesIO()
    with ZipFile(result, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data, compress_type=ZIP_STORED if name in stored else ZIP_DEFLATED)
    return result.getvalue()


def odf_bytes(extension):
    kind = {'.odt': 'text', '.ods': 'spreadsheet', '.odp': 'presentation'}[extension]
    mime = 'application/vnd.oasis.opendocument.' + kind
    if kind == 'text':
        body = f'<text:p>{FIRST}</text:p><text:p>{SECOND}</text:p>'
    elif kind == 'spreadsheet':
        body = f'''<table:table table:name="Research"><table:table-row>
          <table:table-cell office:value-type="string"><text:p>{FIRST}</text:p></table:table-cell>
          <table:table-cell office:value-type="string"><text:p>{SECOND}</text:p></table:table-cell>
        </table:table-row></table:table>'''
    else:
        body = f'''<draw:page draw:name="First slide"><draw:frame><draw:text-box>
          <text:p>{FIRST}</text:p></draw:text-box></draw:frame></draw:page>
          <draw:page draw:name="Second slide"><draw:frame><draw:text-box>
          <text:p>{SECOND}</text:p></draw:text-box></draw:frame></draw:page>'''
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
      <office:document-content xmlns:office="{ODF}office:1.0"
        xmlns:text="{ODF}text:1.0" xmlns:table="{ODF}table:1.0" xmlns:draw="{ODF}drawing:1.0"
        office:version="1.3"><office:body><office:{kind}>{body}</office:{kind}>
      </office:body></office:document-content>'''
    manifest = f'''<manifest:manifest xmlns:manifest="{ODF}manifest:1.0" manifest:version="1.3">
      <manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/>
      <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>
      </manifest:manifest>'''
    return archive_bytes((('mimetype', mime), ('content.xml', content),
                          ('META-INF/manifest.xml', manifest)), stored=('mimetype',))


def office_bytes(extension):
    if extension == '.pptx':
        root = 'ppt/presentation.xml'
        root_type = 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'
        part_type = 'application/vnd.openxmlformats-officedocument.presentationml.slide+xml'
        root_xml = f'''<p:presentation xmlns:p="{SLIDE}" xmlns:r="{REL}"><p:sldIdLst>
          <p:sldId id="256" r:id="secondFile"/><p:sldId id="257" r:id="firstFile"/>
          </p:sldIdLst></p:presentation>'''
        links = [('secondFile', 'slides/slide2.xml', 'slide'), ('firstFile', 'slides/slide1.xml', 'slide')]
        def part(text):
            return f'''<p:sld xmlns:p="{SLIDE}" xmlns:a="{DRAWING}"><p:cSld><p:spTree>
              <p:sp><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t></a:r></a:p>
              </p:txBody></p:sp></p:spTree></p:cSld></p:sld>'''
        parts = [('ppt/slides/slide1.xml', part(SECOND)), ('ppt/slides/slide2.xml', part(FIRST))]
        rel_path = 'ppt/_rels/presentation.xml.rels'
    else:
        root = 'xl/workbook.xml'
        root_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'
        part_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
        root_xml = f'''<workbook xmlns="{SHEET}" xmlns:r="{REL}"><sheets>
          <sheet name="First research sheet" sheetId="2" r:id="secondFile"/>
          <sheet name="Second research sheet" sheetId="1" r:id="firstFile"/>
          </sheets></workbook>'''
        links = [('secondFile', 'worksheets/sheet2.xml', 'worksheet'),
                 ('firstFile', 'worksheets/sheet1.xml', 'worksheet')]
        def part(text):
            return f'''<worksheet xmlns="{SHEET}"><sheetData><row r="1">
              <c r="A1" t="inlineStr"><is><t>{text}</t></is></c>
              </row></sheetData></worksheet>'''
        parts = [('xl/worksheets/sheet1.xml', part(SECOND)), ('xl/worksheets/sheet2.xml', part(FIRST))]
        rel_path = 'xl/_rels/workbook.xml.rels'
    types = f'''<Types xmlns="{CT}">
      <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
      <Default Extension="xml" ContentType="application/xml"/>
      <Override PartName="/{root}" ContentType="{root_type}"/>'''
    types += ''.join(f'<Override PartName="/{name}" ContentType="{part_type}"/>' for name, _ in parts)
    types += '</Types>'
    rels = f'<Relationships xmlns="{PKG}">' + ''.join(
        f'<Relationship Id="{ident}" Type="{REL}/{kind}" Target="{target}"/>'
        for ident, target, kind in links) + '</Relationships>'
    root_rels = f'''<Relationships xmlns="{PKG}"><Relationship Id="root"
      Type="{REL}/officeDocument" Target="{root}"/></Relationships>'''
    return archive_bytes([('[Content_Types].xml', types), ('_rels/.rels', root_rels),
                          *parts, (root, root_xml), (rel_path, rels)])


def source_bytes(extension):
    if extension in ('.html', '.htm', '.xhtml'):
        return (f'''<!doctype html><html><head><title>Embedded title is not catalog authority</title>
          <script>Neverindexscript</script></head><body><p>{FIRST}</p>
          <p>{SECOND}</p></body></html>''').encode('utf-8')
    if extension in ('.md', '.markdown'):
        return f'# Original heading\r\n\r\n{FIRST}\r\n\r\n**{SECOND}**\r\n'.encode('utf-8')
    if extension == '.eml':
        mail = EmailMessage()
        mail['Subject'] = 'Research correspondence'
        mail['From'] = 'author@example.invalid'
        mail['To'] = 'reader@example.invalid'
        mail.set_content(FIRST + '\n\n' + SECOND, charset='utf-8')
        mail.add_alternative('<html><body>Neverindexalternate</body></html>', subtype='html')
        mail.add_attachment(b'Neverindexattachment', maintype='application',
                            subtype='octet-stream', filename='attachment.bin')
        return mail.as_bytes()
    if extension == '.fb2':
        return (f'''<?xml version="1.0" encoding="UTF-8"?>
          <FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description>
          <title-info><book-title>Embedded title</book-title><lang>uk</lang></title-info>
          </description><body><section><p>{FIRST}</p><p>{SECOND}</p></section>
          </body></FictionBook>''').encode('utf-8')
    if extension == '.fb3':
        body = f'''<fb3-body xmlns="http://www.fictionbook.org/FictionBook3/body">
          <section><p>{FIRST}</p><p>{SECOND}</p></section></fb3-body>'''
        relationships = f'''<Relationships xmlns="{PKG}"><Relationship Id="body"
          Type="{PKG}/fb3-body" Target="fb3/body.xml"/></Relationships>'''
        content_types = f'''<Types xmlns="{CT}">
          <Default Extension="xml" ContentType="application/xml"/>
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Override PartName="/fb3/body.xml" ContentType="application/fb3-body+xml"/></Types>'''
        return archive_bytes((('fb3/body.xml', body), ('_rels/.rels', relationships),
                              ('[Content_Types].xml', content_types)))
    if extension in ('.odt', '.odp', '.ods'):
        return odf_bytes(extension)
    if extension in ('.pptx', '.xlsx'):
        return office_bytes(extension)
    raise AssertionError(extension)


FORMATS = ('.html', '.md', '.eml', '.fb2', '.fb3', '.odt', '.ods', '.odp', '.pptx', '.xlsx')


@pytest.mark.parametrize('extension', FORMATS)
def test_extended_source_import_reader_search_and_quote_boundary(tmp_path, extension):
    source = tmp_path / ('Наукове джерело' + extension)
    original = source_bytes(extension)
    source.write_bytes(original)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Additional academic formats')
    collection = app.create_collection(workspace, 'Selected originals')
    topic = app.create_topic(workspace, 'Evidence')
    result = app.import_file(workspace, source, title='Chosen catalog title',
        authors=('Олена Коваль', 'Ján Novák'), language='uk', edition='Study edition',
        collections=(collection,))
    assert result['state'] == 'complete', result
    assert result['isolated'] is True and result['verified'] is False
    book, = app.browse(workspace, collection=collection)
    assert (book.title, book.authors, book.language, book.edition) == (
        'Chosen catalog title', ('Олена Коваль', 'Ján Novák'), 'uk', 'Study edition')
    assert book.asset == sha256(original).hexdigest()
    request = load_request(app.root, result['request_id'])
    assert request['asset_sha256'] == book.asset
    assert request['result']['warnings'] == result['warnings']
    if extension in ('.html', '.eml'):
        from carr_library.ui.import_center import row_details
        details = row_details(dict(file=source.name, state='complete', result=result))
        assert 'Extraction notes:' in details
        assert ('HTML scripts and active or nontext content were omitted.' in details
                if extension == '.html' else 'Email attachments were omitted and were not opened.' in details)
    with app.read(workspace) as db:
        config = json.loads(db.execute('SELECT config FROM revision WHERE id=?', (result['id'],)).fetchone()[0])
    assert config['warnings'] == result['warnings']
    assert config['provenance']['asset_sha256'] == book.asset
    assert len(config['logical_locators']) == result['completed']
    assert app.assets.resolve(book.asset).read_bytes() == original
    assert app.assets.resolve(book.asset).suffix == extension
    desk = ReadingDesk(app, workspace, book.edition_id)
    initial = desk.page(1)
    pages = [initial] + [desk.page(i) for i in range(2, initial.total + 1)]
    combined = '\n'.join(page.raw_text for page in pages)
    assert FIRST in combined and SECOND in combined
    assert combined.index(FIRST) < combined.index(SECOND)
    assert all(page.locator.kind == 'logical_segment' and not page.verified for page in pages)
    assert all(page.locator.printed is None for page in pages)
    assert all(page.location.asset_sha256 == sha256(original).hexdigest() for page in pages)
    hit, = app.search(workspace, 'Librarymarker', collection=collection)
    assert hit.verified is False and FIRST in app.preview(workspace, hit.ref, hit.revision)
    assert app.search(workspace, 'Neverindexscript') == ()
    assert app.search(workspace, 'Neverindexalternate') == ()
    assert app.search(workspace, 'Neverindexattachment') == ()
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='unverified'):
        app.save_hit(workspace, topic, hit.ref, hit.revision)
    assert app.evidence(workspace, topic) == ()
    other = app.create_workspace('Separate scope')
    assert app.search(other, 'Librarymarker') == ()
    assert source.read_bytes() == original


@pytest.mark.parametrize('encoding', ('utf-16', 'utf-32', 'cp1251'))
def test_html_declared_encoding_is_preserved_through_intake_and_worker(tmp_path, encoding):
    marker = 'Незмінний оригінал українською.'
    declaration = '<meta charset="windows-1251">' if encoding == 'cp1251' else ''
    original = f'<!doctype html><html><head>{declaration}</head><body><p>{marker}</p></body></html>'.encode(encoding)
    source = tmp_path / 'language.html'
    source.write_bytes(original)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Encoded sources')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and not result['verified'], result
    book, = app.browse(workspace)
    assert ReadingDesk(app, workspace, book.edition_id).page(1).raw_text == marker
    assert app.assets.resolve(book.asset).read_bytes() == original


def test_long_xml_paragraph_import_is_readable_without_character_loss(tmp_path):
    text = 'Original ' + ('a' * 2_000_000) + ' кінцевий текст'
    original = ('<?xml version="1.0" encoding="UTF-8"?><FictionBook '
        'xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><section><p>' +
        text + '</p></section></body></FictionBook>').encode('utf-8')
    source = tmp_path / 'long.fb2'
    source.write_bytes(original)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Long source paragraph')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['completed'] == 2, result
    book, = app.browse(workspace)
    desk = ReadingDesk(app, workspace, book.edition_id)
    first, second = desk.page(1), desk.page(2)
    assert first.raw_text + second.raw_text == text
    assert not first.verified and not second.verified
    assert app.assets.resolve(book.asset).read_bytes() == original
    with app.read(workspace) as db:
        config = json.loads(db.execute('SELECT config FROM revision WHERE id=?', (result['id'],)).fetchone()[0])
    locators = config['logical_locators']
    assert locators[0]['source_locator'] == locators[1]['source_locator']
    assert locators[0]['characters'] == [0, len(first.raw_text)]
    assert locators[1]['characters'] == [len(first.raw_text), len(text)]


@pytest.mark.parametrize('extension', ('.htm', '.xhtml', '.markdown'))
def test_source_extension_alias_retains_canonical_format_after_original_is_removed(tmp_path, extension):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Aliases')
    source = tmp_path / ('source' + extension)
    original = source_bytes(extension)
    source.write_bytes(original)
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete', result
    source.unlink()
    hit, = app.search(workspace, 'Librarymarker')
    canonical = '.html' if extension in ('.htm', '.xhtml') else '.md'
    prepared = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert prepared.path.suffix == canonical and prepared.path.read_bytes() == original
    assert FIRST in app.preview(workspace, hit.ref, hit.revision)


def test_mixed_document_batch_pause_reopen_and_backup_preserve_exact_sources(tmp_path):
    folder = tmp_path / 'selected'
    folder.mkdir()
    originals = {}
    for extension in ('.html', '.odt', '.pptx', '.xlsx'):
        source = folder / ('source' + extension)
        source.write_bytes(source_bytes(extension))
        originals[sha256(source.read_bytes()).hexdigest()] = source.read_bytes()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Batch formats')
    collection = app.create_collection(workspace, 'Collected sources')
    planned = app.plan_import_batch(workspace, [folder], collections=(collection,))
    assert planned['counts']['queued'] == 4 and planned['counts'].get('rejected', 0) == 0
    pause = False
    def progressed(batch):
        nonlocal pause
        if batch['counts']['complete']:
            pause = True
    partial = app.run_import_batch(workspace, planned['batch_id'],
                                   cancelled=lambda: pause, progress=progressed)
    assert partial['state'] == 'paused' and partial['counts']['complete'] == 1
    completed = next(row for row in partial['rows'] if row['state'] == 'complete')
    completed_identity = tuple(completed[k] for k in ('row_id', 'work_id', 'edition_id', 'request_id'))
    reopened = LibraryApp(app.root)
    result = reopened.run_import_batch(workspace, planned['batch_id'])
    assert result['state'] == 'complete' and result['counts']['complete'] == 4
    same = next(row for row in result['rows'] if row['row_id'] == completed['row_id'])
    assert tuple(same[k] for k in ('row_id', 'work_id', 'edition_id', 'request_id')) == completed_identity
    assert all(row['result']['verified'] is False for row in result['rows'])
    assert len(reopened.browse(workspace, collection=collection)) == 4
    backup = tmp_path / 'backup'
    checksum = create_backup(reopened, backup)
    for path in folder.iterdir():
        path.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    assert len(restored.browse(workspace, collection=collection)) == 4
    hits = restored.search(workspace, 'Librarymarker', collection=collection)
    assert len(hits) == 4 and all(not hit.verified for hit in hits)
    for book in restored.browse(workspace):
        assert restored.assets.resolve(book.asset).read_bytes() == originals[book.asset]
        assert FIRST in '\n'.join(ReadingDesk(restored, workspace, book.edition_id).page(i).raw_text
            for i in range(1, ReadingDesk(restored, workspace, book.edition_id).page(1).total + 1))


def test_exact_reimport_adds_membership_and_conflicting_metadata_does_not_publish(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Replay')
    source = tmp_path / 'source.html'
    source.write_bytes(source_bytes('.html'))
    first = app.import_file(workspace, source, title='Immutable catalog title',
                            work_id='synthetic-work', edition_id='synthetic-edition')
    assert first['state'] == 'complete', first
    collection = app.create_collection(workspace, 'New membership')
    replay = app.import_file(workspace, source, title='Immutable catalog title',
        work_id='synthetic-work', edition_id='synthetic-edition', collections=(collection,))
    assert replay['state'] == 'complete' and replay['id'] == first['id'], replay
    assert len(app.browse(workspace, collection=collection)) == 1
    invalid = app.import_file(workspace, source, title='Conflicting replacement title',
        work_id='synthetic-work', edition_id='synthetic-edition')
    assert invalid['state'] == 'failed', invalid
    book, = app.browse(workspace)
    assert book.title == 'Immutable catalog title'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    assert len(app.search(workspace, 'Librarymarker')) == 1


def test_changed_planned_document_is_rejected_before_retention(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Planned source')
    source = tmp_path / 'source.odt'
    source.write_bytes(source_bytes('.odt'))
    plan = app.plan_import_batch(workspace, [source])
    assert plan['counts']['queued'] == 1
    source.write_bytes(odf_bytes('.odp'))
    result = app.run_import_batch(workspace, plan['batch_id'])
    assert result['rows'][0]['state'] == 'failed', result
    assert app.browse(workspace) == () and tuple(app.assets_dir.iterdir()) == ()


def test_damaged_retained_original_invalidates_existing_preview_and_reader(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source integrity')
    source = tmp_path / 'source.md'
    source.write_bytes(source_bytes('.md'))
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete', result
    book, = app.browse(workspace)
    hit, = app.search(workspace, 'Librarymarker')
    desk = ReadingDesk(app, workspace, book.edition_id)
    original = app.assets.resolve(book.asset)
    original.write_bytes(b'Altered retained bytes, not the indexed original.')
    with pytest.raises(ValueError):
        app.preview(workspace, hit.ref, hit.revision)
    with pytest.raises(ValueError):
        desk.page(1)
    with pytest.raises(ValueError):
        prepare_source_file(app, workspace, hit.ref, hit.revision)


def test_unsafe_archive_does_not_publish_searchable_text(tmp_path):
    source = tmp_path / 'unsafe.odt'
    source.write_bytes(archive_bytes((('mimetype', 'application/vnd.oasis.opendocument.text'),
        ('../content.xml', FIRST), ('content.xml', '<bad/>')), stored=('mimetype',)))
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Rejected archive')
    result = app.import_file(workspace, source)
    assert result['state'] == 'failed', result
    assert app.browse(workspace) == () and app.search(workspace, 'Librarymarker') == ()


def test_cancelled_extended_import_can_resume_same_identity(tmp_path):
    source = tmp_path / 'cancelled.eml'
    source.write_bytes(source_bytes('.eml'))
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Cancellable import')
    cancelled = app.import_file(workspace, source, cancelled=lambda: True)
    assert cancelled['state'] == 'cancelled' and cancelled['isolated'] is True
    assert app.browse(workspace) == () and tuple(app.assets_dir.iterdir()) == ()
    request = load_request(app.root, cancelled['request_id'])
    resumed = LibraryApp(app.root).resume_import(workspace, cancelled['request_id'])
    assert resumed['state'] == 'complete' and resumed['request_id'] == cancelled['request_id']
    assert resumed['edition'] == request['metadata']['edition_id']
    book, = app.browse(workspace)
    assert book.edition_id == resumed['edition']


@pytest.mark.parametrize('extension', FORMATS)
def test_pending_extended_original_survives_complete_backup_and_resume(tmp_path, extension):
    """Canonical pending recovery retains each format after external input loss."""
    from carr_library.catalog.intake import snapshot
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request
    from uuid import uuid4

    source = tmp_path / ('pending' + extension)
    original = source_bytes(extension)
    source.write_bytes(original)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending additional document')
    metadata = dict(title='Pending original', authors=[], language=None, edition=None,
        volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(source.parent, source.name).manifest()
    request = prepare_request(app.root, workspace, source, metadata, Config(),
        expected_manifest=planned)
    with pytest.raises(InterruptedError):
        app._import_file(workspace, source, **metadata, _request_id=request['request_id'],
            _expected_manifest=planned, cancelled=lambda: True)
    asset = sha256(original).hexdigest()
    assert app.assets.resolve(asset).read_bytes() == original
    assert source.read_bytes() == original and app.browse(workspace) == ()
    source.unlink()

    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert manifest['version'] == 4
    retained, = manifest['pending']['assets']
    assert retained['sha256'] == asset and retained['mime'] == planned['mime']
    assert retained['filename'] == asset + extension
    assert (backup / 'pending' / 'originals' / retained['filename']).read_bytes() == original

    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    assert restored.assets.resolve(asset).read_bytes() == original
    copied_request = load_request(restored.root, request['request_id'])
    assert copied_request['data_dir'] == str(restored.root)
    assert copied_request['source'] == str(restored.assets.resolve(asset))
    assert copied_request['metadata'] == metadata
    assert copied_request['expected_manifest'] == planned
    result = restored.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete', result
    assert result['edition'] == metadata['edition_id'] and result['verified'] is False
    assert result['request_id'] == request['request_id']
    assert restored.assets.resolve(asset).read_bytes() == original
    assert restored.search(workspace, 'Librarymarker')


@pytest.mark.parametrize('extension', ('.html', '.eml', '.pptx', '.xlsx', '.odt'))
def test_untrusted_active_source_formats_never_launch_default_viewer(tmp_path, extension):
    source = tmp_path / ('active-capable' + extension)
    source.write_bytes(source_bytes(extension))
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Safe source reading')
    imported = app.import_file(workspace, source)
    assert imported['state'] == 'complete', imported
    hit, = app.search(workspace, 'Librarymarker')
    prepared = prepare_source_file(app, workspace, hit.ref, hit.revision)
    calls = []
    with pytest.raises(ValueError):
        launch_source_file(app, prepared, lambda path: calls.append(path) or True)
    assert calls == []
    assert FIRST in app.preview(workspace, hit.ref, hit.revision)


@pytest.mark.parametrize('extension', ('.fb3', '.pptx', '.xlsx'))
def test_malformed_primary_relationship_or_text_leaf_does_not_publish(tmp_path, extension):
    original = source_bytes(extension)
    with ZipFile(BytesIO(original)) as archive:
        entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
    if extension == '.fb3':
        modified = [(name, value.replace(b'Target="fb3/body.xml"',
                    b'Target="https://example.invalid/untrusted-body.xml" TargetMode="External"')
                    if name == '_rels/.rels' else value) for name, value in entries]
    else:
        part = 'ppt/slides/slide2.xml' if extension == '.pptx' else 'xl/worksheets/sheet2.xml'
        modified = [(name, value.replace(FIRST.encode('utf-8'),
                    b'Visible <unexpected>Omitted semantics</unexpected> tail')
                    if name == part else value) for name, value in entries]
    malformed = archive_bytes(modified)
    source = tmp_path / ('malformed' + extension)
    source.write_bytes(malformed)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Fail-closed document identity')
    result = app.import_file(workspace, source)
    assert result['state'] == 'failed', result
    assert result['isolated'] is True
    assert app.browse(workspace) == ()
    assert app.search(workspace, 'Visible') == ()
    assert app.search(workspace, 'Librarymarker') == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    assert source.read_bytes() == malformed


@pytest.mark.parametrize('arguments', (
    ('--self-test',), ('--backup', 'unused-backup'), ('studio', 'voices'),
    ('--lookup-doi', '10.5555/example', '--allow-online-doi', '--workspace', 'selected'),
))
def test_format_listing_rejects_another_operation_before_opening_any_runtime(arguments, monkeypatch):
    from carr_library.__main__ import main
    def forbidden(*args, **kwargs):
        raise AssertionError('Ambiguous command must not initialize an application or native runtime.')
    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    monkeypatch.setattr('carr_library.native_runtime.activate_bundled_runtime', forbidden)
    with pytest.raises(SystemExit) as error:
        main(['--document-formats', *arguments])
    assert error.value.code == 2


def test_format_listing_does_not_open_a_library_or_native_runtime(monkeypatch, capsys):
    from carr_library.__main__ import main
    def forbidden(*args, **kwargs):
        raise AssertionError('Format listing must not open a library or native runtime.')
    monkeypatch.setattr('carr_library.application.LibraryApp', forbidden)
    monkeypatch.setattr('carr_library.native_runtime.activate_bundled_runtime', forbidden)
    assert main(['--document-formats']) == 0
    text = capsys.readouterr().out
    assert 'HTML' in text and '.xlsx' in text
