from contextlib import closing
from hashlib import sha256
import json

import pytest

from carr_library.ingest import bundle
from carr_library.ingest.restore import RestoreError
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def storage(tmp_path):
    with closing(Storage(tmp_path / 'live.sqlite')) as s:
        s.db.execute('PRAGMA journal_mode=WAL')
        for workspace, revision, raw in [('A', 'a1', 'Міст old'), ('B', 'b1', 'Міст other'),
                                         ('A', 'a2', 'Міст new')]:
            s.publish_revision(workspace, 'same-title', revision, 'a' * 64, revision,
                               [Page(raw, 3, 'iv', True)])
        yield s


def test_live_round_trip(storage, tmp_path):
    root, target = tmp_path / 'Бібліотека backup', tmp_path / 'fresh.sqlite'
    expected = bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    manifest = json.loads((root / bundle.MANIFEST).read_bytes())
    assert expected == sha256((root / bundle.MANIFEST).read_bytes()).hexdigest()
    assert manifest['sha256'] == sha256((root / bundle.SNAPSHOT).read_bytes()).hexdigest()
    assert bundle.restore_revision_bundle(root, target, expected_manifest_sha256=expected) == target
    with closing(Storage(target)) as restored:
        restored.check()
        old = restored.get_page('A', 'a1', 1)
        assert (old['raw'], old['physical'], old['printed'], old['verified']) == ('Міст old', 3, 'iv', 1)
        assert restored.get_page('B', 'a1', 1) is None
        assert [tuple(row) for row in restored.db.execute(
            'SELECT workspace,revision FROM active_page ORDER BY workspace')] == [('A', 'a2'), ('B', 'b1')]
        assert restored.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'Міст'").fetchone()[0] == 2
    assert (root / bundle.SNAPSHOT).read_bytes() == target.read_bytes()


def test_explicit_scope_and_no_clobber(storage, tmp_path):
    root = tmp_path / 'backup'
    with pytest.raises(RestoreError):
        bundle.create_revision_bundle(storage, root)
    assert not root.exists()
    expected = bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    with pytest.raises(FileExistsError):
        bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    target = tmp_path / 'existing'
    target.write_bytes(b'keep')
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256=expected)
    assert target.read_bytes() == b'keep'


@pytest.mark.parametrize('name', [bundle.MANIFEST, bundle.SNAPSHOT])
def test_tampering(storage, tmp_path, name):
    root, target = tmp_path / 'backup', tmp_path / 'fresh'
    expected = bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    path = root / name
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256=expected)
    assert not target.exists()


def test_manifest_last_on_interruption(storage, tmp_path, monkeypatch):
    root, target = tmp_path / 'backup', tmp_path / 'fresh'
    real_link = bundle.os.link
    def fail_manifest(src, dst):
        if dst == root / bundle.MANIFEST:
            raise OSError('synthetic interruption')
        return real_link(src, dst)
    monkeypatch.setattr(bundle.os, 'link', fail_manifest)
    with pytest.raises(OSError):
        bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    assert (root / bundle.SNAPSHOT).is_file()
    assert not (root / bundle.MANIFEST).exists()
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256='0' * 64)
    assert not target.exists()


def test_duplicate_manifest_keys(storage, tmp_path):
    root, target = tmp_path / 'backup', tmp_path / 'fresh'
    bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    path = root / bundle.MANIFEST
    raw = path.read_bytes().replace(b'{', b'{"version":1,', 1)
    path.write_bytes(raw)
    with pytest.raises(RestoreError, match='duplicate'):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256=sha256(raw).hexdigest())
    assert not target.exists()


def test_limits_and_unexpected_files(storage, tmp_path):
    root, target = tmp_path / 'backup', tmp_path / 'fresh'
    with pytest.raises(RestoreError):
        bundle.create_revision_bundle(storage, root, include_all_workspaces=True, max_bytes=1)
    assert not root.exists()
    expected = bundle.create_revision_bundle(storage, root, include_all_workspaces=True)
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256=expected, max_bytes=1)
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, root / 'fresh', expected_manifest_sha256=expected)
    (root / 'extra').write_bytes(b'extra')
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256=expected)
    assert not target.exists()
