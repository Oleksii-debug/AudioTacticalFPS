"""Original synthetic containers: real parsing, source fidelity and fail-closed bounds."""
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

import pytest

from carr_library.extract import document_formats_xml as module
from carr_library.extract.document_formats_xml import extract_xml_document


ODF = 'urn:oasis:names:tc:opendocument:xmlns:'
O = ODF + 'office:1.0'
T = ODF + 'text:1.0'
TB = ODF + 'table:1.0'
D = ODF + 'drawing:1.0'
PRES = ODF + 'presentation:1.0'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
FB2 = 'http://www.gribuser.ru/xml/fictionbook/2.0'
FB3 = 'http://www.fictionbook.org/FictionBook3/body'


def zipped(entries):
    target = BytesIO()
    with ZipFile(target, 'w') as archive:
        for name, value in entries:
            archive.writestr(name, value,
                compress_type=ZIP_STORED if name == 'mimetype' else ZIP_DEFLATED)
    return target.getvalue()


def odf(suffix, text=None, *, styles=None, manifest=None, extra=()):
    kind = {'.odt': 'text', '.odp': 'presentation', '.ods': 'spreadsheet'}[suffix]
    if text is None:
        if suffix == '.odt':
            text = '<text:h>Title</text:h><text:p>  Перший <text:span>текст</text:span><text:s text:c="3"/>kôň<text:tab/>last<text:line-break/>line </text:p>'
        elif suffix == '.odp':
            text = '<draw:page draw:name="One"><draw:frame><draw:text-box><text:p>First</text:p></draw:text-box></draw:frame></draw:page><draw:page draw:name="Two"><draw:frame><draw:text-box><text:p>Second</text:p></draw:text-box></draw:frame></draw:page>'
        else:
            text = '<table:table table:name="Sheet"><table:table-row><table:table-cell><text:p>First</text:p></table:table-cell><table:table-cell><text:p>Second</text:p></table:table-cell></table:table-row></table:table>'
    mime = 'application/vnd.oasis.opendocument.' + kind
    xml = f'<office:document-content xmlns:office="{O}" xmlns:text="{T}" xmlns:table="{TB}" xmlns:draw="{D}" xmlns:presentation="{PRES}"><office:body><office:{kind}>{text}</office:{kind}></office:body></office:document-content>'
    manifest = manifest or f'<manifest:manifest xmlns:manifest="{ODF}manifest:1.0"><manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/></manifest:manifest>'
    entries = [('mimetype', mime), ('content.xml', xml), ('META-INF/manifest.xml', manifest)]
    if styles is not None:
        entries.append(('styles.xml', styles))
    return zipped([*entries, *extra])


def relationships(items):
    return f'<Relationships xmlns="{REL}">' + ''.join(
        f'<Relationship Id="{ident}" Type="{kind}" Target="{target}"{more}/>'
        for ident, kind, target, more in items) + '</Relationships>'


def office(suffix, *, parts=None, order=None, links=None, extra=()):
    if suffix == '.pptx':
        root, folder, stem = 'ppt/presentation.xml', 'ppt', 'slide'
        parts = parts or [slide('Last'), slide('First')]
        order = order or [2, 1]
        xml = f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>' + ''.join(
            f'<p:sldId id="{i+255}" r:id="r{i}"/>' for i in order) + '</p:sldIdLst></p:presentation>'
        relfile = 'ppt/_rels/presentation.xml.rels'
        names = [f'ppt/slides/slide{i}.xml' for i in range(1, len(parts) + 1)]
        targets = [f'slides/slide{i}.xml' for i in range(1, len(parts) + 1)]
    else:
        root, folder, stem = 'xl/workbook.xml', 'xl', 'worksheet'
        parts = parts or [sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>Last</t></is></c></row>'),
                          sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>First</t></is></c></row>')]
        order = order or [2, 1]
        xml = f'<workbook xmlns="{X}" xmlns:r="{R}"><sheets>' + ''.join(
            f'<sheet sheetId="{i}" name="Book {i}" r:id="r{i}"/>' for i in order) + '</sheets></workbook>'
        relfile = 'xl/_rels/workbook.xml.rels'
        names = [f'xl/worksheets/sheet{i}.xml' for i in range(1, len(parts) + 1)]
        targets = [f'worksheets/sheet{i}.xml' for i in range(1, len(parts) + 1)]
    links = links if links is not None else [(f'r{i}', R + '/' + stem, target, '')
                                              for i, target in enumerate(targets, 1)]
    entries = [('[Content_Types].xml', f'<Types xmlns="{CT}"><Default Extension="xml" ContentType="application/xml"/></Types>'),
        ('_rels/.rels', relationships([('root', R + '/officeDocument', root, '')])),
        (root, xml), (relfile, relationships(links)), *zip(names, parts), *extra]
    return zipped(entries)


def slide(text, *, hidden=False, body=None):
    paragraph = body or f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
    return f'<p:sld xmlns:p="{P}" xmlns:a="{A}" show="{0 if hidden else 1}"><p:cSld><p:spTree><p:sp><p:txBody>{paragraph}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'


def sheet(rows, *, before='', after=''):
    return f'<worksheet xmlns="{X}">{before}<sheetData>{rows}</sheetData>{after}</worksheet>'


def fb2(body='<section><title><p>Title</p></title><p>Книга <emphasis>štúdia</emphasis> remains.</p></section>'):
    return f'<FictionBook xmlns="{FB2}"><description><title-info><book-title>Not body</book-title></title-info></description><body>{body}</body></FictionBook>'.encode()


def fb3(body='<section><title><p>Title</p></title><p>Text <em>Є</em>.</p></section>'):
    return zipped([('[Content_Types].xml', f'<Types xmlns="{CT}"><Override PartName="/fb3/body.xml" ContentType="application/fb3-body+xml"/></Types>'),
        ('_rels/.rels', relationships([('body', REL + '/fb3-body', 'fb3/body.xml', '')])),
        ('fb3/body.xml', f'<fb3-body xmlns="{FB3}">{body}</fb3-body>')])


def texts(result):
    return [record['raw_text'] for record in result['records']]


def test_inert_office_converter_containers_preserve_all_surrounding_reading_text():
    body = ('<office:forms/><text:p>Before</text:p><draw:custom-shape>'
            '<text:p>Shape text</text:p><draw:enhanced-geometry draw:type="rectangle">'
            '<draw:equation draw:name="f0" draw:formula="0"/>'
            '</draw:enhanced-geometry></draw:custom-shape><text:p>After</text:p>')
    result = extract_xml_document(odf('.odt', body), '.odt')
    assert texts(result) == ['Before', 'Shape text', 'After']
    assert 'vector_geometry_not_read' in result['warnings']


@pytest.mark.parametrize('fragment', [
    '<office:forms><text:p>Must not disappear</text:p></office:forms>',
    '<office:forms foreign="true"/>',
    '<office:forms>Must not disappear</office:forms>',
    '<draw:enhanced-geometry><text:p>Must not disappear</text:p></draw:enhanced-geometry>',
    '<draw:enhanced-geometry><draw:equation>Must not disappear</draw:equation></draw:enhanced-geometry>',
    '<draw:enhanced-geometry><draw:handle><text:p>Must not disappear</text:p></draw:handle></draw:enhanced-geometry>',
], ids=['populated-form', 'foreign-form-setting', 'form-text', 'geometry-text-node', 'equation-text', 'nested-handle'])
def test_converter_container_support_never_silently_drops_unknown_text_or_controls(fragment):
    with pytest.raises(ValueError, match='form|shape'):
        extract_xml_document(odf('.odt', '<text:p>Before</text:p>' + fragment), '.odt')


@pytest.mark.parametrize('suffix,make,expected', [
    ('.odt', lambda: odf('.odt'), ['Title', '  Перший текст   kôň\tlast\nline ']),
    ('.odp', lambda: odf('.odp'), ['First', 'Second']),
    ('.ods', lambda: odf('.ods'), ['First', 'Second']),
    ('.pptx', lambda: office('.pptx'), ['First', 'Last']),
    ('.xlsx', lambda: office('.xlsx'), ['First', 'Last']),
    ('.fb2', fb2, ['Title', 'Книга štúdia remains.']),
    ('.fb3', fb3, ['Title', 'Text Є.']),
])
def test_real_supported_document_order_source_hash_and_unverified_locators(suffix, make, expected):
    source = make()
    fingerprint = sha256(source).hexdigest()
    result = extract_xml_document(source, suffix)
    assert texts(result) == expected
    assert result['status'] == 'review_required'
    assert result['provenance']['asset_sha256'] == fingerprint
    assert result['provenance']['format'] == suffix[1:]
    assert 'logical_locator_not_page' in result['warnings']
    for row in result['records']:
        assert row['text_sha256'] == sha256(row['raw_text'].encode()).hexdigest()
        assert row['physical_index'] is None and row['printed_label'] is None
        assert row['printed_label_verified'] is False and row['logical_locator']
    assert sha256(source).hexdigest() == fingerprint


def test_fb2_preserves_decomposed_unicode_and_includes_authored_note_body():
    source = fb2('<section><p>A e\u0301 Б</p></section>').replace(b'</FictionBook>',
        '<body name="notes"><section><p>Authored note</p></section></body></FictionBook>'.encode())
    result = extract_xml_document(source, '.fb2')
    assert texts(result) == ['A e\u0301 Б', 'Authored note']
    assert result['records'][1]['logical_locator']['body'] == 2


@pytest.mark.parametrize('body', ['<section>VISIBLE PREFIX<p>SAFE LITERAL</p></section>',
                                '<section><p>SAFE LITERAL</p>VISIBLE TAIL</section>'])
@pytest.mark.parametrize('suffix', ['.fb2', '.fb3'])
def test_fictionbook_container_text_cannot_be_silently_dropped(body, suffix):
    source = fb2(body) if suffix == '.fb2' else fb3(body)
    with pytest.raises(ValueError, match='direct FictionBook body text'):
        extract_xml_document(source, suffix)


def test_fb3_does_not_promote_paper_page_metadata_or_image_alt_to_quote_text():
    result = extract_xml_document(fb3('<section><p>Before<img src="external" alt="private description"/> after<paper-page-break page-before="120"/></p></section>'), '.fb3')
    assert texts(result) == ['Before after']
    assert 'inline_notes_images_not_read' in result['warnings']
    assert 'unverified_print_labels_not_used' in result['warnings']


@pytest.mark.parametrize('body_rel', [
    [], [('body', REL + '/fb3-body', 'https://example.invalid/body.xml', ' TargetMode="External"')],
    [('body', REL + '/fb3-body', 'fb3/other.xml', '')],
    [('one', REL + '/fb3-body', 'fb3/body.xml', ''), ('two', REL + '/fb3-body', 'fb3/body.xml', '')],
])
def test_fb3_declared_primary_body_cannot_be_missing_ambiguous_external_or_different(body_rel):
    with ZipFile(BytesIO(fb3())) as archive:
        entries = [(i.filename, relationships(body_rel) if i.filename == '_rels/.rels'
                    else archive.read(i)) for i in archive.infolist()]
    with pytest.raises(ValueError, match='primary resource'):
        extract_xml_document(zipped(entries), '.fb3')


@pytest.mark.parametrize('content', ['<section><clipped/></section>', '<section><p>Before<unknown>lost</unknown>after</p></section>'])
def test_fb3_unknown_or_clipped_content_fails_closed(content):
    with pytest.raises(ValueError):
        extract_xml_document(fb3(content), '.fb3')


def test_odf_preserves_cell_repeats_and_logical_coordinates():
    data = odf('.ods', '<table:table table:name="S"><table:table-row table:number-rows-repeated="2"><table:table-cell table:number-columns-repeated="2"><text:p>Same</text:p></table:table-cell><table:table-cell office:value="7"/></table:table-row></table:table>')
    result = extract_xml_document(data, '.ods')
    assert texts(result) == ['Same', 'Same', '7', 'Same', 'Same', '7']
    assert [(r['logical_locator']['row'], r['logical_locator']['column']) for r in result['records']] == [(1,1),(1,2),(1,3),(2,1),(2,2),(2,3)]


def test_empty_spreadsheet_repeats_are_not_expanded_to_records():
    data = odf('.ods', '<table:table><table:table-row><table:table-cell><text:p>Visible</text:p></table:table-cell></table:table-row><table:table-row table:number-rows-repeated="1000000"><table:table-cell table:number-columns-repeated="16384"/></table:table-row></table:table>')
    assert texts(extract_xml_document(data, '.ods')) == ['Visible']


@pytest.mark.parametrize('count', ['9999999999999999999999999', '0', '-2', '1.5', '10001'])
def test_nonempty_repeat_amplification_is_rejected(count):
    data = odf('.ods', f'<table:table><table:table-row table:number-rows-repeated="{count}"><table:table-cell><text:p>Repeated</text:p></table:table-cell></table:table-row></table:table>')
    with pytest.raises(ValueError):
        extract_xml_document(data, '.ods')


def test_odf_formula_and_hidden_rows_are_explicit_omissions():
    data = odf('.ods', '<table:table><table:table-row><table:table-cell><text:p>Visible</text:p></table:table-cell><table:table-cell table:formula="of:=WEBSERVICE(&quot;https://example.invalid&quot;)"><text:p>Formula private cache</text:p></table:table-cell></table:table-row><table:table-row table:visibility="collapse"><table:table-cell><text:p>Hidden</text:p></table:table-cell></table:table-row></table:table>')
    result = extract_xml_document(data, '.ods')
    assert texts(result) == ['Visible']
    assert {'formula_cells_not_read', 'hidden_rows_not_read'} <= set(result['warnings'])


def test_odf_matrix_formula_dependents_never_become_literal_source_cells():
    source = odf('.ods', '<table:table><table:table-row><table:table-cell table:formula="of:={1,2}" table:number-matrix-columns-spanned="2" table:number-matrix-rows-spanned="1"><text:p>MASTER FORMULA</text:p></table:table-cell><table:table-cell><text:p>DERIVED MATRIX VALUE</text:p></table:table-cell><table:table-cell><text:p>SAFE LITERAL</text:p></table:table-cell></table:table-row></table:table>')
    with pytest.raises(ValueError, match='Matrix'):
        extract_xml_document(source, '.ods')


def test_hidden_odf_column_group_cannot_expose_hidden_cells():
    source = odf('.ods', '<table:table><table:table-column-group table:display="false"><table:table-column/></table:table-column-group><table:table-column/><table:table-row><table:table-cell><text:p>GROUP HIDDEN</text:p></table:table-cell><table:table-cell><text:p>SAFE LITERAL</text:p></table:table-cell></table:table-row></table:table>')
    with pytest.raises(ValueError, match='column groups'):
        extract_xml_document(source, '.ods')


@pytest.mark.parametrize('body', [
    '<text:p>SAFE LITERAL</text:p><alien:section xmlns:alien="urn:unknown">VISIBLE BUT DROPPED</alien:section>',
    '<text:section>VISIBLE PREFIX<text:p>SAFE LITERAL</text:p></text:section>',
    '<text:section><text:p>SAFE LITERAL</text:p>VISIBLE TAIL</text:section>',
])
def test_odf_foreign_or_raw_body_text_cannot_be_silently_dropped(body):
    with pytest.raises(ValueError, match='body'):
        extract_xml_document(odf('.odt', body), '.odt')


@pytest.mark.parametrize('repeat,nested', [(1, False), (3, False), (3, True)])
def test_hidden_odf_group_cannot_shift_following_logical_row_numbers(repeat, nested):
    hidden = f'<table:table-row table:number-rows-repeated="{repeat}"><table:table-cell><text:p>Hidden</text:p></table:table-cell></table:table-row>'
    if nested:
        hidden = '<table:table-row-group>' + hidden + '</table:table-row-group>'
    source = odf('.ods', '<table:table><table:table-row-group table:display="false">' + hidden + '</table:table-row-group><table:table-row><table:table-cell><text:p>Visible</text:p></table:table-cell></table:table-row></table:table>')
    with pytest.raises(ValueError, match='row groups'):
        extract_xml_document(source, '.ods')


def test_supported_hidden_rows_still_advance_repeated_row_coordinates():
    source = odf('.ods', '<table:table><table:table-row table:visibility="collapse" table:number-rows-repeated="3"><table:table-cell><text:p>Hidden</text:p></table:table-cell></table:table-row><table:table-row><table:table-cell><text:p>Visible</text:p></table:table-cell></table:table-row></table:table>')
    result = extract_xml_document(source, '.ods')
    assert texts(result) == ['Visible']
    assert result['records'][0]['logical_locator']['row'] == 4


@pytest.mark.parametrize('style_location', ['named', 'automatic'])
def test_inherited_table_visibility_never_exposes_hidden_cell_text(style_location):
    inner = f'<style:style xmlns:style="{ODF}style:1.0" xmlns:table="{TB}"><style:table-properties table:display="false"/></style:style>'
    if style_location == 'named':
        styles = f'<office:document-styles xmlns:office="{O}"><office:styles>{inner}</office:styles></office:document-styles>'
        source = odf('.ods', styles=styles)
    else:
        automatic = f'<office:automatic-styles>{inner}</office:automatic-styles>'
        with ZipFile(BytesIO(odf('.ods'))) as archive:
            entries = [(i.filename, archive.read(i).replace(b'<office:body>',
                automatic.encode() + b'<office:body>') if i.filename == 'content.xml'
                else archive.read(i)) for i in archive.infolist()]
        source = zipped(entries)
    with pytest.raises(ValueError, match='Inherited spreadsheet visibility'):
        extract_xml_document(source, '.ods')


@pytest.mark.parametrize('markup', [
    '<text:p>Before<text:hidden-text>hidden</text:hidden-text>after</text:p>',
    '<text:p>Before<text:conditional-text>conditional</text:conditional-text>after</text:p>',
    '<text:tracked-changes/><text:p>Visible</text:p>',
    '<text:p>Before<text:page-number>999</text:page-number>after</text:p>',
])
def test_odf_unsupported_visibility_changes_and_dynamic_fields_require_review(markup):
    with pytest.raises(ValueError):
        extract_xml_document(odf('.odt', markup), '.odt')


def test_odf_style_hidden_text_is_not_silently_imported():
    styles = f'<office:document-styles xmlns:office="{O}" xmlns:style="{ODF}style:1.0" xmlns:text="{T}"><office:styles><style:style><style:text-properties text:display="none"/></style:style></office:styles></office:document-styles>'
    with pytest.raises(ValueError, match='Hidden') as failure:
        extract_xml_document(odf('.odt', styles=styles), '.odt')
    assert str(failure.value) == (
        'Hidden document text requires review. '
        '[part=styles.xml; element=style:text-properties; condition=text:display]')


@pytest.mark.parametrize('separator', ['s', 'tab', 'line-break'])
def test_odf_separator_cannot_silently_discard_embedded_source(separator):
    data = odf('.odt', f'<text:p>Before<text:{separator}>Omitted source</text:{separator}>after</text:p>')
    with pytest.raises(ValueError, match='separators'):
        extract_xml_document(data, '.odt')


def test_odf_encryption_and_active_scripts_are_rejected():
    manifest = f'<manifest:manifest xmlns:manifest="{ODF}manifest:1.0"><manifest:encryption-data/></manifest:manifest>'
    with pytest.raises(ValueError, match='Encrypted'):
        extract_xml_document(odf('.odt', manifest=manifest), '.odt')
    with pytest.raises(ValueError, match='Active'):
        extract_xml_document(odf('.odt', extra=[('Scripts/javascript/code.js', 'anything')]), '.odt')


def test_pptx_hidden_slides_are_omitted_and_stored_slide_order_retained():
    result = extract_xml_document(office('.pptx', parts=[slide('Hidden', hidden=True), slide('Visible')]), '.pptx')
    assert texts(result) == ['Visible']
    assert result['records'][0]['logical_locator']['slide'] == 1
    assert 'hidden_slides_not_read' in result['warnings']


def test_pptx_field_and_hidden_shape_rejected():
    for data in [office('.pptx', parts=[slide('', body='<a:p><a:fld><a:t>Dynamic</a:t></a:fld></a:p>')], order=[1]),
                 office('.pptx', parts=[slide('Visible').replace('<p:sp>', '<p:sp hidden="1">')], order=[1])]:
        with pytest.raises(ValueError):
            extract_xml_document(data, '.pptx')


def test_pptx_text_value_nested_source_fails_closed():
    data = office('.pptx', parts=[slide('', body='<a:p><a:r><a:t>Visible<unexpected>Omitted semantics</unexpected>tail</a:t></a:r></a:p>')], order=[1])
    with pytest.raises(ValueError, match='Nested content'):
        extract_xml_document(data, '.pptx')


def test_pptx_external_primary_slide_is_not_loaded():
    data = office('.pptx', links=[('r1', R + '/slide', 'https://example.invalid/remote.xml', ' TargetMode="External"')], order=[1])
    with pytest.raises(ValueError, match='relationship'):
        extract_xml_document(data, '.pptx')


def test_xlsx_real_openpyxl_document_and_literal_unicode(tmp_path):
    from openpyxl import Workbook
    book = Workbook()
    book.active.title = 'Дослідження'
    book.active.append(['Українська', 'Slovenčina', 42])
    book.create_sheet('Later').append(['Last'])
    source = BytesIO()
    book.save(source)
    assert texts(extract_xml_document(source.getvalue(), '.xlsx')) == ['Українська', 'Slovenčina', '42', 'Last']


def test_xlsx_shared_strings_rich_text_and_formula_cache_omissions():
    rows = '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>WEBSERVICE(&quot;https://example.invalid&quot;)</f><v>Private cache</v></c><c r="C1"><v>123</v></c></row>'
    links = [('r1', R + '/worksheet', 'worksheets/sheet1.xml', ''), ('s', R + '/sharedStrings', 'sharedStrings.xml', '')]
    sst = f'<sst xmlns="{X}"><si><r><t>Книга </t></r><r><t>e\u0301</t></r><rPh sb="0" eb="2"><t>Not body</t></rPh></si></sst>'
    result = extract_xml_document(office('.xlsx', parts=[sheet(rows)], order=[1], links=links, extra=[('xl/sharedStrings.xml', sst)]), '.xlsx')
    assert texts(result) == ['Книга e\u0301', '123']
    assert 'formula_cells_not_read' in result['warnings']


@pytest.mark.parametrize('formula_attrs', ['t="array" ref="A1:B1"', 't="shared" ref="A1:B1"',
                                          't="dataTable" ref="A1:B1"', 'ref="A1:B1"'])
def test_xlsx_ranged_formula_dependents_are_not_mislabeled_as_literal_values(formula_attrs):
    rows = f'<row r="1"><c r="A1"><f {formula_attrs}>SYNTHETIC_ARRAY</f><v>1</v></c><c r="B1" t="str"><v>DERIVED ARRAY VALUE</v></c><c r="C1" t="inlineStr"><is><t>SAFE LITERAL</t></is></c></row>'
    source = office('.xlsx', parts=[sheet(rows)], order=[1])
    with pytest.raises(ValueError, match='Ranged'):
        extract_xml_document(source, '.xlsx')


@pytest.mark.parametrize('cell', [
    '<c r="A1" t="inlineStr"><is><t>Visible<unexpected>Lost</unexpected>tail</t></is></c>',
    '<c r="A1" t="str"><v>Visible<unexpected>Lost</unexpected>tail</v></c>',
    '<c r="A1" t="inlineStr"><is><r><t>Visible<unexpected>Lost</unexpected>tail</t></r></is></c>',
])
def test_xlsx_text_and_value_elements_cannot_silently_drop_nested_source(cell):
    data = office('.xlsx', parts=[sheet(f'<row r="1">{cell}</row>')], order=[1])
    with pytest.raises(ValueError, match='Nested content'):
        extract_xml_document(data, '.xlsx')


def test_xlsx_hidden_rows_columns_omitted_without_large_range_expansion():
    rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>Visible</t></is></c><c r="B1" t="inlineStr"><is><t>Hidden column</t></is></c></row><row r="2" hidden="true"><c r="A2" t="inlineStr"><is><t>Hidden row</t></is></c></row>'
    before = '<cols><col min="2" max="16384" hidden="1"/></cols>'
    result = extract_xml_document(office('.xlsx', parts=[sheet(rows, before=before)], order=[1]), '.xlsx')
    assert texts(result) == ['Visible']
    assert {'hidden_rows_not_read','hidden_columns_not_read'} <= set(result['warnings'])


@pytest.mark.parametrize('cell', [
    '<c r="XFE1"><v>outside</v></c>', '<c r="A0"><v>outside</v></c>',
    '<c r="A1" t="s"><v>9999999999999999999999999</v></c>',
    '<c r="B1"><v>one</v></c><c r="A1"><v>two</v></c>',
    '<c r="A1"><v>one</v></c><c r="A1"><v>two</v></c>',
])
def test_xlsx_bad_coordinates_order_and_shared_strings_are_rejected(cell):
    with pytest.raises(ValueError):
        extract_xml_document(office('.xlsx', parts=[sheet(f'<row r="1">{cell}</row>')], order=[1]), '.xlsx')


def test_xlsx_custom_hidden_format_does_not_leak_values():
    links = [('r1', R + '/worksheet', 'worksheets/sheet1.xml', ''), ('st', R + '/styles', 'styles.xml', '')]
    styles = f'<styleSheet xmlns="{X}"><numFmts><numFmt numFmtId="164" formatCode=";;;"/></numFmts></styleSheet>'
    source = office('.xlsx', parts=[sheet('<row r="1"><c r="A1" s="1"><v>Hidden</v></c></row>')], order=[1], links=links, extra=[('xl/styles.xml', styles)])
    with pytest.raises(ValueError, match='formatting'):
        extract_xml_document(source, '.xlsx')


@pytest.mark.parametrize('suffix,make', [('.odt', lambda: odf('.odt')), ('.pptx', lambda: office('.pptx')), ('.xlsx', lambda: office('.xlsx')), ('.fb3', fb3)])
def test_unsafe_zip_member_is_rejected_for_every_container(suffix, make):
    with ZipFile(BytesIO(make())) as archive:
        entries = [(i.filename, archive.read(i)) for i in archive.infolist()]
    with pytest.raises(ValueError, match='unsafe archive'):
        extract_xml_document(zipped([*entries, ('../escape', 'private')]), suffix)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_dtd_entity_is_rejected_without_private_payload_in_error(encoding):
    payload = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE FictionBook [<!ENTITY leak SYSTEM "file:///PRIVATE_MUST_NOT_APPEAR">]><FictionBook xmlns="{FB2}"><body><section><p>&leak;</p></section></body></FictionBook>'
    with pytest.raises(ValueError) as exc:
        extract_xml_document(payload.encode(encoding), '.fb2')
    assert 'PRIVATE_MUST_NOT_APPEAR' not in str(exc.value)


def test_xml_depth_is_bounded_during_parse():
    source = fb2('<section>' * 100 + '<p>Deep</p>' + '</section>' * 100)
    with pytest.raises(ValueError, match='structure budget'):
        extract_xml_document(source, '.fb2')


@pytest.mark.parametrize('budget', [{'max_bytes': 1}, {'max_chars': 2}, {'max_chars': True}, {'deadline': float('inf')}, {'deadline': float('nan')}])
def test_invalid_or_exhausted_budgets_are_rejected(budget):
    with pytest.raises(ValueError):
        extract_xml_document(fb2(), '.fb2', **budget)


@pytest.mark.parametrize('suffix', [None, [], {}, 12, '.zip', '.docm'])
def test_invalid_or_unsupported_format_is_a_controlled_failure(suffix):
    with pytest.raises(ValueError, match='Invalid'):
        extract_xml_document(fb2(), suffix)


def test_cancellation_inside_real_xml_parse_and_retry():
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        return count >= 30
    source = fb2('<section>' + '<p>Text</p>' * 100 + '</section>')
    with pytest.raises(InterruptedError):
        extract_xml_document(source, '.fb2', cancelled=cancelled)
    assert count == 30
    assert len(extract_xml_document(source, '.fb2')['records']) == 100


def test_deadline_inside_real_parse_uses_shared_absolute_budget(monkeypatch):
    count = 0
    def now():
        nonlocal count
        count += 1
        return 100 if count < 15 else 102
    monkeypatch.setattr(module.time, 'monotonic', now)
    source = fb2('<section>' + '<p>Text</p>' * 100 + '</section>')
    with pytest.raises(TimeoutError):
        extract_xml_document(source, '.fb2', deadline=101)
    assert count == 15


def test_record_limit_and_empty_visible_content_rejected(monkeypatch):
    monkeypatch.setattr(module, 'MAX_RECORDS', 2)
    with pytest.raises(ValueError, match='budget'):
        extract_xml_document(fb2('<section><p>A</p><p>B</p><p>C</p></section>'), '.fb2')
    with pytest.raises(ValueError, match='no supported visible text'):
        extract_xml_document(fb2('<section><p> </p></section>'), '.fb2')
