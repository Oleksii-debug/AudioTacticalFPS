"""Read-only container screening using CPython zipfile; not an XML parser.

No filesystem extraction. The caller must pass the bounded original snapshot
and later parse those same bytes in the extraction worker.
"""
from io import BytesIO
from pathlib import PureWindowsPath
import ntpath
import stat
import unicodedata
import zipfile
import zlib

MAX_MEMBERS = 10_000
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MEMBER = 32 * 1024 * 1024
FORMATS = {
    '.docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document',
              {'[Content_Types].xml', '_rels/.rels', 'word/document.xml'}),
    '.xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              {'[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml'}),
    '.pptx': ('application/vnd.openxmlformats-officedocument.presentationml.presentation',
              {'[Content_Types].xml', '_rels/.rels', 'ppt/presentation.xml'}),
    '.odt': ('application/vnd.oasis.opendocument.text',
             {'mimetype', 'content.xml', 'META-INF/manifest.xml'}),
    '.ods': ('application/vnd.oasis.opendocument.spreadsheet',
             {'mimetype', 'content.xml', 'META-INF/manifest.xml'}),
    '.odp': ('application/vnd.oasis.opendocument.presentation',
             {'mimetype', 'content.xml', 'META-INF/manifest.xml'}),
    '.fb3': ('application/x-fictionbook3',
             {'[Content_Types].xml', '_rels/.rels', 'fb3/body.xml'}),
    '.epub': ('application/epub+zip', {'mimetype', 'META-INF/container.xml'}),
}


def _reserved(part):
    checker = getattr(ntpath, "isreserved", None)
    return checker(part) if checker else PureWindowsPath(part).is_reserved()


def _name(info):
    name = info.orig_filename
    parts = name.removesuffix('/').split('/')
    if (not name or name != info.filename or '\\' in name or ':' in name
            or any(ord(c) < 32 for c in name)
            or any(p in ('', '.', '..') or p.rstrip(' .') != p
                   or _reserved(p) for p in parts)):
        raise ValueError('unsafe archive member')
    mode = stat.S_IFMT(info.external_attr >> 16)
    expected = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
    if mode not in (0, expected) or info.external_attr & 0x400:
        raise ValueError('archive link or special file')
    return unicodedata.normalize('NFC', '/'.join(parts)).casefold()


def validate_office(data: bytes, suffix: str, *, max_expanded=MAX_EXPANDED,
                    max_members=MAX_MEMBERS, max_member=MAX_MEMBER) -> str:
    """Check container structure, resource budget and member CRCs, not contents.

    Required filenames are format hints; actual XML/content validity remains
    the extractor's responsibility. Only ZIP_STORED and ZIP_DEFLATED accepted.
    """
    if suffix not in FORMATS or not 0 < max_expanded <= MAX_EXPANDED or not 0 < max_member <= MAX_MEMBER or not 0 < max_members <= MAX_MEMBERS:
        raise ValueError('unsupported archive or invalid limit')
    mime, required = FORMATS[suffix]
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if not members or len(members) > max_members:
                raise ValueError('archive member limit')
            names = {}
            files = set()
            total = 0
            for info in members:
                key = _name(info)
                if key in names:
                    raise ValueError('duplicate or aliased archive member')
                names[key] = info.is_dir()
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError('encrypted or unsupported compression')
                total += info.file_size
                if info.file_size > max_member or total > max_expanded:
                    raise ValueError('archive expansion limit')
                if not info.is_dir():
                    files.add(info.filename)
                elif info.file_size:
                    raise ValueError('directory contains data')
            for key in names:
                parts = key.split('/')
                if any(names.get('/'.join(parts[:i])) is False for i in range(1, len(parts))):
                    raise ValueError('archive path conflicts with file')
            if not required <= files:
                raise ValueError('missing document container markers')
            office_roots = {'word/document.xml', 'xl/workbook.xml', 'ppt/presentation.xml', 'fb3/body.xml'}
            if suffix in ('.docx', '.xlsx', '.pptx', '.fb3') and len(office_roots & files) != 1:
                raise ValueError('ambiguous document container')
            if suffix == '.epub':
                marker = archive.getinfo('mimetype')
                if marker.file_size != len(b'application/epub+zip') or marker.compress_type != zipfile.ZIP_STORED or archive.read(marker) != b'application/epub+zip':
                    raise ValueError('invalid EPUB mimetype')
            if suffix in ('.odt', '.ods', '.odp'):
                marker = archive.getinfo('mimetype')
                expected = mime.encode('ascii')
                if (marker.file_size != len(expected) or marker.compress_type != zipfile.ZIP_STORED
                        or archive.read(marker) != expected):
                    raise ValueError('invalid OpenDocument mimetype')
            consumed = 0
            for info in members:
                actual = 0
                with archive.open(info) as stream:
                    while chunk := stream.read(64 * 1024):
                        actual += len(chunk)
                        consumed += len(chunk)
                        if actual > max_member or consumed > max_expanded or actual > info.file_size:
                            raise ValueError('archive expansion limit')
                if actual != info.file_size:
                    raise ValueError('archive size mismatch')
            return mime
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise ValueError('invalid or corrupt document archive') from exc
