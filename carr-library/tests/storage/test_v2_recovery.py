"""ADR61 V2 transaction/recovery gate, real disk SQLite, synthetic data only.

Reuse PR21's V1 process-exit/sidecar test pattern on the combined V2 schema.
No application catalog or ingest API is mocked: direct SQL seeds only the
catalog rows required by the storage contract. Full ingest is a separate gate.
"""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

import pytest

from carr_library.storage.revisions import Page, Storage


def register(s, ws, edition, asset):
    db = s.db
    db.execute('INSERT INTO catalog_asset VALUES(?,?,?) ON CONFLICT DO NOTHING',
               (asset, 'text/plain', 3))
    db.execute('INSERT INTO catalog_work VALUES(?,?,?)', (ws, edition, 'Same title'))
    db.execute('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)',
               (ws, edition, edition, asset, 'uk', None, None))
    db.execute('INSERT INTO catalog_membership VALUES(?,?,?)', (ws, 'c', edition))


def seed(path, mode):
    with closing(Storage(path, schema_version=2)) as s:
        assert s.db.execute('PRAGMA journal_mode=' + mode).fetchone()[0] == mode.lower()
        with s.transaction():
            for ws in ('a', 'b'):
                s.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                s.db.execute('INSERT INTO catalog_collection VALUES(?,?,?,0)', (ws, 'c', 'Research'))
                register(s, ws, 'e', ws * 64)
                s.stage_revision(ws, 'e', ws + '1', ws * 64, 'fixture',
                                 [Page('oldtoken', 3, 'iv', True)])


def stage(s):
    register(s, 'a', 'new', 'c' * 64)
    s.stage_revision('a', 'new', 'new1', 'c' * 64, 'fixture',
                     [Page('addedtoken ' + 'x ' * 10000) for _ in range(16)])
    s.stage_revision('a', 'e', 'a2', 'a' * 64, 'fixture', [Page('updatedtoken', 3, 'iv', True)])
    s.db.execute("DELETE FROM catalog_membership WHERE workspace='a' AND edition='e'")


def hits(s, term, ws):
    return [r[0] for r in s.db.execute(
        'SELECT p.revision FROM page_fts JOIN active_page p ON p.id=page_fts.rowid '
        'WHERE page_fts MATCH ? AND p.workspace=? ORDER BY p.ordinal', (term, ws))]


def assert_state(s, committed):
    s.check()
    assert s.get_page('a', 'a1', 1)['raw'] == 'oldtoken'
    assert s.get_page('a', 'a1', 1)['printed'] == 'iv'
    assert s.get_page('b', 'a1', 1) is None
    assert hits(s, 'oldtoken', 'b') == ['b1']
    assert hits(s, 'updatedtoken', 'b') == []
    assert hits(s, 'oldtoken', 'a') == ([] if committed else ['a1'])
    assert hits(s, 'updatedtoken', 'a') == (['a2'] if committed else [])
    assert hits(s, 'addedtoken', 'a') == (['new1'] * 16 if committed else [])
    assert (s.get_page('a', 'new1', 1) is not None) == committed
    assert (s.get_page('a', 'a2', 1) is not None) == committed
    for table, col, value in [('catalog_work', 'id', 'new'), ('catalog_edition', 'id', 'new'),
                              ('catalog_asset', 'digest', 'c' * 64),
                              ('catalog_membership', 'edition', 'new')]:
        assert s.db.execute(f'SELECT count(*) FROM {table} WHERE {col}=?', (value,)).fetchone()[0] == int(committed)
    assert s.db.execute("SELECT count(*) FROM catalog_membership WHERE workspace='a' AND edition='e'").fetchone()[0] == int(not committed)


def child(*args):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), *map(str, args)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)


@pytest.mark.parametrize('mode', ['DELETE', 'WAL'])
@pytest.mark.parametrize('committed', [False, True])
def test_process_exit_combined_publication(tmp_path, mode, committed):
    path = tmp_path / 'library.db'
    seed(path, mode)
    child('publish', path, mode, int(committed))
    with closing(Storage(path, schema_version=2)) as s:
        assert_state(s, committed)
        if not committed:
            with s.transaction():
                stage(s)
            assert_state(s, True)
        with pytest.raises(sqlite3.IntegrityError):
            s.publish_revision('a', 'e', 'bad', 'b' * 64, 'fixture', [Page('wrong')])
        assert s.get_page('a', 'bad', 1) is None
        out = tmp_path / 'snapshot.db'
        s.backup(out)
        assert out.read_bytes()[18:20] == b'\x01\x01'
        assert all(not Path(str(out) + suffix).exists() for suffix in ('-wal', '-shm', '-journal'))
    with closing(Storage(out, schema_version=2)) as restored:
        assert_state(restored, True)


def test_engine_rollback_does_not_mask_original_failure(tmp_path):
    path = tmp_path / 'library.db'
    seed(path, 'DELETE')
    with closing(Storage(path, schema_version=2)) as s:
        def interrupted_pages():
            yield Page('partialtoken')
            # Native ON CONFLICT ROLLBACK ends the whole outer transaction.
            s.db.execute("INSERT OR ROLLBACK INTO workspace VALUES('a')")
        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE constraint failed: workspace.id'):
            with s.transaction():
                register(s, 'a', 'new', 'c' * 64)
                s.stage_revision('a', 'new', 'new1', 'c' * 64, 'fixture', interrupted_pages())
        assert not s.db.in_transaction
        assert_state(s, False)
        with s.transaction():
            stage(s)
        assert_state(s, True)


def test_v2_foreign_wal_and_retry_snapshot(tmp_path):
    path, foreign, target = [tmp_path / name for name in ('library.db', 'foreign.db', 'snapshot.db')]
    seed(path, 'WAL')
    seed(foreign, 'WAL')
    with closing(Storage(path, schema_version=2)) as s, closing(Storage(foreign, schema_version=2)) as other:
        other.db.execute('PRAGMA wal_autocheckpoint=0')
        other.publish_revision('b', 'e', 'foreign', 'b' * 64, 'fixture', [Page('foreigntoken')])
        wal = Path(str(foreign) + '-wal').read_bytes()
        side = Path(str(target) + '-wal')
        side.write_bytes(wal)
        with pytest.raises(FileExistsError):
            s.backup(target)
        assert not target.exists()
        assert side.read_bytes() == wal
        side.unlink()  # test owns this synthetic sidecar
        s.backup(target)
    with closing(Storage(target, schema_version=2)) as restored:
        assert_state(restored, False)
        assert restored.get_page('b', 'foreign', 1) is None
        assert hits(restored, 'foreigntoken', 'b') == []


def test_process_exit_before_v2_backup_publication(tmp_path):
    path, target = tmp_path / 'library.db', tmp_path / 'snapshot.db'
    seed(path, 'WAL')
    child('backup', path, target)
    assert not target.exists()
    with closing(Storage(path, schema_version=2)) as s:
        assert_state(s, False)
        s.backup(target)
    with closing(Storage(target, schema_version=2)) as restored:
        assert_state(restored, False)


if __name__ == '__main__':
    operation, path, arg, *rest = sys.argv[1:]
    with closing(Storage(path, schema_version=2)) as s:
        if operation == 'publish':
            assert s.db.execute('PRAGMA journal_mode=' + arg).fetchone()[0] == arg.lower()
            s.db.execute('PRAGMA cache_size=8')
            with s.transaction():
                stage(s)
                if rest == ['0']:
                    os._exit(73)
            os._exit(73)
        if operation == 'backup':
            # Real SQLite copy/check/fsync run; terminate before final hard link.
            from carr_library.storage import revisions
            revisions.os.link = lambda *args: os._exit(73)
            s.backup(arg)
    raise AssertionError('child did not reach crash checkpoint')
