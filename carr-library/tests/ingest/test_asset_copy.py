from hashlib import sha256
import sqlite3

import pytest

from carr_library.ingest.asset_copy import copy_revision_assets, verify_revision_assets
from carr_library.ingest.retained_assets import RetainedAssets, RetentionError


@pytest.fixture
def fixture(tmp_path):
    # Real SQLite, minimal relevant PR21 schema; not full Storage validation.
    db = sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE revision(id TEXT PRIMARY KEY, workspace TEXT, asset TEXT);'
                     'CREATE TABLE sealed(revision TEXT PRIMARY KEY);'
                     'CREATE TABLE active(workspace TEXT, revision TEXT);')
    root = tmp_path / 'originals'
    root.mkdir()
    store = RetainedAssets(root)
    old = store.retain(b'old edition', 'text/plain', sha256(b'old edition').hexdigest())
    new = store.retain('нова редакція'.encode(), 'text/plain', sha256('нова редакція'.encode()).hexdigest())
    orphan = store.retain(b'orphan', 'text/plain', sha256(b'orphan').hexdigest())
    db.executemany('INSERT INTO revision VALUES(?,?,?)', [('r1','A',old.stem),('r2','A',new.stem),('r3','B',new.stem)])
    db.executemany('INSERT INTO sealed VALUES(?)', [('r1',),('r2',),('r3',)])
    db.executemany('INSERT INTO active VALUES(?,?)', [('A','r2'),('B','r3')])
    try:
        yield db, store, old, new, orphan
    finally:
        db.close()


def test_historical_and_deduplicated_copy(fixture, tmp_path):
    db, source, old, new, orphan = fixture
    dst = tmp_path / 'copy'
    assert copy_revision_assets(db, source, dst, include_all_workspaces=True) == 2
    assert set(p.name for p in dst.iterdir()) == {old.name, new.name}
    assert not (dst / orphan.name).exists()
    assert (dst / old.name).read_bytes() == old.read_bytes()
    assert not (dst / old.name).samefile(old)
    old.unlink()
    assert verify_revision_assets(db, RetainedAssets(dst), include_all_workspaces=True) == 2
    restored = tmp_path / 'restored'
    assert copy_revision_assets(db, RetainedAssets(dst), restored, include_all_workspaces=True) == 2
    assert (restored / old.name).read_bytes() == b'old edition'


def test_no_consent_no_copy(fixture, tmp_path):
    db, source, *_ = fixture
    dst = tmp_path / 'copy'
    with pytest.raises(RetentionError, match='consent'):
        copy_revision_assets(db, source, dst)
    assert not dst.exists()


@pytest.mark.parametrize('failure', ['missing','corrupt','unsealed','count','bytes'])
def test_preflight(fixture, tmp_path, failure):
    db, source, old, *_ = fixture
    kwargs = {}
    if failure == 'missing': old.unlink()
    elif failure == 'corrupt': old.write_bytes(b'changed')
    elif failure == 'unsealed': db.execute('DELETE FROM sealed WHERE revision=?', ('r1',))
    elif failure == 'count': kwargs['max_assets'] = 1
    elif failure == 'bytes': kwargs['max_bytes'] = 1
    dst = tmp_path / 'copy'
    with pytest.raises(RetentionError):
        copy_revision_assets(db, source, dst, include_all_workspaces=True, **kwargs)
    assert not dst.exists()


def test_no_clobber_or_overlap(fixture, tmp_path):
    db, source, *_ = fixture
    with pytest.raises(FileExistsError):
        copy_revision_assets(db, source, tmp_path, include_all_workspaces=True)
    with pytest.raises(RetentionError, match='overlaps'):
        copy_revision_assets(db, source, source.root / 'copy', include_all_workspaces=True)


def test_incomplete_or_unexpected_copy_rejected(fixture, tmp_path):
    db, source, old, *_ = fixture
    dst = tmp_path / 'copy'
    copy_revision_assets(db, source, dst, include_all_workspaces=True)
    (dst / 'unexpected').write_text('unexpected')
    with pytest.raises(RetentionError, match='namespace'):
        verify_revision_assets(db, RetainedAssets(dst), include_all_workspaces=True)
    (dst / 'unexpected').unlink()
    (dst / old.name).unlink()
    with pytest.raises(RetentionError, match='missing'):
        verify_revision_assets(db, RetainedAssets(dst), include_all_workspaces=True)
