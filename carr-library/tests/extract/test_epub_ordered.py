"""Synthetic EPUB package, ordering, text fidelity and rejection regressions."""
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile, ZIP_STORED

import pytest

from carr_library.extract import epub_ordered
from carr_library.extract.epub_ordered import extract_epub


OCF = 'urn:oasis:names:tc:opendocument:xmlns:container'
OPF = 'http://www.idpf.org/2007/opf'
XHTML = 'http://www.w3.org/1999/xhtml'
ENC = 'http://www.w3.org/2001/04/xmlenc#'


def chapter(text, *, title='Chapter', doctype='', attrs=''):
    return (f'{doctype}<html xmlns="{XHTML}" {attrs}><head><title>{title}</title></head>'
            f'<body>{text}</body></html>')


def epub(*, documents=None, manifest=None, spine=None, package=None,
         container=None, extra=(), version='3.0'):
    """Documents are ordered ZIP members, intentionally independent of spine."""
    documents = documents if documents is not None else [('EPUB/one.xhtml', chapter('<p>First text.</p>'))]
    manifest = manifest if manifest is not None else '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
    spine = spine if spine is not None else '<itemref idref="one"/>'
    package = package if package is not None else (
        f'<package xmlns="{OPF}" version="{version}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Synthetic book</dc:title><dc:creator>Test author</dc:creator><dc:language>uk</dc:language>'
        f'</metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
    container = container if container is not None else (
        f'<container xmlns="{OCF}"><rootfiles><rootfile full-path="EPUB/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>')
    stream = BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_STORED) as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', container)
        archive.writestr('EPUB/content.opf', package)
        for path, text in (*documents, *extra):
            archive.writestr(path, text)
    return stream.getvalue()


def encryption(path, algorithm='http://www.idpf.org/2008/embedding'):
    return (f'<encryption xmlns="{OCF}" xmlns:enc="{ENC}"><enc:EncryptedData>'
            f'<enc:EncryptionMethod Algorithm="{algorithm}"/><enc:CipherData>'
            f'<enc:CipherReference URI="{path}"/></enc:CipherData>'
            '</enc:EncryptedData></encryption>')


def test_spine_order_repeated_occurrences_supplementary_and_no_fake_pages():
    data = epub(documents=[('EPUB/two.xhtml', chapter('<p>Second Žilina</p>', title='Second')),
                           ('EPUB/one.xhtml', chapter('<p>Перша книжка</p>', title='First'))],
        manifest='<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
                 '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="one"/><itemref idref="two" linear="no"/><itemref idref="one"/>')
    result = extract_epub(data)
    assert result['status'] == 'review_required'
    assert result['provenance']['asset_sha256'] == sha256(data).hexdigest()
    assert [r['raw_text'] for r in result['records']] == ['Перша книжка', 'Second Žilina', 'Перша книжка']
    assert [r['logical_locator']['spine_index'] for r in result['records']] == [1, 2, 3]
    assert [r['logical_locator']['linear'] for r in result['records']] == [True, False, True]
    assert [r['logical_locator']['href'] for r in result['records']] == ['EPUB/one.xhtml', 'EPUB/two.xhtml', 'EPUB/one.xhtml']
    assert 'supplementary_spine_items_included' in result['warnings']
    for row in result['records']:
        assert row['physical_index'] is row['printed_label'] is None
        assert row['printed_label_verified'] is False
        assert row['text_sha256'] == sha256(row['raw_text'].encode()).hexdigest()
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result


def test_inline_tails_paragraphs_lists_tables_and_preformatted_text():
    data = epub(documents=[('EPUB/one.xhtml', chapter('''
      <h1>Heading</h1>
      <p>Inter<em>nation</em>al <strong>research</strong> &amp; Česko.</p>
      <ul><li>One</li><li>Two</li></ul>
      <table><colgroup><col/><col/></colgroup><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>
      <pre>  keep  spaces\nnext\tfield  </pre>
      <p>Last<br/>line</p>
    '''))])
    result = extract_epub(data)
    assert result['records'][0]['raw_text'] == (
        'Heading\nInternational research & Česko.\nOne\nTwo\nA\nB\nC\nD\n'
        '  keep  spaces\nnext\tfield  \nLast\nline')
    assert 'table_layout_not_rendered' in result['warnings']


def test_script_style_template_hidden_omitted_with_tails_and_until_found_retained():
    data = epub(documents=[('EPUB/one.xhtml', chapter('''<p>Before <script>bad()</script>after.
      <span hidden="">Secret</span> Visible <span hidden="until-found">Discoverable</span>.</p>
      <style>body{color:red}</style><template>Template text</template>
      <p style="DISPLAY: none !important">Hidden CSS</p><p>End.</p>'''))])
    result = extract_epub(data)
    assert result['records'][0]['raw_text'] == 'Before after. Visible Discoverable.\nHidden CSS\nEnd.'
    assert {'script_content_omitted', 'style_content_omitted', 'template_content_omitted',
            'hidden_content_omitted', 'styles_not_rendered'} <= set(result['warnings'])


@pytest.mark.parametrize('markup', [
    '<p style="display:none;display:block">Visible content</p>',
    '<p style="visibility:hidden;visibility:visible">Visible content</p>',
    '<div style="visibility:hidden"><p style="visibility:visible">Visible content</p></div>',
    '<p style="display:block!important;display:none">Visible content</p>',
])
def test_css_dependent_text_is_retained_without_guessing_rendered_visibility(markup):
    result = extract_epub(epub(documents=[('EPUB/one.xhtml', chapter('<p>Anchor</p>' + markup))]))
    assert result['records'][0]['raw_text'] == 'Anchor\nVisible content'
    assert 'styles_not_rendered' in result['warnings']


def test_images_have_separate_authored_alternatives_never_quotation_text():
    data = epub(documents=[('EPUB/one.xhtml', chapter('<p>Before<img src="https://example.invalid/a.png" '
           'alt="A map of Slovakia"/>After</p><img src="missing.png"/>'))])
    result = extract_epub(data)
    row = result['records'][0]
    assert row['raw_text'] == 'Before\nAfter'
    assert row['alternatives'] == [{'kind': 'image_alt', 'image_index': 1, 'text': 'A map of Slovakia'}]
    assert 'images_omitted_from_text' in result['warnings']
    assert 'images_without_text_alternatives' in result['warnings']


def test_decorative_empty_alt_is_not_falsely_reported_missing():
    result = extract_epub(epub(documents=[('EPUB/one.xhtml', chapter('<p>Body</p><img src="line.png" alt=""/>'))]))
    assert result['records'][0]['alternatives'] == []
    assert 'images_without_text_alternatives' not in result['warnings']


def test_image_cover_and_text_chapter_keep_original_spine_occurrences():
    data = epub(documents=[('EPUB/cover.xhtml', chapter('<img src="cover.png" alt="Cover"/>')),
                           ('EPUB/one.xhtml', chapter('<p>Body</p>'))],
        manifest='<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
                 '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="cover"/><itemref idref="one"/>')
    assert [r['raw_text'] for r in extract_epub(data)['records']] == ['', 'Body']


def test_harmless_html_doctype_and_epub2_are_supported():
    data = epub(version='2.0', documents=[('EPUB/one.xhtml', chapter('<p>Body</p>', doctype='<!DOCTYPE html>'))])
    assert extract_epub(data)['records'][0]['raw_text'] == 'Body'


@pytest.mark.parametrize('doctype', [
    '<!DOCTYPE html SYSTEM "https://example.invalid/book.dtd">',
    '<!DOCTYPE html PUBLIC "bad" "file:///synthetic-private-path">',
    '<!DOCTYPE html [<!ENTITY sneaky "secret">]>',
    '<!DOCTYPE html [<!ATTLIST p hidden CDATA "hidden">]>',
])
def test_external_and_internal_dtd_are_rejected_without_source_disclosure(doctype):
    data = epub(documents=[('EPUB/one.xhtml', chapter('<p>Body</p>', doctype=doctype))])
    with pytest.raises(ValueError, match='DTD|XML') as error:
        extract_epub(data)
    assert 'synthetic-private-path' not in str(error.value)


def test_percent_encoded_unicode_space_and_safe_parent_resolution():
    data = epub(documents=[('Text/číslo one.xhtml', chapter('<p>Číslo</p>'))],
        manifest='<item id="one" href="../Text/%C4%8D%C3%ADslo%20one.xhtml" media-type="application/xhtml+xml"/>')
    result = extract_epub(data)
    assert result['records'][0]['logical_locator']['href'] == 'Text/číslo one.xhtml'


@pytest.mark.parametrize('href', ['../../outside.xhtml', '%2e%2e/%2e%2e/private.xhtml',
    '/absolute.xhtml', 'https://example.invalid/chapter.xhtml', '//example.invalid/chapter.xhtml',
    'one.xhtml#fragment', 'one.xhtml?q=1', 'one%2Ftwo.xhtml', 'one%5ctwo.xhtml', '%00.xhtml', '%ZZ.xhtml'])
def test_unsafe_or_remote_primary_resource_rejected(href):
    data = epub(manifest=f'<item id="one" href="{href}" media-type="application/xhtml+xml"/>')
    with pytest.raises(ValueError):
        extract_epub(data)


def test_unused_remote_manifest_resource_is_not_fetched():
    data = epub(manifest='<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="remote" href="https://example.invalid/font.ttf" media-type="font/ttf"/>')
    assert 'remote_resources_not_loaded' in extract_epub(data)['warnings']


@pytest.mark.parametrize('markup', [
    '<svg xmlns="http://www.w3.org/2000/svg"><text>Math</text></svg>',
    '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>',
    '<form><input value="Text"/></form>', '<object>Object text</object>',
    '<iframe>Frame text</iframe>', '<ruby>Base<rt>Annotation</rt></ruby>',
])
def test_unsupported_content_cannot_silently_shorten_a_sentence(markup):
    data = epub(documents=[('EPUB/one.xhtml', chapter('<p>Before ' + markup + ' after.</p>'))])
    with pytest.raises(ValueError, match='adapter|unsupported'):
        extract_epub(data)


def test_svg_spine_requires_separate_adapter():
    data = epub(manifest='<item id="one" href="one.xhtml" media-type="image/svg+xml"/>')
    with pytest.raises(ValueError, match='XHTML'):
        extract_epub(data)


@pytest.mark.parametrize('where', ['html', 'manifest'])
def test_xml_base_is_rejected(where):
    if where == 'html':
        data = epub(documents=[('EPUB/one.xhtml', chapter('<p>Text</p>', attrs='xml:base="https://example.invalid/"'))])
    else:
        data = epub(manifest='<item id="one" href="one.xhtml" xml:base="../" media-type="application/xhtml+xml"/>')
    with pytest.raises(ValueError, match='xml:base'):
        extract_epub(data)


def test_obfuscated_unused_fonts_are_allowed_but_never_rendered():
    data = epub(manifest='<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="font" href="font.ttf" media-type="font/ttf"/>',
        extra=(('EPUB/font.ttf', b'obfuscated synthetic font'),
               ('META-INF/encryption.xml', encryption('EPUB/font.ttf'))))
    assert 'obfuscated_fonts_not_rendered' in extract_epub(data)['warnings']


def test_encrypted_primary_content_is_rejected_even_with_obfuscation_algorithm():
    data = epub(extra=(('META-INF/encryption.xml', encryption('EPUB/one.xhtml')),))
    with pytest.raises(ValueError, match='Encrypted EPUB content'):
        extract_epub(data)


def test_unknown_encryption_is_rejected_for_non_content_resource():
    data = epub(manifest='<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="font" href="font.ttf" media-type="font/ttf"/>',
        extra=(('EPUB/font.ttf', b'font'), ('META-INF/encryption.xml', encryption('EPUB/font.ttf', 'unknown'))))
    with pytest.raises(ValueError, match='obfuscated'):
        extract_epub(data)


def test_invalid_missing_and_duplicate_manifest_ids_are_rejected():
    for manifest in ('<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>' * 2,
                     '<item id="other" href="one.xhtml" media-type="application/xhtml+xml"/>',
                     '<item id="" href="one.xhtml" media-type="application/xhtml+xml"/>'):
        with pytest.raises(ValueError):
            extract_epub(epub(manifest=manifest))


def test_missing_chapter_and_malformed_xml_fail_explicitly():
    with pytest.raises(ValueError, match='missing'):
        extract_epub(epub(documents=[]))
    with pytest.raises(ValueError, match='XML'):
        extract_epub(epub(documents=[('EPUB/one.xhtml', '<html>broken')]))


def test_multiple_renditions_are_not_implicitly_selected():
    roots = '<rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>'
    data = epub(container=f'<container xmlns="{OCF}"><rootfiles>{roots}{roots}</rootfiles></container>')
    with pytest.raises(ValueError, match='renditions'):
        extract_epub(data)


@pytest.mark.parametrize('limit,value', [('MAX_XML_DEPTH', 4), ('MAX_XML_NODES', 5),
    ('MAX_TOTAL_NODES', 5), ('MAX_CHAPTER_XML', 30), ('MAX_TOTAL_XML', 30),
    ('MAX_TEXT', 3), ('MAX_LOCATORS', 10)])
def test_parser_and_text_resource_budgets(limit, value, monkeypatch):
    monkeypatch.setattr(epub_ordered, limit, value)
    data = epub(documents=[('EPUB/one.xhtml', chapter('<section><div><p>Bounded text</p></div></section>'))])
    with pytest.raises(ValueError, match='budget|limit'):
        extract_epub(data)


def test_spine_occurrences_are_bounded_even_when_xml_is_cached(monkeypatch):
    monkeypatch.setattr(epub_ordered, 'MAX_SPINE', 2)
    with pytest.raises(ValueError, match='spine'):
        extract_epub(epub(spine='<itemref idref="one"/>' * 3))
    monkeypatch.setattr(epub_ordered, 'MAX_SPINE', 10)
    monkeypatch.setattr(epub_ordered, 'MAX_TEXT', 15)
    with pytest.raises(ValueError, match='budget'):
        extract_epub(epub(spine='<itemref idref="one"/>' * 3))


def test_real_spine_boundary_matches_reader_limit():
    result = extract_epub(epub(spine='<itemref idref="one"/>' * 1000))
    assert len(result['records']) == 1000
    assert result['records'][-1]['logical_locator']['spine_index'] == 1000
    with pytest.raises(ValueError, match='spine'):
        extract_epub(epub(spine='<itemref idref="one"/>' * 1001))


def test_text_outside_xhtml_body_is_not_silently_ignored():
    invalid = chapter('<p>Body</p>').replace('</body>', '</body><p>Omitted prose</p>')
    with pytest.raises(ValueError, match='structure'):
        extract_epub(epub(documents=[('EPUB/one.xhtml', invalid)]))


def test_archive_traversal_and_duplicate_entries_are_rejected():
    with pytest.raises(ValueError, match='archive'):
        extract_epub(epub(extra=(('../private.txt', 'data'),)))
    with pytest.warns(UserWarning, match='Duplicate name'):
        data = epub(extra=(('EPUB/one.xhtml', chapter('<p>Different</p>')),))
    with pytest.raises(ValueError, match='archive'):
        extract_epub(data)


def test_title_falls_back_to_heading_and_metadata_is_only_unverified_hints():
    result = extract_epub(epub(documents=[('EPUB/one.xhtml', chapter('<h1>  A <em>clear</em> heading </h1><p>Body</p>', title=''))]))
    assert result['records'][0]['logical_locator']['title'] == 'A clear heading'
    assert result['metadata_hints'] == {'verification': 'unverified', 'titles': ['Synthetic book'],
                                        'creators': ['Test author'], 'languages': ['uk']}


def test_source_snapshot_type_and_empty_text():
    for data in (b'', bytearray(epub()), b'not a zip'):
        with pytest.raises(ValueError):
            extract_epub(data)
    with pytest.raises(ValueError, match='no readable'):
        extract_epub(epub(documents=[('EPUB/one.xhtml', chapter('<p> </p>'))]))
