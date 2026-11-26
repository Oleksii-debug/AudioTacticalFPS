import os
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.ingest.catalog_asset_copy import (
    catalog_inventory, copy_catalog_assets, verify_catalog_assets,
)
from carr_library.ingest.retained_assets import RetainedAssets, RetentionError

CONSENT = dict(include_all_workspaces=True)


@pytest.fixture
def state(tmp_path):
    db = sqlite3.connect(tmp_path / 'live.db')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
    db.executescript((Path(__file__).parent.parent / 'catalog' /
                      'catalog_membership_candidate.sql').read_text(encoding='utf-8'))
    # Minimal revision boundary only; not a production Storage migration.
    db.executescript('CREATE TABLE revision(id TEXT PRIMARY KEY,workspace TEXT,edition TEXT,asset TEXT);'
                     'CREATE TABLE sealed(revision TEXT PRIMARY KEY REFERENCES revision(id));')
    root = tmp_path / 'originals'; root.mkdir()
    source = RetainedAssets(root)
    db.executemany('INSERT INTO workspace VALUES(?)', [('a',), ('b',)])
    db.executemany('INSERT INTO catalog_work VALUES(?,?,?)', [('a','w','Same'), ('b','w','Same')])

    def add(data, edition, *, workspace='a', revision=None, mime='text/plain'):
        h = sha256(data).hexdigest()
        p = source.retain(data, mime, h)
        db.execute('INSERT OR IGNORE INTO catalog_asset VALUES(?,?,?)', (h,mime,len(data)))
        db.execute('INSERT INTO catalog_edition(workspace,id,work,asset) VALUES(?,?,?,?)',
                   (workspace,edition,'w',h))
        if revision:
            db.execute('INSERT INTO revision VALUES(?,?,?,?)', (revision,workspace,edition,h))
            db.execute('INSERT INTO sealed VALUES(?)', (revision,))
        db.commit()
        return p

    old = add('Історичний текст\r\n'.encode(), 'old', revision='r1')
    newer = add('Druhé vydanie'.encode(), 'new', revision='r2')
    add(old.read_bytes(), 'old', workspace='b', revision='r3')
    pending = add(b'not indexed yet', 'pending')
    snap = sqlite3.connect(tmp_path / 'snapshot.db')
    db.backup(snap)
    try:
        yield db, snap, source, old, newer, pending, add
    finally:
        snap.close(); db.close()


def test_catalog_only_and_duplicate_workspaces(state, tmp_path):
    db,snap,source,old,newer,pending,add = state
    later = add(b'after snapshot', 'later')
    dst = tmp_path / 'copy'
    entries = copy_catalog_assets(snap, source, dst, **CONSENT)
    assert len(entries) == 3
    assert (dst / pending.name).read_bytes() == pending.read_bytes()
    assert (dst / old.name).read_bytes() == old.read_bytes()
    assert not (dst / later.name).exists()
    assert verify_catalog_assets(snap, RetainedAssets(dst), **CONSENT) == entries
    # Restore to independent storage, not a hardlink back to source.
    restored = tmp_path / 'restore'
    assert copy_catalog_assets(snap, RetainedAssets(dst), restored, **CONSENT) == entries
    assert not os.path.samefile(old, restored / old.name)
    assert (restored / newer.name).read_bytes() == newer.read_bytes()


def test_mime_drift_rejected(state, tmp_path):
    _,snap,source,old,*_ = state
    dst = tmp_path / 'copy'
    copy_catalog_assets(snap, source, dst, **CONSENT)
    (dst / old.name).rename(dst / (old.stem + '.pdf'))
    with pytest.raises(RetentionError):
        verify_catalog_assets(snap, RetainedAssets(dst), **CONSENT)
    old.rename(old.with_suffix('.pdf'))
    with pytest.raises(RetentionError, match='MIME or size'):
        copy_catalog_assets(snap, source, tmp_path / 'bad-source', **CONSENT)


def test_size_drift_rejected(state, tmp_path):
    _,snap,source,old,*_ = state
    # Corrupt synthetic DB metadata, not the retained bytes.
    snap.execute('DROP TRIGGER catalog_asset_no_update')
    snap.execute('UPDATE catalog_asset SET size=size+1 WHERE digest=?', (old.stem,))
    with pytest.raises(RetentionError, match='MIME or size'):
        copy_catalog_assets(snap, source, tmp_path / 'copy', **CONSENT)


def test_wrong_edition_binding(state):
    _,snap,_,old,newer,*_ = state
    snap.execute('UPDATE revision SET asset=? WHERE id=?', (newer.stem, 'r1'))
    with pytest.raises(RetentionError, match='unbound revision'):
        catalog_inventory(snap, **CONSENT)


def test_unsealed_revision(state):
    _,snap,*_ = state
    snap.execute("DELETE FROM sealed WHERE revision='r1'")
    with pytest.raises(RetentionError, match='unsealed'):
        catalog_inventory(snap, **CONSENT)


def test_missing_catalog_original(state, tmp_path):
    _,snap,source,_,_,pending,_ = state
    pending.unlink()
    dst = tmp_path / 'copy'
    with pytest.raises(RetentionError, match='missing'):
        copy_catalog_assets(snap, source, dst, **CONSENT)
    assert not dst.exists()


def test_unexpected_or_corrupt_output(state, tmp_path):
    _,snap,source,old,*_ = state
    dst = tmp_path / 'copy'
    copy_catalog_assets(snap, source, dst, **CONSENT)
    extra = dst / 'extra'; extra.write_bytes(b'unrelated')
    with pytest.raises(RetentionError, match='namespace'):
        verify_catalog_assets(snap, RetainedAssets(dst), **CONSENT)
    extra.unlink()
    (dst / old.name).write_bytes(b'x' * old.stat().st_size)
    with pytest.raises(RetentionError, match='digest mismatch'):
        verify_catalog_assets(snap, RetainedAssets(dst), **CONSENT)


def test_consent_limits_no_clobber(state, tmp_path):
    _,snap,source,*_ = state
    dst = tmp_path / 'copy'
    with pytest.raises(RetentionError, match='consent'):
        copy_catalog_assets(snap, source, dst)
    with pytest.raises(RetentionError, match='count limit'):
        copy_catalog_assets(snap, source, dst, max_assets=2, **CONSENT)
    with pytest.raises(RetentionError, match='byte limit'):
        copy_catalog_assets(snap, source, dst, max_bytes=1, **CONSENT)
    copy_catalog_assets(snap, source, dst, **CONSENT)
    with pytest.raises(FileExistsError):
        copy_catalog_assets(snap, source, dst, **CONSENT)
    with pytest.raises(RetentionError, match='overlaps'):
        copy_catalog_assets(snap, source, source.root / 'nested', **CONSENT)
