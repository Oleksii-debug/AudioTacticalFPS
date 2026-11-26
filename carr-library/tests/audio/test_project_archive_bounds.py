"""Reject malformed central directories before allocating ZipInfo objects."""
import struct
import zipfile

import pytest

from carr_library.audio import backup
from carr_library.audio.jobs import RenderCancelled
from carr_library.ui.audio_studio import StudioController


def make_snapshot(tmp_path):
    controller = StudioController(private_root=tmp_path / 'private')
    controller.new('Synthetic archive fixture.')
    project = tmp_path / 'studio.json'
    controller.save(project)
    archive = tmp_path / 'snapshot.zip'
    backup.backup_project(project, archive)
    return archive


@pytest.mark.parametrize('damage', ['underreported_count', 'overreported_count', 'directory_budget',
                                   'multiple_disks', 'oversized_extra', 'comment'])
def test_bad_central_directory_is_refused_before_zipfile(tmp_path, monkeypatch, damage):
    archive = make_snapshot(tmp_path)
    raw = bytearray(archive.read_bytes())
    end = len(raw) - 22
    record = list(struct.unpack('<4s4H2LH', raw[end:]))
    if damage == 'underreported_count':
        record[3] = record[4] = 1
    elif damage == 'overreported_count':
        record[3] = record[4] = 3
    elif damage == 'directory_budget':
        record[5] = backup.CENTRAL_DIRECTORY_BUDGET + 1
    elif damage == 'multiple_disks':
        record[1] = 1
    elif damage == 'oversized_extra':
        struct.pack_into('<H', raw, record[6] + 30, 65535)
    else:
        record[7] = 1
    raw[end:] = struct.pack('<4s4H2LH', *record)
    archive.write_bytes(raw)

    def forbidden_allocation(*args, **kwargs):
        raise AssertionError('ZipFile must not see an invalid central directory.')

    monkeypatch.setattr(backup.zipfile, 'ZipFile', forbidden_allocation)
    with pytest.raises(ValueError, match='snapshot|ZIP64'):
        backup.restore_project(archive, tmp_path / 'unpublished')
    assert not (tmp_path / 'unpublished').exists()


def test_preflight_cancellation_occurs_before_zipfile_allocation(tmp_path, monkeypatch):
    archive = make_snapshot(tmp_path)
    calls = []

    def cancelled():
        calls.append(True)
        return len(calls) >= 3

    def forbidden_allocation(*args, **kwargs):
        raise AssertionError('Cancelled preflight cannot allocate ZipInfo objects.')

    monkeypatch.setattr(backup.zipfile, 'ZipFile', forbidden_allocation)
    with pytest.raises(RenderCancelled):
        backup.restore_project(archive, tmp_path / 'unpublished', cancelled=cancelled)
    assert not (tmp_path / 'unpublished').exists()


def test_actual_zip64_end_records_roundtrip(tmp_path, monkeypatch):
    # The real Python writer emits ZIP64 records for a small synthetic payload.
    # No sparse file or fabricated multi-gigabyte data is claimed.
    with monkeypatch.context() as patch:
        patch.setattr(zipfile, 'ZIP64_LIMIT', 0)
        archive = make_snapshot(tmp_path)
    assert b'PK\x06\x06' in archive.read_bytes()
    result = backup.restore_project(archive, tmp_path / 'restored')
    controller = StudioController(private_root=tmp_path / 'restored-private')
    controller.open(result['project_file'])
    assert controller.script == 'Synthetic archive fixture.'

