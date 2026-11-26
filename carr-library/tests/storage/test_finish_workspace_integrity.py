"""A successful backup must not silently hide a populated research library."""
from contextlib import closing
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.storage.revisions import Storage


def populated_library(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Library Україна', 'Source-backed research')
    app.add_text(workspace, 'Historical source', 'Exact historical quotation.')
    topic = app.create_topic(workspace, 'Research')
    hit, = app.search(workspace, 'historical quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Independent note')
    return app, workspace, topic


def test_backup_refuses_missing_workspace_presentation(tmp_path):
    app, workspace, _ = populated_library(tmp_path)
    # Simulate a damaged profile without removing any retained source or evidence.
    with closing(sqlite3.connect(app.database)) as db:
        db.execute('DELETE FROM workspace_meta WHERE workspace=?', (workspace,))
        db.commit()
    assert app.workspaces(include_archived=True) == ()
    with closing(Storage(app.database, schema_version=4)) as storage:
        with pytest.raises(ValueError, match='workspace'):
            storage.check()
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='workspace'):
        app.backup(destination)
    assert not destination.exists()
    with pytest.raises(ValueError, match='workspace'):
        LibraryApp(app.root)


@pytest.mark.parametrize(('column', 'value'), [
    ('name', b'Binary name'),
    ('name', 'Library\x00hidden suffix'),
    ('description', b'Binary description'),
    ('description', 'Description\x00hidden suffix'),
])
def test_backup_refuses_malformed_workspace_presentation(tmp_path, column, value):
    app, workspace, _ = populated_library(tmp_path)
    assert column in ('name', 'description')
    with closing(sqlite3.connect(app.database)) as db:
        db.execute(f'UPDATE workspace_meta SET {column}=? WHERE workspace=?', (value, workspace))
        db.commit()
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='workspace'):
        app.backup(destination)
    assert not destination.exists()


def test_archived_populated_workspace_survives_checked_backup_and_restore(tmp_path):
    app, workspace, topic = populated_library(tmp_path)
    before = app.export_topic(workspace, topic, format='text')
    app.archive_workspace(workspace)
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(restored_root)
    assert restored.workspaces() == ()
    archived, = restored.workspaces(include_archived=True)
    assert archived.id == workspace and archived.archived
    restored.restore_workspace(workspace)
    assert restored.export_topic(workspace, topic, format='text') == before
    assert restored.evidence(workspace, topic)[0].note == 'Independent note'
