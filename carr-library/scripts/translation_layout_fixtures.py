"""Original synthetic EPUB sources shared only by native/conformance probes.

No provider, model, Word dependency or user asset is used.
"""
from io import BytesIO
import struct
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED
import zlib


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

