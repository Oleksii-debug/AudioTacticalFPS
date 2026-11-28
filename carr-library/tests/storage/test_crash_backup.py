"""Real child-process termination tests for native SQLite persistence."""
import subprocess
import sys

import pytest

from carr_library.storage.revisions import Page, Storage

ASSET = 'a' * 64


def seed(path):
    s = Storage(path)
    s.publish_revision('a', 'edition', 'old', ASSET, 'fixture',
                       [Page('Міст old', 1, 'iv', True)])
    s.close()


def child(code, *paths):
    p = subprocess.run([sys.executable, '-B', '-c', code, *map(str, paths)],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 73, (p.returncode, p.stdout, p.stderr)


def hits(s, term):
    return [r[0] for r in s.db.execute(
        'SELECT p.raw FROM page_fts JOIN active_page p ON p.id=page_fts.rowid '
        'WHERE page_fts MATCH ? AND p.workspace=?', (term, 'a'))]


def test_process_exit_before_revision_commit(tmp_path):
    path = tmp_path / 'library.db'
    seed(path)
    child('''
import os, sys
from carr_library.storage.revisions import Storage, Page
s = Storage(sys.argv[1])
s.db.set_trace_callback(lambda sql: os._exit(73) if sql.strip().upper() == 'COMMIT' else None)
s.publish_revision('a', 'edition', 'new', 'b'*64, 'fixture', [Page('new text')])
''', path)
    s = Storage(path)
    try:
        s.check()
        assert hits(s, 'old') == ['Міст old']
        assert hits(s, 'new') == []
        assert s.get_page('a', 'new', 1) is None
        assert s.get_page('a', 'old', 1)['printed'] == 'iv'
    finally:
        s.close()


def test_process_exit_before_backup_copy(tmp_path):
    source, target = tmp_path / 'library.db', tmp_path / 'backup.db'
    seed(source)
    child('''
import os, sys
from carr_library.storage import revisions
s = revisions.Storage(sys.argv[1])
def crash(*args, **kwargs):
    os._exit(73)
revisions.sqlite3.connect = crash
s.backup(sys.argv[2])
''', source, target)
    assert not target.exists(), 'interrupted backup exposed a final destination'
    s = Storage(source)
    try:
        s.check()
        assert hits(s, 'old') == ['Міст old']
        s.backup(target)
    finally:
        s.close()
    copy = Storage(target)
    try:
        copy.check()
        assert hits(copy, 'old') == ['Міст old']
    finally:
        copy.close()


def test_backup_history_and_no_clobber(tmp_path):
    source, target = tmp_path / 'library.db', tmp_path / 'backup.db'
    seed(source)
    s = Storage(source)
    try:
        s.publish_revision('a', 'edition', 'new', ASSET, 'fixture', [Page('новий new')])
        s.backup(target)
        original = target.read_bytes()
        with pytest.raises(FileExistsError):
            s.backup(target)
        assert target.read_bytes() == original
    finally:
        s.close()
    copy = Storage(target)
    try:
        copy.check()
        assert hits(copy, 'new') == ['новий new']
        assert hits(copy, 'old') == []
        assert copy.get_page('a', 'old', 1)['raw'] == 'Міст old'
    finally:
        copy.close()


def test_backup_competing_destination(tmp_path, monkeypatch):
    from carr_library.storage import revisions
    source, target = tmp_path / 'library.db', tmp_path / 'backup.db'
    seed(source)
    real_link = revisions.os.link

    def competitor(src, dst):
        target.write_bytes(b'other writer')
        real_link(src, dst)

    monkeypatch.setattr(revisions.os, 'link', competitor)
    s = Storage(source)
    try:
        with pytest.raises(FileExistsError):
            s.backup(target)
        assert target.read_bytes() == b'other writer'
        assert not list(tmp_path.glob('.carr-backup-*'))
    finally:
        s.close()


def test_backup_failed_check_never_publishes(tmp_path, monkeypatch):
    source, target = tmp_path / 'library.db', tmp_path / 'backup.db'
    seed(source)

    def fail_check(self, *, cancelled=None):
        assert callable(cancelled)
        raise ValueError('synthetic integrity failure')

    monkeypatch.setattr(Storage, 'check', fail_check)
    s = Storage(source)
    try:
        with pytest.raises(ValueError, match='integrity failure'):
            s.backup(target)
        assert not target.exists()
        assert not list(tmp_path.glob('.carr-backup-*'))
    finally:
        s.close()
