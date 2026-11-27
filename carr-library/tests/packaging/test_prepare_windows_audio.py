"""Offline packaging safety using synthetic ZIPs; no executable is run."""
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import struct
import zipfile

import pytest


MODULE = Path(__file__).resolve().parents[2] / 'packaging' / 'prepare_windows_audio.py'
spec = importlib.util.spec_from_file_location('prepare_windows_audio', MODULE)
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)


def pe(machine=0x8664):
    value = bytearray(128)
    value[:2] = b'MZ'
    struct.pack_into('<I', value, 0x3C, 64)
    value[64:68] = b'PE\x00\x00'
    struct.pack_into('<H', value, 68, machine)
    struct.pack_into('<H', value, 88, 0x20B)
    return bytes(value)


def fixture(tmp_path, *, extra=(), machine=0x8664):
    cache = tmp_path / 'cache'
    cache.mkdir()
    source = cache / 'synthetic.zip'
    data = {'bin/ffmpeg.exe': pe(machine), 'bin/ffprobe.exe': pe(machine),
            'LICENSE': b'Synthetic license fixture.', 'README.txt': b'Synthetic source fixture.'}
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in data.items():
            archive.writestr('release/' + name, raw)
        for name, raw in extra:
            if isinstance(name, str):
                # Preserve deliberately unsafe separators on Windows; the
                # ZIP constructor otherwise sanitizes the tested input.
                info = zipfile.ZipInfo('synthetic-placeholder')
                info.filename = info.orig_filename = name
                info.compress_type = zipfile.ZIP_DEFLATED
            else:
                info = name
            archive.writestr(info, raw)
    lock = {'schema': 1, 'target': 'win_amd64', 'engine': 'FFmpeg', 'engine_version': 'synthetic',
            'license': 'GPL-3.0-only', 'source': {'revision': 'synthetic'},
            'artifact': {'url': 'https://github.com/GyanD/codexffmpeg/releases/download/test/synthetic.zip',
                         'filename': source.name, 'size': source.stat().st_size,
                         'sha256': audio.digest(source), 'format': 'zip'},
            'deployment': {'download_default': False, 'public_binary_distribution': False},
            'output_files': {name: {'member': 'release/' + name, 'size': len(raw),
                                   'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in data.items()}}
    path = tmp_path / 'lock.json'
    path.write_text(json.dumps(lock), encoding='utf-8')
    return path, cache, tmp_path / 'runtime', lock


def test_offline_prepare_verifies_each_file_and_retains_notices(tmp_path):
    path, cache, output, lock = fixture(tmp_path)
    result = audio.prepare(path, cache, output)
    assert result['status'] == 'VERIFIED_AND_PREPARED'
    assert result['execution_test'] == 'NOT_RUN' and not result['public_binary_distribution']
    for name, entry in lock['output_files'].items():
        assert audio.digest(output / name) == entry['sha256']
    manifest = json.loads((output / 'runtime-manifest.json').read_text(encoding='utf-8'))
    assert manifest['lock_sha256'] == audio.digest(path)
    assert set(manifest['files']) == audio.REQUIRED_FILES
    with pytest.raises(FileExistsError):
        audio.prepare(path, cache, output)


def test_missing_offline_archive_never_downloads(tmp_path, monkeypatch):
    path, cache, output, lock = fixture(tmp_path)
    (cache / lock['artifact']['filename']).unlink()
    def forbidden(*args, **kwargs):
        raise AssertionError('Network was not authorized.')
    monkeypatch.setattr(audio.urllib.request, 'build_opener', forbidden)
    with pytest.raises(FileNotFoundError, match='offline'):
        audio.prepare(path, cache, output)
    assert not output.exists()


def test_archive_and_selected_member_tampering_do_not_publish(tmp_path):
    path, cache, output, lock = fixture(tmp_path)
    lock['output_files']['bin/ffmpeg.exe']['sha256'] = '0' * 64
    path.write_text(json.dumps(lock), encoding='utf-8')
    with pytest.raises(ValueError, match='SHA256'):
        audio.prepare(path, cache, output)
    assert not output.exists() and not list(tmp_path.glob('.audio-runtime-*'))
    lock['artifact']['sha256'] = '1' * 64
    path.write_text(json.dumps(lock), encoding='utf-8')
    with pytest.raises(ValueError, match='archive differs'):
        audio.prepare(path, cache, output)


@pytest.mark.parametrize('bad', ['../outside', '/absolute', 'release/C:/escape', 'release\\escape'])
def test_archive_paths_cannot_escape_even_with_a_matching_archive_hash(tmp_path, bad):
    path, cache, output, lock = fixture(tmp_path, extra=((bad, b'evil'),))
    with zipfile.ZipFile(cache / lock['artifact']['filename']) as archive:
        assert bad in {info.orig_filename for info in archive.infolist()}
    with pytest.raises(ValueError, match='path'):
        audio.prepare(path, cache, output)
    assert not output.exists()


def test_case_collisions_are_rejected(tmp_path):
    path, cache, output, lock = fixture(tmp_path, extra=(('RELEASE/LICENSE', b'duplicate'),))
    with pytest.raises(ValueError, match='colliding'):
        audio.prepare(path, cache, output)


def test_archive_symlink_is_rejected(tmp_path):
    symlink = zipfile.ZipInfo('release/link')
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    path, cache, output, lock = fixture(tmp_path, extra=((symlink, b'/private'),))
    with pytest.raises(ValueError, match='link'):
        audio.prepare(path, cache, output)


def test_wrong_executable_architecture_is_rejected_before_publication(tmp_path):
    path, cache, output, lock = fixture(tmp_path, machine=0x14C)
    with pytest.raises(ValueError, match='AMD64'):
        audio.prepare(path, cache, output)
    assert not output.exists()


@pytest.mark.parametrize('url', ['http://github.com/archive', 'https://evil.invalid/archive',
    'https://name:secret@github.com/archive', 'https://github.com:444/archive',
    'file:///private/archive'])
def test_unsafe_download_hosts_and_redirects_are_rejected(url):
    with pytest.raises(ValueError):
        audio._url(url)


def test_private_profile_and_exact_manifest_are_required(tmp_path):
    path, cache, output, lock = fixture(tmp_path)
    lock['deployment']['public_binary_distribution'] = True
    path.write_text(json.dumps(lock), encoding='utf-8')
    with pytest.raises(ValueError, match='private'):
        audio.prepare(path, cache, output)


def test_directory_publication_never_replaces_existing_empty_directory(tmp_path):
    staged, output = tmp_path / 'staged', tmp_path / 'existing'
    staged.mkdir()
    output.mkdir()
    (staged / 'file').write_bytes(b'new')
    with pytest.raises(FileExistsError):
        audio._publish_directory(staged, output)
    assert output.is_dir() and not list(output.iterdir())
    assert (staged / 'file').read_bytes() == b'new'


def test_raw_zip_name_is_validated_before_platform_normalization(tmp_path, monkeypatch):
    path, cache, output, lock = fixture(tmp_path, extra=((r'release\escape', b'evil'),))
    original = zipfile.ZipFile.infolist
    def windows_members(archive):
        members = original(archive)
        for member in members:
            member.filename = member.orig_filename.replace(chr(92), '/')
        return members
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', windows_members)
    with pytest.raises(ValueError, match='path'):
        audio.prepare(path, cache, output)
    assert not output.exists()
