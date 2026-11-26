"""Real V2 catalog/revision composition and local backup compatibility."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.ingest import bundle
from carr_library.ingest.restore import RestoreError
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def library(tmp_path):
    with closing(Storage(tmp_path / 'library.sqlite', schema_version=2)) as s:
        s.db.execute('PRAGMA journal_mode=WAL')
        with s.transaction():
            for ws in ('A', 'B'):
                s.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                Catalog(s.db, ws).create_collection('research', 'Research')
        yield s


def record(author):
    source = 'Міст — archive'.encode('utf-8')
    return Registration('work', 'edition',
                        dict(sha256=sha256(source).hexdigest(), mime='text/plain', size=len(source)),
                        title='Same title', authors=(author,), language='uk', collections=('research',))


def publish(s, ws, rev, text):
    r = record('Author ' + ws)
    with s.transaction():
        register_batch(Catalog(s.db, ws), (r,))
        s.stage_revision(ws, r.edition_id, rev, r.manifest['sha256'], 'synthetic', [Page(text)])
    s.check()


def test_v2_roundtrip(library, tmp_path):
    publish(library, 'A', 'a1', 'Міст old')
    publish(library, 'B', 'b1', 'Міст other')
    publish(library, 'A', 'a2', 'Міст new')
    root = tmp_path / 'backup'
    manifest_hash = bundle.create_revision_bundle(library, root, include_all_workspaces=True)
    assert json.loads((root / bundle.MANIFEST).read_bytes())['schema_version'] == 2
    target = tmp_path / 'restored.sqlite'
    bundle.restore_revision_bundle(root, target, expected_manifest_sha256=manifest_hash)
    assert target.read_bytes() == (root / bundle.SNAPSHOT).read_bytes()
    with closing(Storage(target, schema_version=2)) as s:
        s.check()
        assert s.get_page('A', 'a1', 1)['raw'] == 'Міст old'
        assert s.get_page('B', 'a1', 1) is None
        assert Catalog(s.db, 'A').editions('research') == Catalog(s.db, 'B').editions('research')
        assert [tuple(r) for r in s.db.execute('SELECT workspace,name FROM catalog_author ORDER BY workspace')] == [('A', 'Author A'), ('B', 'Author B')]
        assert s.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1
        assert [tuple(r) for r in s.db.execute("SELECT a.workspace,a.raw FROM page_fts JOIN active_page a ON a.id=page_fts.rowid WHERE page_fts MATCH 'Міст' ORDER BY a.workspace")] == [('A', 'Міст new'), ('B', 'Міст other')]


@pytest.mark.parametrize('version', [1, 3, True, '2'])
def test_bad_schema_version(library, tmp_path, version):
    publish(library, 'A', 'a1', 'Міст old')
    root, target = tmp_path / 'backup', tmp_path / 'restored.sqlite'
    bundle.create_revision_bundle(library, root, include_all_workspaces=True)
    path = root / bundle.MANIFEST
    manifest = json.loads(path.read_bytes())
    manifest['schema_version'] = version
    raw = json.dumps(manifest).encode('utf-8')
    path.write_bytes(raw)
    with pytest.raises(RestoreError):
        bundle.restore_revision_bundle(root, target, expected_manifest_sha256=sha256(raw).hexdigest())
    assert not target.exists()


def test_atomic_catalog_revision_rollback_and_retry(library, tmp_path):
    r = record('Author A')
    with pytest.raises(ValueError, match='invalid raw page'):
        with library.transaction():
            register_batch(Catalog(library.db, 'A'), (r,))
            library.stage_revision('A', 'edition', 'a1', r.manifest['sha256'], 'synthetic', [Page('valid'), Page('bad\x00')])
    assert Catalog(library.db, 'A').editions() == ()
    assert library.get_page('A', 'a1', 1) is None
    assert library.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 0
    with library.transaction():
        register_batch(Catalog(library.db, 'A'), (r,))
        library.stage_revision('A', 'edition', 'a1', r.manifest['sha256'], 'synthetic', [Page('Міст')])
        with closing(sqlite3.connect(tmp_path / 'library.sqlite')) as reader:
            assert reader.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 0
            assert reader.execute('SELECT count(*) FROM active_page').fetchone()[0] == 0
    library.check()
    root, target = tmp_path / 'backup', tmp_path / 'restored.sqlite'
    token = bundle.create_revision_bundle(library, root, include_all_workspaces=True)
    bundle.restore_revision_bundle(root, target, expected_manifest_sha256=token)
    with closing(Storage(target, schema_version=2)) as restored:
        assert restored.get_page('A', 'a1', 1)['raw'] == 'Міст'
        assert restored.db.execute('SELECT count(*) FROM catalog_membership').fetchone()[0] == 1
        restored.check()


def test_legacy_v1_roundtrip(tmp_path):
    root, target = tmp_path / 'backup', tmp_path / 'restored.sqlite'
    with closing(Storage(tmp_path / 'legacy.sqlite', schema_version=1)) as s:
        s.publish_revision('A', 'edition', 'r', 'a'*64, 'v1', [Page('Original', 1, 'iv', True)])
        token = bundle.create_revision_bundle(s, root, include_all_workspaces=True)
    assert json.loads((root / bundle.MANIFEST).read_bytes())['schema_version'] == 1
    bundle.restore_revision_bundle(root, target, expected_manifest_sha256=token)
    with closing(Storage(target, schema_version=1)) as s:
        assert s.get_page('A', 'r', 1)['printed'] == 'iv'
        s.check()
