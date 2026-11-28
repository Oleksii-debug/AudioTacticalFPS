"""A single actual checked SQLite snapshot, never a cached constructor check."""
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker
from carr_library.ingest.reconciliation import reconciliation
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.storage.revisions import Storage


def _completed(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded exact reconciliation')
    source = tmp_path / 'original.txt'
    source.write_text('Exact actual source for the held transaction.', encoding='utf-8')
    budget = replace(ImportBudget(), source_bytes=MIB, segment_bytes=MIB,
        segments=10, steps=11, seconds=30, memory_bytes=512*MIB,
        library_bytes=32*MIB, temporary_bytes=128*MIB)
    result = app.import_file(workspace, source, import_budget=budget)
    assert result['state'] == 'complete' and result['isolated'], result
    saved = worker.load_request(app.root, result['request_id'])
    return app, workspace, result, saved


def test_whole_store_check_precedes_all_receipts_on_the_same_held_connection(tmp_path, monkeypatch):
    app, workspace, result, saved = _completed(tmp_path)
    actual_check = Storage._check
    observed = []

    def check(storage):
        assert storage.db.in_transaction
        observed.append(storage.db)
        return actual_check(storage)
    monkeypatch.setattr(Storage, '_check', check)
    with reconciliation(app, workspace) as owner:
        assert observed == [owner.db]
        for _ in range(8):
            recovered = worker._reconcile_publication(
                app.root, result['request_id'], saved['attempt'], initial=saved, _context=owner)
            assert recovered['state'] == 'complete' and recovered['id'] == result['id']
            assert recovered['publication_recovered'] is True
            assert owner.storage.db is owner.db and owner.db.in_transaction
        assert observed == [owner.db]
    assert len(observed) == 1
    with pytest.raises(ValueError, match='owner changed'):
        owner.check(app.root, workspace)


def test_real_competing_commit_is_excluded_until_the_checked_snapshot_closes(tmp_path):
    app, workspace, result, saved = _completed(tmp_path)
    with closing(sqlite3.connect(app.database, isolation_level=None, timeout=0)) as competing:
        with reconciliation(app, workspace) as owner:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                competing.execute('UPDATE workspace_meta SET archived=archived WHERE workspace=?',
                                  (workspace,))
            recovered = worker._reconcile_publication(
                app.root, result['request_id'], saved['attempt'], initial=saved, _context=owner)
            assert recovered['id'] == result['id']
            assert competing.in_transaction is False
        competing.execute('UPDATE workspace_meta SET archived=archived WHERE workspace=?',
                          (workspace,))
    # A new bounded pass performs a fresh full validation after that real commit.
    with reconciliation(app, workspace) as next_owner:
        assert worker._reconcile_publication(app.root, result['request_id'], saved['attempt'],
                initial=saved, _context=next_owner)['state'] == 'complete'


def test_unexpected_same_connection_write_invalidates_the_bounded_snapshot(tmp_path):
    app, workspace, result, saved = _completed(tmp_path)
    before = worker._path(app.root, result['request_id']).read_bytes()
    with pytest.raises(ValueError, match='generation changed'):
        with reconciliation(app, workspace) as owner:
            # A real SQL write, including an equal-value update, is not part of
            # this read/proof pass and cannot be adopted into its trusted stamp.
            owner.db.execute('UPDATE workspace_meta SET archived=archived WHERE workspace=?',
                             (workspace,))
            worker._reconcile_publication(app.root, result['request_id'], saved['attempt'],
                                          initial=saved, _context=owner)
    assert worker._path(app.root, result['request_id']).read_bytes() == before
    with reconciliation(app, workspace) as fresh:
        assert worker._reconcile_publication(app.root, result['request_id'], saved['attempt'],
                initial=saved, _context=fresh)['state'] == 'complete'


def test_held_context_rejects_foreign_scope_and_equal_byte_database_alias(tmp_path):
    app, workspace, result, saved = _completed(tmp_path)
    other = app.create_workspace('Other actual library scope')
    with reconciliation(app, workspace) as owner:
        with pytest.raises(ValueError, match='owner changed'):
            owner.check(app.root, other)
        alias = tmp_path / 'foreign-database.sqlite'
        import os
        os.link(app.database, alias)
        try:
            with pytest.raises(ValueError):
                worker._reconcile_publication(app.root, result['request_id'], saved['attempt'],
                                              initial=saved, _context=owner)
        finally:
            alias.unlink()
        assert worker._reconcile_publication(app.root, result['request_id'], saved['attempt'],
                initial=saved, _context=owner)['state'] == 'complete'
