from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp, open_existing_library
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.reader.reading_desk import ReadingDesk


def inventory(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_existing_reader_never_constructs_storage_or_takes_writer(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Existing')
    item = app.add_text(workspace, 'Text', 'Exact retained source.')
    import carr_library.application as module
    monkeypatch.setattr(module, 'Storage', lambda *a, **k: pytest.fail('No writable Storage'))
    with exclusive_writer(app.root):
        before = inventory(app.root)
        opened = open_existing_library(app.root)
        assert ReadingDesk(opened, workspace, item['edition']).page(1).raw_text == 'Exact retained source.'
        assert inventory(app.root) == before


def test_missing_root_or_database_does_not_create_anything(tmp_path):
    missing = tmp_path / 'missing'
    with pytest.raises((OSError, ValueError)):
        open_existing_library(missing)
    assert not missing.exists()
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises((OSError, ValueError)):
        open_existing_library(empty)
    assert list(empty.iterdir()) == []


def test_unknown_schema_is_not_migrated(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    with sqlite3.connect(app.database) as db:
        db.execute('PRAGMA user_version=999')
    before = app.database.read_bytes()
    with pytest.raises(ValueError, match='schema'):
        open_existing_library(app.root)
    assert app.database.read_bytes() == before


def test_incomplete_restore_and_cancellation_are_refused(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    with pytest.raises(TimeoutError):
        open_existing_library(app.root, cancelled=lambda: True)
    (app.root / '.restore-incomplete').write_text('not complete', encoding='utf-8')
    with pytest.raises(ValueError, match='incomplete'):
        open_existing_library(app.root)
