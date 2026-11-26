import os
import sqlite3

import pytest

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from carr_library.ingest.restore import RestoreError, restore_revision_snapshot
from carr_library.storage.revisions import Page, Storage


def manifest(path):
    return dict(version=1, kind='revision-storage', schema_version=1,
                size_bytes=path.stat().st_size, sha256=hash_file(path))


@pytest.fixture
def snapshot(tmp_path):
    src = Storage(tmp_path / 'live.sqlite')
    try:
        src.publish_revision('A', 'edition', 'old', 'a'*64, 'v1', [Page('Старий — текст', 1, 'iv', True)])
        src.publish_revision('A', 'edition', 'new', 'b'*64, 'v1', [Page('New passage', 1)])
        src.publish_revision('B', 'edition', 'other', 'c'*64, 'v1', [Page('New passage', 1)])
        p = tmp_path / 'backup.sqlite'
        src.backup(p)
    finally:
        src.close()
    return p


def test_restore_exact_and_isolated(snapshot, tmp_path):
    target = tmp_path / 'restored.sqlite'
    m = manifest(snapshot)
    assert restore_revision_snapshot(snapshot, target, m) == target
    assert hash_file(target) == hash_file(snapshot) == m['sha256']
    restored = Storage(target)
    try:
        restored.check()
        row = restored.get_page('A', 'old', 1)
        assert (row['raw'], row['printed'], row['verified']) == ('Старий — текст', 'iv', 1)
        assert restored.get_page('B', 'old', 1) is None
        assert restored.db.execute('SELECT revision FROM active WHERE workspace=?', ('A',)).fetchone()[0] == 'new'
    finally:
        restored.close()


@pytest.mark.parametrize('field,value', [('version', 2), ('schema_version', True), ('sha256', '0'*64), ('size_bytes', 1)])
def test_bad_manifest(snapshot, tmp_path, field, value):
    m = manifest(snapshot)
    m[field] = value
    target = tmp_path / 'restore.sqlite'
    with pytest.raises(RestoreError):
        restore_revision_snapshot(snapshot, target, m)
    assert not target.exists()


def test_limit(snapshot, tmp_path):
    with pytest.raises(RestoreError):
        restore_revision_snapshot(snapshot, tmp_path / 'new', manifest(snapshot), max_bytes=32)


def test_no_overwrite(snapshot, tmp_path):
    target = tmp_path / 'existing'
    target.write_bytes(b'keep me')
    with pytest.raises(RestoreError):
        restore_revision_snapshot(snapshot, target, manifest(snapshot))
    assert target.read_bytes() == b'keep me'


def test_sidecar(snapshot, tmp_path):
    target = tmp_path / 'restored'
    sidecar = tmp_path / 'restored-wal'
    sidecar.write_bytes(b'keep')
    with pytest.raises(RestoreError):
        restore_revision_snapshot(snapshot, target, manifest(snapshot))
    assert not target.exists() and sidecar.read_bytes() == b'keep'
    (tmp_path / 'backup.sqlite-wal').write_bytes(b'pending')
    with pytest.raises(RestoreError):
        restore_revision_snapshot(snapshot, tmp_path / 'new', manifest(snapshot))


def test_wrong_schema(snapshot, tmp_path):
    bad = tmp_path / 'wrong.sqlite'
    db = sqlite3.connect(bad)
    try:
        db.execute('CREATE TABLE unrelated(x)')
        db.execute('PRAGMA user_version=1')
    finally:
        db.close()
    target = tmp_path / 'restored'
    with pytest.raises(RestoreError, match='schema'):
        restore_revision_snapshot(bad, target, manifest(bad))
    assert not target.exists()


def test_corrupt_digest_with_updated_manifest(snapshot, tmp_path):
    db = sqlite3.connect(snapshot)
    try:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        db.execute('DROP TRIGGER page_bu')
        db.execute("UPDATE page SET raw='tampered' WHERE revision='old'")
        db.execute(sql)
        db.commit()
    finally:
        db.close()
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='raw digest mismatch'):
        restore_revision_snapshot(snapshot, target, manifest(snapshot))
    assert not target.exists()


def test_race_no_clobber(snapshot, tmp_path, monkeypatch):
    target = tmp_path / 'restored'
    real = os.link
    def race(src, dst):
        target.write_bytes(b'concurrent owner')
        return real(src, dst)
    monkeypatch.setattr(os, 'link', race)
    with pytest.raises(FileExistsError):
        restore_revision_snapshot(snapshot, target, manifest(snapshot))
    assert target.read_bytes() == b'concurrent owner'
