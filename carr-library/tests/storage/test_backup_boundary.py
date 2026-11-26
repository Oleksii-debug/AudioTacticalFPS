"""CARR-SEC-004 regressions; all data is synthetic."""
from pathlib import Path

import pytest

from carr_library.storage import revisions
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def source(tmp_path):
    s = Storage(tmp_path / 'source.db')
    s.publish_revision('A', 'edition', 'a', 'a'*64, 'v1', [Page('synthetic A', 1)])
    try:
        yield s
    finally:
        s.close()


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_backup_rejects_existing_sidecar(source, tmp_path, suffix):
    target = tmp_path / 'backup.db'
    side = Path(str(target) + suffix)
    side.write_bytes(b'synthetic existing sidecar')
    with pytest.raises(FileExistsError):
        source.backup(target)
    assert not target.exists()
    assert side.read_bytes() == b'synthetic existing sidecar'


def test_foreign_wal_rejected(source, tmp_path):
    other = Storage(tmp_path / 'other.db')
    target = tmp_path / 'backup.db'
    side = Path(str(target) + '-wal')
    try:
        other.db.execute('PRAGMA journal_mode=WAL')
        other.db.execute('PRAGMA wal_autocheckpoint=0')
        other.publish_revision('B', 'edition', 'b', 'b'*64, 'v1', [Page('synthetic B', 1)])
        wal = (tmp_path / 'other.db-wal').read_bytes()
        side.write_bytes(wal)
        with pytest.raises(FileExistsError):
            source.backup(target)
        assert not target.exists()
        assert side.read_bytes() == wal
    finally:
        other.close()


def test_late_sidecar_rejected(source, tmp_path, monkeypatch):
    target = tmp_path / 'backup.db'
    side = Path(str(target) + '-journal')
    fsync = revisions.os.fsync

    def competing_fsync(fd):
        fsync(fd)
        side.write_bytes(b'late sidecar')

    monkeypatch.setattr(revisions.os, 'fsync', competing_fsync)
    with pytest.raises(FileExistsError):
        source.backup(target)
    assert not target.exists()
    assert side.read_bytes() == b'late sidecar'
    assert not list(tmp_path.glob('.carr-backup-*'))


def test_backup_live_wal_is_standalone(source, tmp_path):
    target = tmp_path / 'backup.db'
    source.db.execute('PRAGMA journal_mode=WAL')
    source.db.execute('PRAGMA wal_autocheckpoint=0')
    source.publish_revision('A', 'edition', 'a2', 'a'*64, 'v1', [Page('new A', 1)])
    source.backup(target)
    assert all(not Path(str(target) + s).exists() for s in ('-wal', '-shm', '-journal'))
    assert target.read_bytes()[18:20] == b'\x01\x01'  # rollback journal file format
    copy = Storage(target)
    try:
        copy.check()
        assert copy.get_page('A', 'a', 1)['raw'] == 'synthetic A'
        assert copy.get_page('A', 'a2', 1)['raw'] == 'new A'
        assert copy.get_page('B', 'b', 1) is None
    finally:
        copy.close()
