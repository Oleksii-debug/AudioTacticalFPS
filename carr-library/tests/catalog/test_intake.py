from hashlib import sha256
import zipfile

import pytest

from carr_library.catalog.intake import IntakeError, snapshot


def test_exact_unicode_bytes_and_duplicate(tmp_path):
    data = 'Історія\nSame title\n'.encode()
    for name in ('one.txt', 'two.txt'):
        (tmp_path / name).write_bytes(data)
    a = snapshot(tmp_path, 'one.txt')
    b = snapshot(tmp_path, 'two.txt')
    assert a == b
    assert a.data == data
    assert a.manifest() == {'sha256': sha256(data).hexdigest(), 'mime': 'text/plain', 'size': len(data)}


@pytest.mark.parametrize('name,data', [
    ('x.txt', b'MZfake'), ('x.txt', b'\x7fELF'), ('x.txt', b'%PDF-1.7'),
    ('x.pdf', b'not PDF'), ('x.pdf', b'%PDF'), ('x.txt', b'\x00abc'),
    ('x.txt', b'\xff'), ('x.txt', b''), ('x.exe', b'plain'),
])
def test_mismatches(tmp_path, name, data):
    (tmp_path / name).write_bytes(data)
    with pytest.raises(IntakeError):
        snapshot(tmp_path, name)


@pytest.mark.parametrize('relative', ['../outside.txt', '/a.txt', 'C:/a.txt', 'a\\b.txt', 'a//b.txt', 'NUL.txt', 'a.txt:stream'])
def test_unsafe_path(tmp_path, relative):
    with pytest.raises(IntakeError):
        snapshot(tmp_path, relative)


def test_zip_slip_is_not_extracted(tmp_path):
    p = tmp_path / 'archive.txt'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('../escape.txt', 'unsafe')
    with pytest.raises(IntakeError):
        snapshot(tmp_path, 'archive.txt')
    assert not (tmp_path.parent / 'escape.txt').exists()


def test_limit(tmp_path):
    (tmp_path / 'x.txt').write_bytes(b'abcde')
    with pytest.raises(IntakeError):
        snapshot(tmp_path, 'x.txt', max_bytes=4)


def test_symlink(tmp_path):
    (tmp_path / 'real.txt').write_text('hello')
    try:
        (tmp_path / 'link.txt').symlink_to(tmp_path / 'real.txt')
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(IntakeError):
        snapshot(tmp_path, 'link.txt')


def test_pdf_and_rtf_screening(tmp_path):
    # These are header fixtures, not structurally validated documents.
    (tmp_path / 'a.pdf').write_bytes(b'%PDF-1.7\n')
    (tmp_path / 'b.rtf').write_bytes(b'{\\rtf1 hello}')
    assert snapshot(tmp_path, 'a.pdf').mime == 'application/pdf'
    assert snapshot(tmp_path, 'b.rtf').mime == 'application/rtf'
