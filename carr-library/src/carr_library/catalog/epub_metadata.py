"""Read-only EPUB metadata hints; never source identity or verified attribution."""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

OCF = '{urn:oasis:names:tc:opendocument:xmlns:container}'
OPF = '{http://www.idpf.org/2007/opf}'
DC = '{http://purl.org/dc/elements/1.1/}'
MAX_XML = 1024 * 1024


@dataclass(frozen=True)
class EpubHints:
    asset_sha256: str
    titles: tuple[str, ...]
    creators: tuple[str, ...]
    languages: tuple[str, ...]
    identifiers: tuple[str, ...]
    publishers: tuple[str, ...]
    verification: str = field(default='unverified', init=False)


def _read_xml(archive, name, *, max_bytes=MAX_XML):
    """Read bounded XML with the lxml runtime already supplied by python-docx.

    Do not resolve DTDs or entities, including local-file entities. Inspect the
    parsed document declaration as well so alternate XML encodings cannot hide
    a DTD from a byte-pattern check. EPUB parsing belongs in the parser worker.
    """
    from lxml import etree
    if type(max_bytes) is not int or not 0 < max_bytes <= 8 * 1024 * 1024:
        raise ValueError('invalid XML resource limit')
    info = archive.getinfo(name)
    if info.is_dir() or info.file_size > max_bytes:
        raise ValueError('invalid metadata member')
    with archive.open(info) as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes or len(data) != info.file_size:
        raise ValueError('metadata size mismatch')
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                             recover=False, huge_tree=False)
    root = etree.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError('DTD content is unsupported')
    return root


def _values(metadata, tag):
    result = []
    for node in metadata.findall(DC + tag):
        # Do not flatten markup or infer identity from creator role refinements.
        value = node.text or ''
        if (not len(node) and value.strip() and len(value) <= 4096
                and all(ord(c) >= 32 and ord(c) != 127 for c in value)):
            result.append(value)
    return tuple(result)


def preview_epub(snapshot):
    """Call in the approved parser worker; never reopen an original path."""
    from .office_archive import validate_office
    data = snapshot.data
    if (snapshot.mime != 'application/epub+zip' or not isinstance(data, bytes)
            or not 0 < len(data) <= 64 * 1024 * 1024
            or snapshot.size != len(data) or sha256(data).hexdigest() != snapshot.digest):
        raise ValueError('invalid EPUB snapshot')
    try:
        validate_office(data, '.epub')
        with ZipFile(BytesIO(data)) as archive:
            container = _read_xml(archive, 'META-INF/container.xml')
            roots = container.findall(OCF + 'rootfiles/' + OCF + 'rootfile')
            if (container.tag != OCF + 'container' or len(roots) != 1
                    or roots[0].get('media-type') != 'application/oebps-package+xml'):
                raise ValueError('missing or ambiguous EPUB rendition')
            name = roots[0].get('full-path', '')
            if not name or not name.lower().endswith('.opf') or '%' in name:
                raise ValueError('unsupported rootfile path')
            package = _read_xml(archive, name)
            metadata = package.findall(OPF + 'metadata')
            if package.tag != OPF + 'package' or len(metadata) != 1:
                raise ValueError('missing or ambiguous metadata')
            return EpubHints(snapshot.digest, *(_values(metadata[0], tag) for tag in
                ('title', 'creator', 'language', 'identifier', 'publisher')))
    except ImportError:
        raise ValueError('EPUB metadata support unavailable') from None
    except Exception:
        # Do not include private paths, raw XML or parser error text.
        raise ValueError('EPUB metadata preview rejected') from None
