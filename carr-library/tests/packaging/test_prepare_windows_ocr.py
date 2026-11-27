"""Pinned source preparation must never execute archives or escape staging."""
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest

zstandard = pytest.importorskip('zstandard', reason='OCR source preparation requires zstandard')
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('prepare_windows_ocr_tests',
                                            ROOT / 'packaging' / 'prepare_windows_ocr.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def lock_file(root, filename='ocr-corresponding-source.tar'):
    lock = {'schema': 1, 'target': 'win_amd64', 'engine_version': 'synthetic',
            'artifacts': {}, 'output_files': {}, 'manifest': {'files': {}},
            'source_archive': {'filename': filename, 'entries': [],
                               'size': 1, 'sha256': '0' * 64}}
    path = root / 'lock.json'
    path.write_text(json.dumps(lock))
    return path


@pytest.mark.parametrize('filename', ['../must-preserve.txt', 'nested/source.tar',
                                      'wrong-name.tar', 'absolute'])
def test_source_filename_rejected_before_any_external_write(tmp_path, filename):
    sentinel = tmp_path / 'must-preserve.txt'
    sentinel.write_bytes(b'Preserve pre-existing content outside staging.')
    if filename == 'absolute':
        filename = str(sentinel)
    lock = lock_file(tmp_path, filename)
    with pytest.raises(ValueError, match='source|filename'):
        prepare.prepare(lock, tmp_path / 'cache', tmp_path / 'runtime', tmp_path / 'sources.tar')
    assert sentinel.read_bytes() == b'Preserve pre-existing content outside staging.'
    assert not (tmp_path / 'cache').exists()
    assert not (tmp_path / 'runtime').exists()
    assert not (tmp_path / 'sources.tar').exists()


@pytest.mark.parametrize('linked', ['runtime', 'sources.tar'])
def test_dangling_destination_link_is_preserved(tmp_path, linked):
    lock = lock_file(tmp_path)
    target = tmp_path / 'unavailable-target'
    link = tmp_path / linked
    try:
        link.symlink_to(target, target_is_directory=linked == 'runtime')
    except OSError:
        pytest.skip('This environment does not permit creating symbolic links')
    with pytest.raises(FileExistsError):
        prepare.prepare(lock, tmp_path / 'cache', tmp_path / 'runtime', tmp_path / 'sources.tar')
    assert link.is_symlink() and not target.exists()
    assert not (tmp_path / 'cache').exists()


def compressed_tar(path, entries):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, payload, kind in entries:
            info = tarfile.TarInfo(name)
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.type = kind
                info.linkname = '../outside-target'
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, BytesIO(payload))
    path.write_bytes(zstandard.ZstdCompressor().compress(stream.getvalue()))
    return path


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_selected_link_archive_members_are_rejected(tmp_path, kind):
    archive = compressed_tar(tmp_path / 'package.tar.zst', [('bin/tesseract.exe', b'', kind)])
    with pytest.raises(ValueError, match='archive entry'):
        prepare.selected_archive_members(archive, ['bin/tesseract.exe'])


def test_selected_traversal_name_is_rejected_without_extracting(tmp_path):
    archive = compressed_tar(tmp_path / 'package.tar.zst',
                             [('../outside-target', b'untrusted', tarfile.REGTYPE)])
    with pytest.raises(ValueError, match='relative path'):
        prepare.selected_archive_members(archive, ['../outside-target'])
    assert not (tmp_path.parent / 'outside-target').exists()


def test_package_install_hook_is_not_extracted_or_executed(tmp_path):
    sentinel = tmp_path / 'must-not-exist'
    archive = compressed_tar(tmp_path / 'package.tar.zst', [
        ('.INSTALL', f'touch "{sentinel}"'.encode(), tarfile.REGTYPE),
        ('bin/tesseract.exe', b'MZ synthetic executable; never run', tarfile.REGTYPE),
    ])
    selected = prepare.selected_archive_members(archive, ['bin/tesseract.exe'])
    assert selected == {'bin/tesseract.exe': b'MZ synthetic executable; never run'}
    assert not sentinel.exists()
    assert not (tmp_path / '.INSTALL').exists()


def test_duplicate_selected_archive_members_are_rejected(tmp_path):
    archive = compressed_tar(tmp_path / 'package.tar.zst', [
        ('bin/tesseract.exe', b'first', tarfile.REGTYPE),
        ('bin/tesseract.exe', b'second', tarfile.REGTYPE),
    ])
    with pytest.raises(ValueError, match='duplicate'):
        prepare.selected_archive_members(archive, ['bin/tesseract.exe'])


def test_prepare_manifest_uses_lf_bytes_and_retains_exact_source_tar(tmp_path):
    from hashlib import sha256

    note = 'Synthetic offline source notice.\n'
    note_bytes = note.encode('utf-8')
    expected_source = BytesIO()
    with tarfile.open(fileobj=expected_source, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo('SOURCE.txt')
        info.mode, info.mtime = 0o644, 0
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.size = len(note_bytes)
        archive.addfile(info, BytesIO(note_bytes))
    source_bytes = expected_source.getvalue()
    output_bytes = b'Synthetic OCR preparation fixture.\n'
    output_record = {'size': len(output_bytes), 'sha256': sha256(output_bytes).hexdigest()}
    manifest = {'schema': 1, 'files': {'README.txt': output_record}}
    lock = {'schema': 1, 'target': 'win_amd64', 'engine_version': 'synthetic',
            'artifacts': {},
            'output_files': {'README.txt': {'text': output_bytes.decode('utf-8'), **output_record}},
            'manifest': manifest,
            'source_archive': {'filename': 'ocr-corresponding-source.tar',
                'entries': [{'name': 'SOURCE.txt', 'text': note}],
                'size': len(source_bytes), 'sha256': sha256(source_bytes).hexdigest()}}
    lock_path = tmp_path / 'lock.json'
    lock_path.write_bytes(json.dumps(lock).encode('utf-8'))
    output, source = tmp_path / 'runtime', tmp_path / 'sources.tar'
    result = prepare.prepare(lock_path, tmp_path / 'cache', output, source)
    raw = (output / 'ocr-manifest.json').read_bytes()
    assert raw.endswith(b'\n') and b'\r' not in raw
    assert json.loads(raw) == manifest
    assert (output / 'README.txt').read_bytes() == output_bytes
    assert source.read_bytes() == source_bytes
    assert result['source_archive_sha256'] == sha256(source_bytes).hexdigest()
    assert result['windows_execution'] == 'NOT_RUN'
