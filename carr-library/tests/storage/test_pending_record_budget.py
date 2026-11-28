"""Bound SQLite record delivery before decoding, preserving actual pending rows."""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget, MIB
from carr_library import pending_backup as pending


def journal(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded pending records')
    result = app.add_text(workspace, 'Original',
                          'Original first.\fOriginal second.\fOriginal third.')
    assert result['state'] == 'complete'
    assert (app.root / 'imports.sqlite').is_file()
    return app, result


@pytest.mark.parametrize('column,size', [
    ('config', 4*128_000 + 1),
    ('config', pending.MAX_PAYLOAD + 2*MIB),
    ('payload', pending.MAX_PAYLOAD + 1),
    ('payload', pending.MAX_PAYLOAD + 2*MIB),
])
def test_oversized_actual_stored_record_is_refused_before_json_decoder(
        tmp_path, monkeypatch, column, size):
    app, result = journal(tmp_path)
    path = app.root / 'imports.sqlite'
    with closing(sqlite3.connect(path)) as db, db:
        if column == 'config':
            db.execute('UPDATE job SET config=? WHERE id=?', ('x' * size, result['id']))
        else:
            db.execute('UPDATE checkpoint SET payload=? WHERE job=? AND ordinal=1',
                       ('x' * size, result['id']))
    monkeypatch.setattr(pending, '_decode',
        lambda *a, **k: pytest.fail('An oversized stored value reached the Python JSON decoder.'))
    selected = replace(BackupBudget(), journal_bytes=2*1024**3)
    with closing(pending._catalog(app.database, selected)) as catalog:
        with pytest.raises((ValueError, sqlite3.DataError), match='allowance|too big'):
            connection, _ = pending._journal(path, catalog, lambda: False, selected)
            connection.close()


def test_cancellation_is_checked_between_real_checkpoints_of_one_job(tmp_path, monkeypatch):
    app, result = journal(tmp_path)
    original = pending._checkpoint
    visited = []
    def observe(checkpoint, job, ordinal, kind):
        value = original(checkpoint, job, ordinal, kind)
        visited.append(ordinal)
        return value
    monkeypatch.setattr(pending, '_checkpoint', observe)
    with closing(pending._catalog(app.database)) as catalog:
        with pytest.raises(InterruptedError, match='cancelled'):
            connection, _ = pending._journal(app.root / 'imports.sqlite', catalog,
                                             lambda: bool(visited))
            connection.close()
    assert visited == [1]
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
        assert db.execute('SELECT count(*) FROM checkpoint WHERE job=?',
                          (result['id'],)).fetchone()[0] == 3
    with closing(sqlite3.connect(app.database)) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
