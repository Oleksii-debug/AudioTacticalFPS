from hashlib import sha256
from io import BytesIO
import stat
import zipfile

import pytest
from carr_library.catalog.intake import IntakeError, snapshot
from carr_library.catalog.office_archive import validate_office


def package(extra=(), suffix='.docx'):
    out = BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        if suffix == '.epub':
            z.writestr('mimetype', b'application/epub+zip')
            z.writestr('META-INF/container.xml', b'container')
        else:
            z.writestr('[Content_Types].xml', b'types')
            z.writestr('_rels/.rels', b'rels')
            z.writestr('word/document.xml' if suffix == '.docx' else 'xl/workbook.xml', b'synthetic')
        for name, data in extra:
            z.writestr(name, data)
    return out.getvalue()


@pytest.mark.parametrize('suffix', ['.docx', '.xlsx', '.epub'])
def test_snapshot(tmp_path, suffix):
    data = package(suffix=suffix)
    path = tmp_path / ('book' + suffix)
    path.write_bytes(data)
    snap = snapshot(tmp_path, path.name)
    assert snap.digest == sha256(data).hexdigest() and snap.data == data
    assert snap.mime == validate_office(data, suffix)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('name', ['../escape.xml', '/absolute.xml', 'C:/x.xml', 'a\\b.xml', 'NUL.xml', 'word/DOCUMENT.xml', 'a//b.xml', 'dir./x.xml'])
def test_unsafe_member(tmp_path, name):
    path = tmp_path / 'book.docx'
    path.write_bytes(package([(name, b'x')]))
    with pytest.raises(IntakeError):
        snapshot(tmp_path, path.name)


def test_symlink_and_collision():
    link = zipfile.ZipInfo('link.xml')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for entries in [[(link, b'target')], [('a', b'x'), ('a/b', b'y')], [('é', b'x'), ('e\u0301', b'y')]]:
        with pytest.raises(ValueError):
            validate_office(package(entries), '.docx')


def test_limits():
    data = package([('big.xml', b'x' * 40)])
    for opts in [{'max_member': 20}, {'max_expanded': 30}, {'max_members': 3}]:
        with pytest.raises(ValueError):
            validate_office(data, '.docx', **opts)


def test_wrong_type_and_corrupt():
    data = package()
    for bad, suffix in [(data, '.xlsx'), (data, '.txt'), (data[:-10], '.docx'), (data.replace(b'synthetic', b'Synthetic'), '.docx')]:
        with pytest.raises(ValueError):
            validate_office(bad, suffix)


def test_real_generated_office(tmp_path):
    docx = pytest.importorskip('docx')
    openpyxl = pytest.importorskip('openpyxl')
    document = docx.Document()
    document.add_paragraph('Synthetic історія')
    document.save(tmp_path / 'a.docx')
    workbook = openpyxl.Workbook()
    workbook.active['A1'] = 'Synthetic історія'
    workbook.save(tmp_path / 'a.xlsx')
    for name in ['a.docx', 'a.xlsx']:
        assert snapshot(tmp_path, name).data == (tmp_path / name).read_bytes()
