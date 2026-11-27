"""Real data-only archive roundtrips and hostile container rejection."""
from hashlib import sha256
from pathlib import Path
import os
import stat
import struct
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

import pytest

from carr_library.sync import archive as module


def source_tree(tmp_path):
    source = tmp_path / 'source'
    (source / 'backup' / 'originals').mkdir(parents=True)
    (source / module.INVENTORY).write_text('{}', encoding='utf-8')
    (source / 'backup' / 'Книга.txt').write_bytes(('Точна цитата.\r\n' * 120000).encode())
    return source


def test_stored_zip64_entries_roundtrip_unicode_empty_dirs_and_bytes(tmp_path):
    source = source_tree(tmp_path)
    output = tmp_path / 'snapshot.zip'
    result = module.pack_directory(source, output)
    assert result['sha256'] == sha256(output.read_bytes()).hexdigest()
    restored = module.unpack_archive(output, tmp_path / 'unpacked')
    assert (restored / 'backup' / 'originals').is_dir()
    assert (restored / 'backup' / 'Книга.txt').read_bytes() == (source / 'backup' / 'Книга.txt').read_bytes()
    assert result['files'] == 2
    second = tmp_path / 'same.zip'
    module.pack_directory(source, second)
    assert output.read_bytes() == second.read_bytes()


def test_actual_zip64_central_directory_accepted(tmp_path, monkeypatch):
    # Force Python's real ZIP64 EOCD generation with small synthetic files.
    import zipfile
    monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 100)
    source = source_tree(tmp_path)
    output = tmp_path / 'zip64.zip'
    module.pack_directory(source, output)
    assert b'PK\x06\x06' in output.read_bytes()[-100:]
    module.unpack_archive(output, tmp_path / 'out')


@pytest.mark.parametrize('bad', ['../escape', '/absolute', 'a\\b', 'con.txt', 'COM¹.txt', 'trailing. ', 'a/./b', 'a//b'])
def test_rejects_unsafe_members_before_creating_destination(tmp_path, bad):
    path = tmp_path / 'unsafe.zip'
    with ZipFile(path, 'w') as z:
        z.writestr(module.INVENTORY, '{}')
        # ZipInfo's constructor normalizes backslashes on Windows. Set the
        # wire filename afterwards so the hostile archive is identical there.
        entry = ZipInfo('placeholder')
        entry.filename = bad
        z.writestr(entry, 'bad')
    with pytest.raises(ValueError):
        module.unpack_archive(path, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('bad', ['a\\b', 'a\x00b'])
def test_wire_aliases_rejected_before_zipinfo_normalization(tmp_path, monkeypatch, bad):
    path = tmp_path / 'wire-name.zip'
    with ZipFile(path, 'w') as archive:
        archive.writestr(module.INVENTORY, '{}')
        entry = ZipInfo('placeholder')
        entry.filename = bad
        archive.writestr(entry, 'bad')
    assert bad.encode() in path.read_bytes()

    def no_zipfile_allocation(*args, **kwargs):
        pytest.fail('Hostile wire names must be rejected before ZipInfo normalization.')

    monkeypatch.setattr(module, 'ZipFile', no_zipfile_allocation)
    with pytest.raises(ValueError, match='Unsafe snapshot archive path'):
        module.unpack_archive(path, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('members', [('Book', 'book'), ('É', 'E\u0301'), ('Books', 'books/one.txt')])
def test_rejects_case_unicode_and_parent_alias_collisions(tmp_path, members):
    path = tmp_path / 'aliases.zip'
    with ZipFile(path, 'w') as z:
        z.writestr(module.INVENTORY, '{}')
        for name in members:
            z.writestr(name, 'x')
    with pytest.raises(ValueError):
        module.unpack_archive(path, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('kind', ['compressed', 'symlink', 'comment', 'truncated', 'oversized-directory', 'trailing'])
def test_rejects_unsupported_or_corrupt_structure(tmp_path, kind):
    path = tmp_path / 'bad.zip'
    with ZipFile(path, 'w', compression=ZIP_DEFLATED if kind == 'compressed' else 0) as z:
        z.writestr(module.INVENTORY, '{}')
        if kind == 'symlink':
            info = ZipInfo('link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, '../target')
        if kind == 'comment':
            z.comment = b'unsupported'
    raw = bytearray(path.read_bytes())
    if kind == 'truncated':
        raw = raw[:-1]
    elif kind == 'oversized-directory':
        struct.pack_into('<L', raw, len(raw) - 10, module.MAX_DIRECTORY + 1)
    elif kind == 'trailing':
        raw.extend(b'extra')
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.unpack_archive(path, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_crc_corruption_never_returns_a_valid_archive(tmp_path):
    source = source_tree(tmp_path)
    path = tmp_path / 'crc.zip'
    module.pack_directory(source, path)
    raw = bytearray(path.read_bytes())
    offset = raw.index('Точна'.encode())
    raw[offset] ^= 1
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.unpack_archive(path, tmp_path / 'private-staging')
    assert (source / 'backup' / 'Книга.txt').read_bytes().startswith('Точна'.encode())


def test_false_small_eocd_count_rejected_before_zipfile_allocation(tmp_path, monkeypatch):
    source = source_tree(tmp_path)
    path = tmp_path / 'count.zip'
    module.pack_directory(source, path)
    raw = bytearray(path.read_bytes())
    struct.pack_into('<HH', raw, len(raw) - 14, 1, 1)
    path.write_bytes(raw)
    def forbidden(*args, **kwargs):
        pytest.fail('ZipFile allocation must follow the bounded directory scan')
    monkeypatch.setattr(module, 'ZipFile', forbidden)
    with pytest.raises(ValueError, match='count'):
        module.unpack_archive(path, tmp_path / 'output')


def test_cancelled_pack_removes_own_partial_without_touching_existing(tmp_path):
    source = source_tree(tmp_path)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls > 8
    output = tmp_path / 'cancelled.zip'
    with pytest.raises(InterruptedError):
        module.pack_directory(source, output, cancelled=cancel)
    assert not output.exists()
    output.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        module.pack_directory(source, output)
    assert output.read_bytes() == b'keep'


def test_source_link_and_hardlink_fail_closed(tmp_path):
    source = source_tree(tmp_path)
    original = source / module.INVENTORY
    linked = source / 'linked'
    os.link(original, linked)
    with pytest.raises(ValueError):
        module.pack_directory(source, tmp_path / 'hardlink.zip')
    linked.unlink()
    try:
        linked.symlink_to(original)
    except OSError:
        # The hardlink boundary above remains an executed assertion on Windows
        # machines without the privilege needed to create symbolic links.
        return
    with pytest.raises(ValueError):
        module.pack_directory(source, tmp_path / 'symlink.zip')


def test_duplicate_json_fields_and_excess_metadata_fail(tmp_path):
    path = tmp_path / 'metadata.json'
    path.write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError, match='Duplicate'):
        module.read_json(path)
    path.write_bytes(b' ' * 100)
    with pytest.raises(ValueError, match='budget'):
        module.read_json(path, max_bytes=10)
