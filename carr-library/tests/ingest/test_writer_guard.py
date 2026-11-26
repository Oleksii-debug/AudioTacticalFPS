import os
import sqlite3
import subprocess
import sys
import time

import pytest

from carr_library.ingest.writer_guard import (
    LOCK_NAME, WriterBusy, WriterGuardError, exclusive_writer, open_ingest_session,
)

CHILD = '''
import sys
from carr_library.ingest.writer_guard import exclusive_writer, WriterBusy
try:
    with exclusive_writer(sys.argv[1]):
        pass
except WriterBusy:
    sys.exit(7)
'''


def test_process_contention_and_release(tmp_path):
    root = tmp_path / 'Бібліотека with spaces'
    root.mkdir()
    with exclusive_writer(root):
        with pytest.raises(WriterBusy):
            with exclusive_writer(root):
                pytest.fail('second writer entered')
        proc = subprocess.run([sys.executable, '-c', CHILD, str(root)], timeout=10)
        assert proc.returncode == 7
    assert subprocess.run([sys.executable, '-c', CHILD, str(root)], timeout=10).returncode == 0


def test_process_death_releases_hard_lock(tmp_path):
    ready = tmp_path / 'ready'
    code = '''
import sys, time
from pathlib import Path
from carr_library.ingest.writer_guard import exclusive_writer
with exclusive_writer(sys.argv[1]):
    Path(sys.argv[2]).touch()
    time.sleep(60)
'''
    proc = subprocess.Popen([sys.executable, '-c', code, str(tmp_path), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        with pytest.raises(WriterBusy):
            with exclusive_writer(tmp_path):
                pytest.fail('writer entered before death')
    finally:
        proc.kill()
        proc.wait(timeout=10)
    with exclusive_writer(tmp_path, timeout=2):
        pass


def test_nonempty_lock_preserved(tmp_path):
    marker = tmp_path / LOCK_NAME
    marker.write_bytes(b'not a lock')
    with pytest.raises(WriterGuardError):
        with exclusive_writer(tmp_path):
            pytest.fail('nonempty lock accepted')
    assert marker.read_bytes() == b'not a lock'


def test_hardlink_preserved(tmp_path):
    original = tmp_path / 'original'
    original.touch()
    os.link(original, tmp_path / LOCK_NAME)
    with pytest.raises(WriterGuardError):
        with exclusive_writer(tmp_path):
            pytest.fail('hardlink accepted')
    assert original.exists()
    assert original.stat().st_nlink == 2


def test_sqlite_writer_exception_releases_lock(tmp_path):
    db = tmp_path / 'test.sqlite'
    with pytest.raises(RuntimeError):
        with exclusive_writer(tmp_path):
            conn = sqlite3.connect(db)
            try:
                conn.execute('CREATE TABLE data(value TEXT)')
                conn.execute('INSERT INTO data VALUES(?)', ('Міст',))
                conn.commit()
                raise RuntimeError('simulated caller error')
            finally:
                conn.close()
    with exclusive_writer(tmp_path):
        conn = sqlite3.connect(db)
        try:
            assert conn.execute('SELECT value FROM data').fetchone() == ('Міст',)
        finally:
            conn.close()


@pytest.mark.parametrize('timeout', [True, -1, 31, float('nan'), 10**400])
def test_invalid_timeout(tmp_path, timeout):
    with pytest.raises(WriterGuardError):
        with exclusive_writer(tmp_path, timeout=timeout):
            pytest.fail('invalid timeout accepted')


def test_bad_database_names_rejected_before_import(tmp_path):
    with pytest.raises(WriterGuardError):
        with open_ingest_session(tmp_path, tmp_path / 'same.sqlite', tmp_path / 'SAME.sqlite'):
            pytest.fail('database aliases accepted')
    assert not (tmp_path / 'same.sqlite').exists()
    with pytest.raises(WriterGuardError):
        with open_ingest_session(tmp_path, tmp_path / 'one.sqlite', tmp_path / 'two.sqlite-wal'):
            pytest.fail('reserved name accepted')

    with pytest.raises(WriterGuardError):
        with open_ingest_session(tmp_path, tmp_path / 'CON.sqlite', tmp_path / 'jobs.sqlite'):
            pytest.fail('Windows device name accepted')


def test_real_ingest_session(tmp_path):
    # Requires the assembled pinned CARR-02/03/05/10 and PR17 dependency tree.
    storage, journal = tmp_path / 'storage.sqlite', tmp_path / 'jobs.sqlite'
    with open_ingest_session(tmp_path, storage, journal) as ingest:
        assert ingest.db.execute('SELECT count(*) FROM job').fetchone()[0] == 0
        assert ingest.storage.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    with open_ingest_session(tmp_path, storage, journal) as ingest:
        assert ingest.db.execute('SELECT count(*) FROM job').fetchone()[0] == 0
