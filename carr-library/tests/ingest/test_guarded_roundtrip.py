"""Compose the existing revision bundle, storage and writer guard without new engines."""
from contextlib import closing
from hashlib import sha256
import subprocess
import sys

import pytest

from carr_library.ingest.bundle import create_revision_bundle, restore_revision_bundle
from carr_library.ingest.restore import RestoreError
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.storage.revisions import Page, Storage

CHILD = '''
import sys
from carr_library.ingest.writer_guard import exclusive_writer, WriterBusy
try:
    with exclusive_writer(sys.argv[1]):
        pass
except WriterBusy:
    sys.exit(7)
'''


def publish(storage, workspace, revision, raw, *, label=None):
    storage.publish_revision(workspace, 'same-edition', revision,
                             sha256(raw.encode()).hexdigest(), 'synthetic',
                             [Page(raw, 1, label, label is not None)])


def search(storage, workspace, term):
    return [tuple(r) for r in storage.db.execute(
        'SELECT p.revision,p.raw FROM page_fts JOIN active_page p ON p.id=page_fts.rowid '
        'WHERE page_fts MATCH ? AND p.workspace=? ORDER BY p.revision', (term, workspace))]


def test_guarded_backup_restore_preserves_revision_and_scope(tmp_path):
    root = tmp_path / 'Бібліотека with spaces'
    root.mkdir()
    source, restored, bundle = root / 'live.sqlite', root / 'copy.sqlite', root / 'backup'
    with exclusive_writer(root), closing(Storage(source)) as live:
        assert live.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        publish(live, 'A', 'a-old', 'Старий railway', label='iv')
        publish(live, 'A', 'a-new', 'Спільний railway')
        publish(live, 'B', 'b-new', 'Спільний railway')
        assert subprocess.run([sys.executable, '-c', CHILD, str(root)], timeout=10).returncode == 7
        manifest = create_revision_bundle(live, bundle, include_all_workspaces=True)
        publish(live, 'A', 'a-later', 'Later change')
    assert subprocess.run([sys.executable, '-c', CHILD, str(root)], timeout=10).returncode == 0
    with exclusive_writer(root):
        restore_revision_bundle(bundle, restored, expected_manifest_sha256=manifest)
        with closing(Storage(restored)) as copy:
            copy.check()
            assert search(copy, 'A', 'railway') == [('a-new', 'Спільний railway')]
            assert search(copy, 'B', 'railway') == [('b-new', 'Спільний railway')]
            old = copy.get_page('A', 'a-old', 1)
            assert (old['raw'], old['physical'], old['printed'], old['verified'], old['digest']) == (
                'Старий railway', 1, 'iv', 1, sha256('Старий railway'.encode()).hexdigest())
            assert copy.get_page('B', 'a-old', 1) is None
            assert copy.get_page('A', 'a-new', 1)['printed'] is None
    with exclusive_writer(root), closing(Storage(source)) as live:
        assert search(live, 'A', 'railway') == []
        assert search(live, 'A', 'Later') == [('a-later', 'Later change')]


def test_guarded_restore_no_clobber_and_lock_released(tmp_path):
    source, bundle, target = tmp_path / 'live.sqlite', tmp_path / 'backup', tmp_path / 'copy.sqlite'
    with exclusive_writer(tmp_path), closing(Storage(source)) as live:
        publish(live, 'A', 'a', 'Synthetic evidence')
        manifest = create_revision_bundle(live, bundle, include_all_workspaces=True)
    target.write_bytes(b'preserve existing file')
    with pytest.raises(RestoreError, match='already exists'):
        with exclusive_writer(tmp_path):
            restore_revision_bundle(bundle, target, expected_manifest_sha256=manifest)
    assert target.read_bytes() == b'preserve existing file'
    assert subprocess.run([sys.executable, '-c', CHILD, str(tmp_path)], timeout=10).returncode == 0


def test_guarded_backup_requires_explicit_all_scope(tmp_path):
    bundle = tmp_path / 'backup'
    with pytest.raises(RestoreError, match='consent'):
        with exclusive_writer(tmp_path), closing(Storage(tmp_path / 'live.sqlite')) as live:
            publish(live, 'A', 'a', 'Synthetic evidence')
            create_revision_bundle(live, bundle)
    assert not bundle.exists()
    assert subprocess.run([sys.executable, '-c', CHILD, str(tmp_path)], timeout=10).returncode == 0
