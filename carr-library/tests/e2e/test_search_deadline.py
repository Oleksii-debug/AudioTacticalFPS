"""A real source search budget covers SQLite schema and scope authorization."""
from contextlib import closing
import sqlite3
from time import monotonic

import pytest

import carr_library.application as application
from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Read deadline fixture')
    app.add_text(workspace, 'Source', 'Railway history and archives.')
    return app, workspace


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'thematic', 'semantic'])
def test_busy_search_fails_promptly_and_preserves_previous_selection(research, mode):
    app, workspace = research
    old, = app.search(workspace, 'history')
    previous = dict(app._selections)
    with closing(sqlite3.connect(app.database, isolation_level=None)) as writer:
        assert writer.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        writer.execute('BEGIN EXCLUSIVE')
        try:
            started = monotonic()
            with pytest.raises((RuntimeError, TimeoutError), match='busy|timed out'):
                app.search(workspace, 'Railway', mode=mode, timeout=.05)
            assert monotonic() - started < 1.0
            assert app._selections == previous
        finally:
            writer.rollback()
    assert app.copy(workspace, old.ref, old.revision, 'quote') == 'history'
    assert app.search(workspace, 'archives', mode=mode)


def test_federated_name_lookup_uses_budget_and_keeps_completed_session(research):
    app, workspace = research
    search = FederatedSearch(app)
    previous = search.search((workspace,), 'history')
    with closing(sqlite3.connect(app.database, isolation_level=None)) as writer:
        assert writer.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        writer.execute('BEGIN EXCLUSIVE')
        try:
            started = monotonic()
            with pytest.raises((RuntimeError, TimeoutError), match='busy|timed out'):
                search.search((workspace,), 'Railway', timeout=.05)
            assert monotonic() - started < 1.0
            assert search.current is previous
        finally:
            writer.rollback()
    assert search.copy(previous.hits[0], 'quote') == 'history'
    assert search.search((workspace,), 'archives').hits


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
@pytest.mark.parametrize('phase', ['schema', 'authorization'])
def test_cancel_interrupts_initial_schema_and_authorization_sql(research, monkeypatch, journal, phase):
    app, workspace = research
    old, = app.search(workspace, 'history')
    previous = dict(app._selections)
    with closing(sqlite3.connect(app.database, isolation_level=None)) as db:
        assert db.execute(f'PRAGMA journal_mode={journal}').fetchone()[0] == journal.lower()
    running = False
    completed = False

    def expensive_sql(db):
        nonlocal running, completed
        running = True
        try:
            db.execute('WITH RECURSIVE n(v) AS '
                '(SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<20000) '
                'SELECT sum(v) FROM n').fetchone()
            completed = True
        finally:
            # Once SQLite unwinds, the cancellation signal is gone. The
            # operation must still remember it, before results are published.
            running = False

    with monkeypatch.context() as patch:
        if phase == 'schema':
            original = application._schema_signature

            def check_schema(db):
                expensive_sql(db)
                return original(db)

            patch.setattr(application, '_schema_signature', check_schema)
        else:
            original = app._authorize

            def authorize(db, scope):
                expensive_sql(db)
                return original(db, scope)

            patch.setattr(app, '_authorize', authorize)
        with pytest.raises(TimeoutError, match='cancelled or timed out'):
            app.search(workspace, 'Railway', cancelled=lambda: running)
    assert not completed
    assert app._selections == previous
    assert app.copy(workspace, old.ref, old.revision, 'quote') == 'history'
    assert app.search(workspace, 'archives')


def test_deadline_interrupts_schema_sql_before_it_completes(research, monkeypatch):
    app, workspace = research
    original = application._schema_signature
    completed = False

    def expensive_schema(db):
        nonlocal completed
        db.execute('WITH RECURSIVE n(v) AS '
            '(SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<1000000) '
            'SELECT sum(v) FROM n').fetchone()
        completed = True
        return original(db)

    monkeypatch.setattr(application, '_schema_signature', expensive_schema)
    with pytest.raises(TimeoutError, match='timed out'):
        app.search(workspace, 'Railway', timeout=.01)
    assert not completed
    assert app._selections == {}


def test_wal_writer_does_not_change_the_authorized_read_snapshot(research):
    app, workspace = research
    with closing(sqlite3.connect(app.database, isolation_level=None)) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE workspace_meta SET archived=1 WHERE workspace=?', (workspace,))
        try:
            hit, = app.search(workspace, 'history', timeout=1.0)
            assert hit.verified and hit.workspace == workspace
            assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'history'
        finally:
            writer.rollback()
    with app.read(workspace) as db:
        assert db.in_transaction
        assert db.execute('SELECT archived FROM workspace_meta WHERE workspace=?',
                          (workspace,)).fetchone()[0] == 0
