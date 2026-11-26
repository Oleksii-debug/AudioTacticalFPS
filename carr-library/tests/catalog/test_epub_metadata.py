from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile
import pytest
from carr_library.catalog.epub_metadata import preview_epub, MAX_XML

OCF = 'urn:oasis:names:tc:opendocument:xmlns:container'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
ROOT = '<rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>'


def fixture(fields='', roots=ROOT, opf=None, extra=None):
    stream = BytesIO()
    with ZipFile(stream, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', f'<container xmlns="{OCF}"><rootfiles>{roots}</rootfiles></container>')
        z.writestr('EPUB/content.opf', opf if opf is not None else
                   f'<package xmlns="{OPF}" xmlns:dc="{DC}"><metadata>{fields}</metadata></package>')
        if extra:
            z.writestr(extra, 'synthetic')
    data = stream.getvalue()
    return SimpleNamespace(data=data, digest=sha256(data).hexdigest(), size=len(data), mime='application/epub+zip')


def test_multilingual_and_multiple_creators():
    s = fixture('<dc:title>Історія</dc:title><dc:creator>А</dc:creator><dc:creator>B</dc:creator><dc:language>uk</dc:language><dc:identifier>synthetic-id</dc:identifier><dc:publisher>Test</dc:publisher>')
    h = preview_epub(s)
    assert (h.titles, h.creators, h.languages, h.identifiers, h.publishers) == (('Історія',), ('А', 'B'), ('uk',), ('synthetic-id',), ('Test',))
    assert h.verification == 'unverified' and h.asset_sha256 == s.digest


def test_unknown():
    h = preview_epub(fixture())
    assert not any((h.titles, h.creators, h.languages, h.identifiers, h.publishers))


@pytest.mark.parametrize('roots', ['', ROOT + ROOT, ROOT.replace('EPUB/content.opf', '../secret.opf'), ROOT.replace('EPUB/content.opf', 'https://example.invalid/book.opf')])
def test_bad_root(roots):
    with pytest.raises(ValueError, match='preview rejected'):
        preview_epub(fixture(roots=roots))


@pytest.mark.parametrize('xml', ['<!DOCTYPE package [<!ENTITY x "abc">]><package>&x;</package>', '<!DOCTYPE package SYSTEM "file:///synthetic"><package/>', '<notclosed>', 'x' * (MAX_XML + 1)])
def test_bad_xml(xml):
    with pytest.raises(ValueError, match='preview rejected'):
        preview_epub(fixture(opf=xml))


def test_archive_traversal():
    with pytest.raises(ValueError, match='preview rejected'):
        preview_epub(fixture(extra='../synthetic'))


def test_hash_mismatch():
    s = fixture()
    s.digest = '0' * 64
    with pytest.raises(ValueError, match='invalid EPUB snapshot'):
        preview_epub(s)


def test_markup_not_flattened():
    assert preview_epub(fixture('<dc:title>Prefix<b>nested</b></dc:title>')).titles == ()
