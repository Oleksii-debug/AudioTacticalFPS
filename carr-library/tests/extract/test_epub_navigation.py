"""Additive contents navigation over the canonical EPUB text extractor."""
from hashlib import sha256
from io import BytesIO
import json
import posixpath
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile

import pytest

from carr_library.extract.epub_ordered import EpubError, extract_epub


XHTML = 'http://www.w3.org/1999/xhtml'
EPUB = 'http://www.idpf.org/2007/ops'
NCX = 'http://www.daisy.org/z3986/2005/ncx/'


def chapter(body, *, title='Chapter', head=''):
    return (f'<html xmlns="{XHTML}" xmlns:epub="{EPUB}"><head>'
            f'<title>{escape(title)}</title>{head}</head><body>{body}</body></html>')


def navigation(items, pages='', *, head=''):
    return chapter(f'<nav epub:type="toc"><ol>{items}</ol></nav>' + (
        f'<nav epub:type="page-list"><ol>{pages}</ol></nav>' if pages else ''), head=head)


def build_epub(chapters=None, *, spine=None, nav=None, ncx=None, version='3.0',
               package_member='EPUB/content.opf', nav_member='EPUB/nav.xhtml', extra=None):
    """Synthetic EPUB bytes; chapters are (id, full ZIP member, XHTML) tuples.

    Default chapters are ``EPUB/one.xhtml#one`` and ``EPUB/two.xhtml#two``.
    Spine values are IDs or (ID, linear) pairs. The helper is reusable by
    contents-service tests without altering the existing extractor fixtures.
    """
    if chapters is None:
        chapters = [('one', 'EPUB/one.xhtml', chapter('<h1 id="one">One</h1><p>First text.</p>')),
                    ('two', 'EPUB/two.xhtml', chapter('<h1 id="two">Two</h1><p>Second text.</p>'))]
    spine = [row[0] for row in chapters] if spine is None else spine
    directory = posixpath.dirname(package_member)
    resources = [(member, source) for _, member, source in chapters]
    manifest = ''.join(f'<item id={quoteattr(ident)} href={quoteattr(quote(posixpath.relpath(member, directory)))} '
        'media-type="application/xhtml+xml"/>' for ident, member, _ in chapters)
    if nav is not None:
        manifest += f'<item id="nav" href={quoteattr(quote(posixpath.relpath(nav_member, directory)))} '
        manifest += 'media-type="application/xhtml+xml" properties="nav"/>'
        resources.append((nav_member, nav))
    if ncx is not None:
        manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        resources.append((posixpath.join(directory, 'toc.ncx'), ncx))
    references = ''.join(f'<itemref idref={quoteattr(row if isinstance(row, str) else row[0])}'
        + ('' if isinstance(row, str) else f' linear={quoteattr(row[1])}') + '/>' for row in spine)
    package = (f'<package xmlns="http://www.idpf.org/2007/opf" version={quoteattr(version)}>'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Synthetic</dc:title></metadata>'
        f'<manifest>{manifest}</manifest><spine' + (' toc="ncx"' if ncx is not None else '')
        + f'>{references}</spine></package>')
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            f'<rootfile full-path={quoteattr(package_member)} media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>')
        archive.writestr(package_member, package)
        for member, source in resources:
            archive.writestr(member, source)
        for member, source in (extra or {}).items():
            archive.writestr(member, source)
    return stream.getvalue()


def test_trimmed_text_headings_and_end_markers_share_final_offsets():
    body = ('\n <p>  Before <em>  text </em>  </p> \n '
            '<h2 id="a">  A <b>B<em>C</em>D</b>E   </h2> '
            '<p>Trailing    </p> <span id="end"/> ')
    nav = navigation('<li><a href="one.xhtml#a">A<b>B<em>C</em>D</b>E</a></li>'
                     '<li><a href="one.xhtml#end">End</a></li>')
    result = extract_epub(build_epub([('one', 'EPUB/one.xhtml', chapter(body, title=''))], nav=nav))
    record = result['records'][0]
    assert record['raw_text'] == 'Before text\nA BCDE\nTrailing'
    assert record['logical_locator'] == dict(spine_index=1, manifest_id='one',
        href='EPUB/one.xhtml', linear=True, title='A BCDE')
    assert record['headings'] == [dict(title='A BCDE', offset=12, level=2)]
    assert result['toc'] == [dict(title='ABCDE', ordinal=1, offset=12, depth=0),
                             dict(title='End', ordinal=1, offset=len(record['raw_text']), depth=0)]
    assert record['text_sha256'] == sha256(record['raw_text'].encode()).hexdigest()


@pytest.mark.parametrize('body,fragment,raw,offset', [
    ('<p>Before  <img id="image" alt="Map"/><span id="after">After</span></p>',
     'after', 'Before\nAfter', 7),
    ('<p>Before  <span id="end"/> </p><p>After</p>', 'end', 'Before\nAfter', 6),
    ('<p>Before</p><pre id="pre">  Keep  spaces\nnext\tfield  </pre>',
     'pre', 'Before\n  Keep  spaces\nnext\tfield  ', 7),
    ('<table><tr><td>A</td><td>B</td></tr></table><p id="next">After<br/>line</p>',
     'next', 'A\nB\nAfter\nline', 4),
    ('<p>Before<br id="break"/></p>', 'break', 'Before\n', 7),
])
def test_generated_breaks_preformatted_text_and_trims_move_markers(body, fragment, raw, offset):
    nav = navigation(f'<li><a href="one.xhtml#{fragment}">Target</a></li>')
    result = extract_epub(build_epub([('one', 'EPUB/one.xhtml', chapter(body))], nav=nav))
    assert result['records'][0]['raw_text'] == raw
    assert result['toc'][0]['offset'] == offset


def test_spine_order_unicode_parent_paths_groups_and_page_hints():
    chapters = [('two', 'Text/two.xhtml', chapter('<p id="two">Second</p>')),
                ('one', 'Text/розділ.xhtml', chapter('<h1 id="початок">Перший</h1><p>Текст</p>'))]
    nav = navigation('<li><span>Part <em>one</em></span><ol><li>'
        '<a href="../Text/%D1%80%D0%BE%D0%B7%D0%B4%D1%96%D0%BB.xhtml#%D0%BF%D0%BE%D1%87%D0%B0%D1%82%D0%BE%D0%BA">Перший</a>'
        '</li><li><a href="../Text/two.xhtml#two">Second</a></li></ol></li>',
        '<li><a href="../Text/розділ.xhtml#початок">xiv</a></li>')
    data = build_epub(chapters, spine=['one', ('two', 'no')], nav=nav)
    result = extract_epub(data)
    assert result['toc'] == [dict(title='Part one', ordinal=None, offset=None, depth=0, heading_only=True),
        dict(title='Перший', ordinal=1, offset=0, depth=1), dict(title='Second', ordinal=2, offset=0, depth=1)]
    assert result['page_hints'] == [dict(title='xiv', ordinal=1, offset=0, depth=0)]
    assert result['records'][1]['logical_locator']['linear'] is False
    assert result['status'] == 'review_required'
    assert 'page_list_labels_unverified' in result['warnings']
    assert result['provenance']['asset_sha256'] == sha256(data).hexdigest()
    assert result['provenance']['xml_parser'] == 'defusedxml'
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    for record in result['records']:
        assert record['physical_index'] is record['printed_label'] is None
        assert record['printed_label_verified'] is False


def test_repeated_spine_keeps_legacy_sections_and_discloses_navigation_ambiguity():
    nav = navigation('<li><a href="one.xhtml#one">One</a></li><li><a href="two.xhtml#two">Two</a></li>')
    result = extract_epub(build_epub(spine=['one', 'two', 'one'], nav=nav))
    assert result['toc'] == result['page_hints'] == []
    assert 'embedded_contents_ambiguous_spine' in result['warnings']
    assert [row['logical_locator']['spine_index'] for row in result['records']] == [1, 2, 3]
    assert result['records'][0]['raw_text'] == result['records'][2]['raw_text']
    assert result['records'][0]['headings'] == result['records'][2]['headings']
    result['records'][0]['headings'][0]['title'] = 'Edited copy'
    assert result['records'][2]['headings'][0]['title'] == 'One'


def test_chapter_warnings_are_separate_without_renaming_global_warnings():
    chapters = [('one', 'EPUB/one.xhtml', chapter('<p>One<img alt="Map"/></p>')),
                ('two', 'EPUB/two.xhtml', chapter('<p>Two</p>'))]
    result = extract_epub(build_epub(chapters))
    assert result['records'][0]['warnings'] == ['images_omitted_from_text']
    assert result['records'][1]['warnings'] == []
    assert 'images_omitted_from_text' in result['warnings']
    assert result['records'][0]['alternatives'] == [dict(kind='image_alt', image_index=1, text='Map')]


def test_missing_embedded_contents_are_explicit_and_headings_remain_available():
    result = extract_epub(build_epub())
    assert result['toc'] == result['page_hints'] == []
    assert 'embedded_contents_unavailable' in result['warnings']
    assert result['records'][0]['headings'] == [dict(title='One', offset=0, level=1)]


def test_ncx_nested_contents_and_page_hints():
    ncx = (f'<ncx xmlns="{NCX}"><navMap><navPoint id="a"><navLabel><text>Two</text></navLabel>'
        '<content src="two.xhtml#two"/><navPoint id="b"><navLabel><text>One</text></navLabel>'
        '<content src="one.xhtml#one"/></navPoint></navPoint></navMap><pageList><pageTarget>'
        '<navLabel><text>ix</text></navLabel><content src="one.xhtml#one"/></pageTarget></pageList></ncx>')
    result = extract_epub(build_epub(version='2.0', spine=['two', 'one'], ncx=ncx))
    assert result['toc'] == [dict(title='Two', ordinal=1, offset=0, depth=0),
                             dict(title='One', ordinal=2, offset=0, depth=1)]
    assert result['page_hints'] == [dict(title='ix', ordinal=2, offset=0, depth=0)]


def test_xhtml_base_cannot_choose_the_wrong_valid_target():
    chapters = [('one', 'OPS/nav/chapter.xhtml', chapter('<p id="start">One</p>')),
                ('two', 'OPS/other/chapter.xhtml', chapter('<p id="start">Two</p>'))]
    nav = navigation('<li><a href="chapter.xhtml#start">Two</a></li>', head='<base href="../other/"/>')
    with pytest.raises(EpubError, match='base'):
        extract_epub(build_epub(chapters, nav=nav, nav_member='OPS/nav/nav.xhtml'))


@pytest.mark.parametrize('href', ['https://example.invalid/private', '//example.invalid/book',
    '../../outside.xhtml', '%2e%2e/%2e%2e/private.xhtml', '%2Fone.xhtml', 'one%5c.xhtml',
    'one.xhtml?query=private', 'one.xhtml?', 'file:///private', 'one%00.xhtml',
    'one%ZZ.xhtml', 'one.xhtml#missing', 'one.xhtml#bad%00', 'one.xhtml#bad%FF',
    'missing.xhtml', 'nav.xhtml', '', ' one.xhtml', 'one.xhtml '])
def test_unsafe_or_dangling_navigation_target_is_not_silently_accepted(href):
    nav = navigation(f'<li><a href={quoteattr(href)}>Target</a></li>')
    with pytest.raises(ValueError) as caught:
        extract_epub(build_epub(nav=nav))
    assert 'example.invalid' not in str(caught.value) and 'private' not in str(caught.value)


def test_duplicate_anchor_is_legacy_readable_but_not_a_navigation_destination():
    chapters = [('one', 'EPUB/one.xhtml', chapter('<p id="same">A</p><p id="same">B</p>'))]
    assert extract_epub(build_epub(chapters))['records'][0]['raw_text'] == 'A\nB'
    nav = navigation('<li><a href="one.xhtml#same">Ambiguous</a></li>')
    with pytest.raises(EpubError, match='anchor.*ambiguous'):
        extract_epub(build_epub(chapters, nav=nav))


def test_page_hints_cannot_be_nonlink_group_headings():
    nav = navigation('<li><a href="one.xhtml">One</a></li>', '<li><span>ix</span></li>')
    with pytest.raises(EpubError, match='page hint'):
        extract_epub(build_epub(nav=nav))


def test_navigation_uses_parser_declared_encoding_and_harmless_html_doctype():
    nav = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE html>'
        + navigation('<li><a href="one.xhtml#one">Зміст 東京</a></li>')).encode('utf-16')
    result = extract_epub(build_epub(nav=nav))
    assert result['toc'][0]['title'] == 'Зміст 東京'


@pytest.mark.parametrize('prefix', [
    '<!DOCTYPE html [<!ENTITY x "expanded">]>',
    '<!DOCTYPE html SYSTEM "https://example.invalid/private.dtd">',
])
def test_navigation_cannot_introduce_dtd_or_entities(prefix):
    with pytest.raises(ValueError):
        extract_epub(build_epub(nav=prefix + navigation('<li><a href="one.xhtml">One</a></li>')))


def test_headings_contents_and_page_hints_share_one_budget(monkeypatch):
    from carr_library.extract import epub_ordered
    nav = navigation('<li><a href="one.xhtml">One</a></li>', '<li><a href="two.xhtml">ix</a></li>')
    monkeypatch.setattr(epub_ordered, 'MAX_NAVIGATION', 3)
    with pytest.raises(EpubError, match='navigation budget'):
        extract_epub(build_epub(nav=nav))


def test_navigation_bytes_and_anchor_counts_are_bounded(monkeypatch):
    from carr_library.extract import epub_ordered
    with monkeypatch.context() as patch:
        patch.setattr(epub_ordered, 'MAX_NAVIGATION_BYTES', 20)
        with pytest.raises(EpubError, match='navigation budget'):
            extract_epub(build_epub())
    with monkeypatch.context() as patch:
        patch.setattr(epub_ordered, 'MAX_ANCHORS', 1)
        with pytest.raises(EpubError, match='anchor identity or budget'):
            extract_epub(build_epub())
