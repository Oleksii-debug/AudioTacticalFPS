"""Portable, read-only archive acceptance on both Windows path API generations."""
from io import BytesIO
import stat
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_BZIP2

import pytest
from carr_library.catalog import office_archive as office


FORMATS = [
    ('.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', ('[Content_Types].xml', '_rels/.rels', 'word/document.xml')),
    ('.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ('[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml')),
    ('.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', ('[Content_Types].xml', '_rels/.rels', 'ppt/presentation.xml')),
    ('.fb3', 'application/x-fictionbook3', ('[Content_Types].xml', '_rels/.rels', 'fb3/body.xml')),
    ('.odt', 'application/vnd.oasis.opendocument.text', ('mimetype', 'content.xml', 'META-INF/manifest.xml')),
    ('.ods', 'application/vnd.oasis.opendocument.spreadsheet', ('mimetype', 'content.xml', 'META-INF/manifest.xml')),
    ('.odp', 'application/vnd.oasis.opendocument.presentation', ('mimetype', 'content.xml', 'META-INF/manifest.xml')),
    ('.epub', 'application/epub+zip', ('mimetype', 'META-INF/container.xml')),
]


def package(entries):
    out = BytesIO()
    with ZipFile(out, 'w') as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return out.getvalue()


def docx(extra=()):
    return package([(name, b'fixture') for name in FORMATS[0][2]] + list(extra))


@pytest.mark.parametrize('suffix,mime,markers', FORMATS)
def test_supported_container(suffix, mime, markers):
    data = package([(name, mime.encode('ascii') if name == 'mimetype' else b'fixture') for name in markers]
                   + [('розділ/資料.xml', b'Unicode')])
    assert office.validate_office(data, suffix) == mime


@pytest.mark.parametrize('suffix,mime,markers', FORMATS)
def test_missing_required_marker(suffix, mime, markers):
    data = package([(name, mime.encode('ascii') if name == 'mimetype' else b'fixture') for name in markers[:-1]])
    with pytest.raises(ValueError, match='missing document container markers'):
        office.validate_office(data, suffix)


@pytest.mark.parametrize('bad', ['a?.xml', 'a*.xml', 'a<.xml', 'a>.xml', 'a".xml', 'a|.xml'])
def test_legacy_windows_path_api_rejects_forbidden_characters(monkeypatch, bad):
    # Python 3.11/3.12 do not have ntpath.isreserved; their pathlib method
    # detects DOS device names, not arbitrary wildcard/punctuation characters.
    # Emulate that API behavior on 3.13 without pretending to run Python 3.11.
    monkeypatch.delattr(office.ntpath, 'isreserved', raising=False)
    monkeypatch.setattr(office.PureWindowsPath, 'is_reserved', lambda self: False)
    with pytest.raises(ValueError, match='unsafe archive member'):
        office.validate_office(docx([(bad, b'x')]), '.docx')


@pytest.mark.parametrize('bad', ['a?.xml', 'a*.xml', 'a<.xml', 'a>.xml', 'a".xml', 'a|.xml', 'a\x01.xml', 'a/../b.xml'])
def test_forbidden_member_current_api(bad):
    with pytest.raises(ValueError, match='unsafe archive member'):
        office.validate_office(docx([(bad, b'x')]), '.docx')


@pytest.mark.parametrize('entries', [
    [('dir', b'a'), ('dir/file.xml', b'b')],
    [('DIR', b'a'), ('dir/file.xml', b'b')],
    [('é.xml', b'a'), ('e\u0301.xml', b'b')],
    [('doc.xml', b'a'), ('DOC.xml', b'b')],
])
def test_path_collisions(entries):
    with pytest.raises(ValueError, match='archive path conflicts|aliased archive member'):
        office.validate_office(docx(entries), '.docx')


@pytest.mark.parametrize('mode', [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK])
def test_special_member_rejected(mode):
    info = ZipInfo('special.xml')
    info.create_system = 3
    info.external_attr = (mode | 0o644) << 16
    with pytest.raises(ValueError, match='archive link or special file'):
        office.validate_office(docx([(info, b'x')]), '.docx')


@pytest.mark.parametrize('limit', [{'max_member': 6}, {'max_expanded': 20}, {'max_members': 2}])
def test_resource_bound(limit):
    with pytest.raises(ValueError, match='archive (expansion|member) limit'):
        office.validate_office(docx(), '.docx', **limit)


@pytest.mark.parametrize('suffix,mime,markers', FORMATS[4:])
def test_compressed_mimetype_rejected(suffix, mime, markers):
    marker = ZipInfo('mimetype')
    marker.compress_type = ZIP_DEFLATED
    data = package([(marker, mime.encode('ascii'))] + [(name, b'fixture') for name in markers[1:]])
    with pytest.raises(ValueError, match='invalid (EPUB|OpenDocument) mimetype'):
        office.validate_office(data, suffix)


def test_unknown_compression_rejected():
    marker = ZipInfo('extra.xml')
    marker.compress_type = ZIP_BZIP2
    with pytest.raises(ValueError, match='unsupported compression'):
        office.validate_office(docx([(marker, b'x')]), '.docx')


def test_corrupt_member_crc_rejected():
    data = docx([('extra.xml', b'checksum-unique-marker')])
    corrupted = data.replace(b'checksum-unique-marker', b'Checksum-unique-marker')
    with pytest.raises(ValueError, match='invalid or corrupt document archive'):
        office.validate_office(corrupted, '.docx')


def test_directory_with_payload_rejected():
    with pytest.raises(ValueError, match='directory contains data'):
        office.validate_office(docx([('dir/', b'x')]), '.docx')


def test_multiple_document_roots_rejected():
    with pytest.raises(ValueError, match='ambiguous document container'):
        office.validate_office(docx([('xl/workbook.xml', b'x')]), '.docx')


def test_exact_resource_bounds_are_inclusive():
    assert office.validate_office(docx(), '.docx', max_member=7, max_expanded=21, max_members=3) == FORMATS[0][1]


def test_wrong_mimetype_rejected():
    data = package([('mimetype', b'application/epub+zip'), ('content.xml', b'x'), ('META-INF/manifest.xml', b'x')])
    with pytest.raises(ValueError, match='invalid OpenDocument mimetype'):
        office.validate_office(data, '.odt')


def test_empty_archive_rejected():
    with pytest.raises(ValueError, match='archive member limit'):
        office.validate_office(package([]), '.docx')


def test_non_document_suffix_rejected():
    with pytest.raises(ValueError, match='unsupported archive'):
        office.validate_office(docx(), '.zip')
