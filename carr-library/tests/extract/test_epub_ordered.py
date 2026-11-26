"""Synthetic real EPUB archives; no installed books or remote services required."""
from hashlib import sha256
from io import BytesIO
import json
import posixpath
from urllib.parse import unquote
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile

import pytest

from carr_library.extract.epub_ordered import EpubError, extract_epub


OCF = 'urn:oasis:names:tc:opendocument:xmlns:container'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
XHTML = 'http://www.w3.org/1999/xhtml'
EPUB = 'http://www.idpf.org/2007/ops'
NCX = 'http://www.daisy.org/z3986/2005/ncx/'


def xhtml(body, *, title='Synthetic chapter', head=''):
    return (f'<html xmlns="{XHTML}" xmlns:epub="{EPUB}"><head>'
            f'<title>{escape(title)}</title>{head}</head><body>{body}</body></html>')


def navigation(items, pages=''):
    return xhtml(f'<nav epub:type="toc"><ol>{items}</ol></nav>' + (
        f'<nav epub:type="page-list"><ol>{pages}</ol></nav>' if pages else ''))


def build_epub(chapters=None, *, spine=None, nav=None, ncx=None, extra=None,
               manifest_extra='', version='3.0', metadata=None, spine_attrs='',
               package_member='EPUB/content.opf', package=None):
    """Build an EPUB byte snapshot reusable by application/integration tests.

    ``chapters`` contains (manifest id, relative href, complete XHTML str/bytes).
    ``spine`` contains ids or (id, linear) pairs. Omitted inputs build two short
    chapters with anchors ``one`` and ``two``. Extra ZIP entries use full paths.
    ``nav`` is a complete navigation XHTML; ``ncx`` is a complete NCX document.
    """
    if chapters is None:
        chapters = [('one', 'one.xhtml', xhtml('<h1 id="one">One</h1><p>Alpha beta.</p>')),
                    ('two', 'two.xhtml', xhtml('<h1 id="two">Two</h1><p>Gamma delta.</p>'))]
    if spine is None:
        spine = [row[0] for row in chapters]
    manifest = ''.join(f'<item id={quoteattr(ident)} href={quoteattr(href)} '
                       'media-type="application/xhtml+xml"/>' for ident, href, _ in chapters)
    resources = [(posixpath.normpath(posixpath.join(posixpath.dirname(package_member),
                         unquote(href))), source) for _, href, source in chapters]
    if nav is not None:
        manifest += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        resources.append((posixpath.join(posixpath.dirname(package_member), 'nav.xhtml'), nav))
    if ncx is not None:
        manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        resources.append((posixpath.join(posixpath.dirname(package_member), 'toc.ncx'), ncx))
        spine_attrs += ' toc="ncx"'
    references = ''.join(f'<itemref idref={quoteattr(row if isinstance(row, str) else row[0])}'
        + ('' if isinstance(row, str) else f' linear={quoteattr(row[1])}') + '/>' for row in spine)
    if metadata is None:
        metadata = '<dc:title>Навчальна книга</dc:title><dc:creator>Test author</dc:creator><dc:language>uk</dc:language>'
    if package is None:
        package = (f'<package xmlns="{OPF}" xmlns:dc="{DC}" version={quoteattr(version)}>'
            f'<metadata>{metadata}</metadata><manifest>{manifest}{manifest_extra}</manifest>'
            f'<spine {spine_attrs}>{references}</spine></package>')
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', f'<container xmlns="{OCF}"><rootfiles>'
            f'<rootfile full-path={quoteattr(package_member)} media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>')
        archive.writestr(package_member, package)
        for member, content in resources:
            archive.writestr(member, content)
        for member, content in (extra or {}).items():
            archive.writestr(member, content)
    return stream.getvalue()


def replace_member(data, member, transform):
    stream = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(stream, 'w') as target:
        for info in source.infolist():
            content = source.read(info)
            target.writestr(info, transform(content) if info.filename == member else content)
    return stream.getvalue()


def encryption(uri, algorithm='http://www.idpf.org/2008/embedding'):
    return ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        f'<enc:EncryptionMethod Algorithm={quoteattr(algorithm)}/><enc:CipherData>'
        f'<enc:CipherReference URI={quoteattr(uri)}/></enc:CipherData></enc:EncryptedData></encryption>')


def test_spine_order_unicode_inline_boundaries_and_original_identity():
    chapters = [('second', 'second.xhtml', xhtml('<h1>Second</h1><p>After.</p>')),
                ('first', '%D0%BF%D0%B5%D1%80%D1%88%D0%B8%D0%B9.xhtml',
                 xhtml('<h1 id="початок">Перший</h1><p>Це <em>цілісний</em> текст.</p>'))]
    data = build_epub(chapters, spine=['first', 'second'])
    original_digest = sha256(data).hexdigest()
    result = extract_epub(data)
    assert [r['title'] for r in result['records']] == ['Перший', 'Second']
    assert result['records'][0]['member'] == 'EPUB/перший.xhtml'
    assert result['records'][0]['raw_text'] == 'Перший\nЦе цілісний текст.\n'
    assert result['status'] == 'review_required'
    assert result['metadata']['titles'] == ('Навчальна книга',)
    assert result['metadata']['verification'] == 'unverified'
    assert result['provenance']['asset_sha256'] == original_digest == sha256(data).hexdigest()
    assert result['provenance']['adapter'] == 'epub-spine-v1'
    assert result['provenance']['engine'] == 'lxml'
    assert json.loads(json.dumps(result))['records'][0]['logical_locator'] == [1]
    for ordinal, record in enumerate(result['records'], 1):
        assert record['logical_locator'] == [ordinal]
        assert record['physical_index'] is None and record['printed_label'] is None
        assert record['printed_label_verified'] is False
        assert record['text_sha256'] == sha256(record['raw_text'].encode()).hexdigest()


def test_navigation_outside_spine_offsets_grouping_and_nested_inline_labels():
    label = 'A<b>B<em>C</em>D</b>E'
    chapter = xhtml(f'<p>Before</p><h2 id="anchor">{label}</h2><p>After.</p>')
    nav = navigation('<li><span>Part <em>one</em></span><ol>'
        f'<li><a href="one.xhtml#anchor">{label}</a></li></ol></li>',
        '<li><a href="one.xhtml#anchor">xiv</a></li>')
    result = extract_epub(build_epub([('one', 'one.xhtml', chapter)], nav=nav))
    record = result['records'][0]
    assert record['headings'] == [dict(title='ABCDE', level=2, offset=7)]
    assert record['raw_text'][7:].startswith('ABCDE\n')
    assert result['toc'] == [dict(title='Part one', ordinal=None, offset=None,
                                  depth=0, heading_only=True),
                             dict(title='ABCDE', ordinal=1, offset=7, depth=1)]
    assert result['page_hints'] == [dict(title='xiv', ordinal=1, offset=7, depth=0)]
    assert 'page_list_labels_unverified' in result['warnings']
    assert record['printed_label'] is None and record['physical_index'] is None
    assert len(result['records']) == 1


def test_navigation_parent_path_within_archive_and_unicode_fragment():
    nav = navigation('<li><a href="../Text/розділ.xhtml#%D0%BF%D0%BE%D1%87%D0%B0%D1%82%D0%BE%D0%BA">Початок</a></li>')
    result = extract_epub(build_epub([('one', '../Text/розділ.xhtml',
                xhtml('<p id="початок">Текст</p>'))], nav=nav))
    assert result['toc'] == [dict(title='Початок', ordinal=1, offset=0, depth=0)]


def test_html_base_cannot_silently_redirect_a_valid_contents_target():
    # Both paths and fragments exist, so ignoring <base> would silently pick
    # the wrong chapter instead of producing a simple missing-resource error.
    chapters = [('first', 'chapter.xhtml', xhtml('<p id="start">First</p>')),
                ('second', '../other/chapter.xhtml', xhtml('<p id="start">Second</p>'))]
    nav = xhtml('<nav epub:type="toc"><ol><li><a href="chapter.xhtml#start">Second chapter</a>'
                '</li></ol></nav>', head='<base href="../other/"/>')
    with pytest.raises(EpubError, match='document base overrides'):
        extract_epub(build_epub(chapters, nav=nav, package_member='OPS/nav/content.opf'))


def test_auxiliary_image_cover_retained_and_descriptions_are_separate():
    chapters = [('cover', 'cover.xhtml', xhtml('<img src="cover.png" alt="Cover description"/>')),
                ('main', 'main.xhtml', xhtml('<p>Main text.</p>')),
                ('note', 'note.xhtml', xhtml('<p>Auxiliary note.</p>'))]
    result = extract_epub(build_epub(chapters, spine=['cover', 'main', ('note', 'no')],
                                    extra={'EPUB/cover.png': b'synthetic image'}))
    cover = result['records'][0]
    assert cover['raw_text'] == ''
    assert cover['image_descriptions'] == ['Cover description']
    assert {'chapter_has_no_readable_text', 'images_not_transcribed'} <= set(cover['warnings'])
    assert result['records'][2]['linear'] is False
    assert result['records'][2]['logical_locator'] == [3]


def test_styles_and_scripts_never_execute_or_claim_rendered_text():
    chapter = xhtml('<p>Visible<script>secret payload()</script> tail</p><style>p{display:none}</style>',
        head='<script src="https://example.invalid/unused.js"/><link rel="stylesheet" href="style.css"/>')
    result = extract_epub(build_epub([('one', 'one.xhtml', chapter)]))
    assert result['records'][0]['raw_text'] == 'Visible tail\n'
    assert {'scripts_not_executed', 'styles_not_rendered'} <= set(result['records'][0]['warnings'])
    assert 'layout_not_verified' in result['warnings']


def test_epub2_ncx_nested_contents_spine_order():
    ncx = (f'<ncx xmlns="{NCX}" version="2005-1"><navMap><navPoint id="a" playOrder="9">'
        '<navLabel><text>Second first</text></navLabel><content src="two.xhtml#two"/>'
        '<navPoint id="b" playOrder="1"><navLabel><text>First second</text></navLabel>'
        '<content src="one.xhtml#one"/></navPoint></navPoint></navMap></ncx>')
    result = extract_epub(build_epub(version='2.0', spine=['two', 'one'], ncx=ncx))
    assert [r['title'] for r in result['records']] == ['Two', 'One']
    assert result['toc'] == [dict(title='Second first', ordinal=1, offset=0, depth=0),
                             dict(title='First second', ordinal=2, offset=0, depth=1)]


def test_xml_declared_utf16_is_decoded_by_xml_parser():
    chapter = ('<?xml version="1.0" encoding="UTF-16"?>' + xhtml('<p>Київ — 東京</p>')).encode('utf-16')
    result = extract_epub(build_epub([('one', 'one.xhtml', chapter)]))
    assert result['records'][0]['raw_text'] == 'Київ — 東京\n'


@pytest.mark.parametrize('chapter', [
    '<!DOCTYPE html [<!ENTITY x "expanded">]>' + xhtml('<p>&x;</p>'),
    '<!DOCTYPE html SYSTEM "https://example.invalid/private.dtd">' + xhtml('<p>Text</p>'),
    '<!DOCTYPE html [<!ENTITY x SYSTEM "file:///private/sensitive.txt">]>' + xhtml('<p>&x;</p>'),
    ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE html [<!ENTITY x "expanded">]>'
        + xhtml('<p>&x;</p>')).encode('utf-16'),
    xhtml('<p>unclosed'),
])
def test_dtd_entities_malformed_xml_rejected_without_source_details(chapter):
    with pytest.raises(EpubError) as caught:
        extract_epub(build_epub([('one', 'one.xhtml', chapter)]))
    assert all(secret not in str(caught.value) for secret in ('private', 'sensitive', 'example.invalid', 'expanded'))


@pytest.mark.parametrize('href', ['https://example.invalid/book', '//example.invalid/book',
    '../../secret.xhtml', '%2e%2e/%2e%2e/secret.xhtml', '%2Fsecret.xhtml',
    '%5Csecret.xhtml', 'one.xhtml?query=private', 'file:///private',
    'one%00.xhtml', 'one%ZZ.xhtml', 'one.xhtml#missing', 'missing.xhtml', 'nav.xhtml', ''])
def test_unsafe_or_dangling_navigation_targets_rejected(href):
    nav = navigation(f'<li><a href={quoteattr(href)}>Title</a></li>')
    with pytest.raises(EpubError):
        extract_epub(build_epub(nav=nav))


@pytest.mark.parametrize('mutation', [
    lambda s: s.replace('id="two"', 'id="one"'),
    lambda s: s.replace('<itemref idref="two"/>', '<itemref idref="one"/>'),
    lambda s: s.replace('<itemref idref="two"/>', '<itemref idref="missing"/>'),
    lambda s: s.replace('href="two.xhtml"', 'href="one.xhtml"'),
    lambda s: s.replace('href="one.xhtml"', 'href=""'),
    lambda s: s.replace('href="one.xhtml"', 'href="https://example.invalid/remote.xhtml"'),
    lambda s: s.replace('<manifest>', '<manifest xml:base="https://example.invalid/">'),
])
def test_ambiguous_or_unsafe_package_rejected(mutation):
    data = replace_member(build_epub(), 'EPUB/content.opf', lambda b: mutation(b.decode()).encode())
    with pytest.raises(EpubError):
        extract_epub(data)


@pytest.mark.parametrize('body', [
    '<h1 id="duplicate">A</h1><p id="duplicate">B</p>',
    '<p xml:base="../">Text</p>',
    '<p>Text <math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math></p>',
    '<svg xmlns="http://www.w3.org/2000/svg"><text>Hidden passage</text></svg>',
    '<p><ruby>字<rt>zì</rt></ruby></p>',
    '<object data="unknown.dat">Fallback text</object>',
])
def test_ambiguous_anchors_and_unsupported_text_shapes_rejected(body):
    with pytest.raises(EpubError):
        extract_epub(build_epub([('one', 'one.xhtml', xhtml(body))]))


def test_font_obfuscation_allowed_but_encrypted_text_rejected():
    data = build_epub(manifest_extra='<item id="font" href="font.otf" media-type="font/otf"/>',
        extra={'EPUB/font.otf': b'synthetic obfuscated font',
               'META-INF/encryption.xml': encryption('EPUB/font.otf')})
    assert extract_epub(data)['records'][0]['raw_text'].startswith('One\n')
    data = build_epub(extra={'META-INF/encryption.xml': encryption('EPUB/one.xhtml',
                           'http://www.w3.org/2001/04/xmlenc#aes256-cbc')})
    with pytest.raises(EpubError, match='Encrypted EPUB textual'):
        extract_epub(data)


@pytest.mark.parametrize('kwargs', [
    dict(chapters=[('one', 'one.xhtml', xhtml('<img alt="Text is in image"/>'))]),
    dict(spine=[('one', 'no'), ('two', 'no')]),
    dict(version='4.0'),
    dict(metadata='<meta property="rendition:layout">pre-paginated</meta>'),
])
def test_empty_and_unsupported_books_fail_explicitly(kwargs):
    with pytest.raises(EpubError):
        extract_epub(build_epub(**kwargs))


def test_navigation_and_text_budgets_are_global(monkeypatch):
    import carr_library.extract.epub_ordered as adapter
    data = build_epub(nav=navigation('<li><a href="one.xhtml#one">One</a></li>'))
    monkeypatch.setattr(adapter, 'MAX_NAVIGATION', 2)
    with pytest.raises(EpubError, match='navigation exceeds'):
        extract_epub(data)
    monkeypatch.setattr(adapter, 'MAX_NAVIGATION', 2000)
    monkeypatch.setattr(adapter, 'MAX_TEXT', 24)
    with pytest.raises(EpubError, match='text exceeds'):
        extract_epub(data)


def test_cancellation_stays_distinct_from_invalid_source():
    with pytest.raises(InterruptedError, match='cancelled'):
        extract_epub(build_epub(), cancelled=lambda: True)


def test_identical_snapshots_have_identical_extraction():
    data = build_epub(nav=navigation('<li><a href="two.xhtml#two">Two</a></li>'))
    assert extract_epub(data) == extract_epub(data)


def test_no_test_only_defusedxml_dependency(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'defusedxml', None)
    monkeypatch.setitem(sys.modules, 'defusedxml.ElementTree', None)
    result = extract_epub(build_epub())
    assert result['metadata']['verification'] == 'unverified'
    assert result['provenance']['engine'] == 'lxml'


@pytest.mark.parametrize('mutation', [
    lambda s: s.replace('</body>', '</body><p>Lost passage</p>'),
    lambda s: s.replace('<head>', 'Lost passage<head>'),
    lambda s: s.replace('</head>', '</head>Lost passage'),
    lambda s: s.replace('</head>', '</head><head><title>Other title</title></head>'),
    lambda s: s.replace('</title>', '</title><title>Other title</title>'),
    lambda s: s.replace('</head>', '<p>Lost passage</p></head>'),
    lambda s: s.replace('</title>', '<b>Illegal title markup</b></title>'),
])
def test_prose_outside_body_and_ambiguous_titles_are_not_silently_lost(mutation):
    chapter = mutation(xhtml('<p>Readable text</p>'))
    with pytest.raises(EpubError):
        extract_epub(build_epub([('one', 'one.xhtml', chapter)]))


@pytest.mark.parametrize('markup', [
    '<img>Lost passage</img>', '<img><p>Lost passage</p></img>',
    '<br>Lost passage</br>', '<link>Lost passage</link>',
    '<meta>Lost passage</meta>', '<source>Lost passage</source>',
    '<col>Lost passage</col>', '<wbr>Lost passage</wbr>',
    '<form><input value="Lost passage"/></form>', '<input value="Lost passage"/>',
    '<widget>Unknown representation</widget>', '<body>Nested body</body>',
])
def test_void_and_unknown_xhtml_cannot_silently_drop_source(markup):
    with pytest.raises(EpubError, match='unsupported'):
        extract_epub(build_epub([('one', 'one.xhtml', xhtml('<p>Start</p>' + markup))]))


def test_static_formatting_warnings_keep_text_and_offsets_deterministic():
    body = ('<h1 id="start">Heading</h1><p style="display:none;display:block">'
        'Old <del>removed</del><ins>added</ins></p><table><colgroup><col/></colgroup>'
        '<tbody><tr><td>A</td><td>B</td></tr></tbody></table><noscript>Fallback</noscript>'
        '<template>Not in the reading flow</template><p>Final</p>')
    result = extract_epub(build_epub([('one', 'one.xhtml', xhtml(body))]))
    record = result['records'][0]
    assert record['raw_text'] == 'Heading\nOld removedadded\nA\nB\nFallback\nFinal\n'
    assert record['headings'] == [dict(title='Heading', offset=0, level=1)]
    assert {'styles_not_rendered', 'revision_markup_not_rendered', 'table_layout_not_rendered',
            'noscript_fallback_included', 'template_content_omitted'} <= set(record['warnings'])


def test_navigation_document_requires_xhtml_structure():
    nav = navigation('<li><a href="one.xhtml">One</a></li>').replace('</body>', '</body><p>Lost</p>')
    with pytest.raises(EpubError, match='document structure'):
        extract_epub(build_epub(nav=nav))
