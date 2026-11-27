"""The desktop cancellation control covers the complete source search operation."""

from contextlib import closing

import sqlite3

from threading import Event

from time import monotonic

import pytest

import carr_library.application as application

from carr_library.application import LibraryApp

@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Budget fixture')
    app.add_text(workspace, 'Source', 'Railway history and railway archives.')
    return app, workspace

@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'thematic', 'semantic'])
def test_busy_search_fails_promptly_and_preserves_previous_selection(research, mode):
    app, workspace = research
    previous, = app.search(workspace, 'history')
    selections = dict(app._selections)
    with closing(sqlite3.connect(app.database, isolation_level=None)) as writer:
        assert writer.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        writer.execute('BEGIN EXCLUSIVE')
        try:
            started = monotonic()
            with pytest.raises(RuntimeError, match='library is busy'):
                app.search(workspace, 'history', mode=mode, timeout=.05)
            assert monotonic() - started < 1.0
            assert app._selections == selections
        finally:
            writer.rollback()
    assert app.copy(workspace, previous.ref, previous.revision, 'quote') == 'history'
    assert app.search(workspace, 'archives')

@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
@pytest.mark.parametrize('phase', ['schema', 'authorization'])
def test_search_cancellation_covers_initial_sql_and_cleans_up(research, monkeypatch, journal, phase):
    app, workspace = research
    previous, = app.search(workspace, 'history')
    selections = dict(app._selections)
    with closing(sqlite3.connect(app.database, isolation_level=None)) as db:
        assert db.execute(f'PRAGMA journal_mode={journal}').fetchone()[0] == journal.lower()
    in_initial_sql = False
    finished = False
    captured = []
    def expensive(db):
        nonlocal in_initial_sql, finished
        captured.append(db)
        in_initial_sql = True
        try:
            db.execute('WITH RECURSIVE n(v) AS '
                '(SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<20000) '
                'SELECT sum(v) FROM n').fetchone()
            finished = True
        finally:
            in_initial_sql = False
    if phase == 'schema':
        original = application._schema_signature
        def schema(db):
            expensive(db)
            return original(db)
        monkeypatch.setattr(application, '_schema_signature', schema)
    else:
        original = app._authorize
        def authorize(db, scope):
            expensive(db)
            return original(db, scope)
        monkeypatch.setattr(app, '_authorize', authorize)
    with pytest.raises(TimeoutError, match='cancelled or timed out'):
        app.search(workspace, 'history', cancelled=lambda: in_initial_sql)
    assert not finished and app._selections == selections
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        captured[0].execute('SELECT 1')
    monkeypatch.undo()
    # Ordinary reads retain their existing contract and the next search succeeds.
    with app.read(workspace) as db:
        assert db.in_transaction
    assert app.copy(workspace, previous.ref, previous.revision, 'quote') == 'history'
    assert app.search(workspace, 'archives')

def test_initial_schema_sql_obeys_search_deadline(research, monkeypatch):
    app, workspace = research
    original = application._schema_signature
    finished = False
    def expensive_schema(db):
        nonlocal finished
        db.execute('WITH RECURSIVE n(v) AS '
            '(SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<10000000) '
            'SELECT sum(v) FROM n').fetchone()
        finished = True
        return original(db)
    monkeypatch.setattr(application, '_schema_signature', expensive_schema)
    started = monotonic()
    with pytest.raises(TimeoutError, match='timed out'):
        app.search(workspace, 'history', timeout=.02)
    assert monotonic() - started < 1.0
    assert not finished and app._selections == {}

def test_federated_name_lookup_obeys_busy_search_policy(research):
    from carr_library.search.federated import FederatedSearch
    app, workspace = research
    service = FederatedSearch(app)
    previous = service.search((workspace,), 'history')
    with closing(sqlite3.connect(app.database, isolation_level=None)) as writer:
        writer.execute('PRAGMA journal_mode=DELETE')
        writer.execute('BEGIN EXCLUSIVE')
        try:
            started = monotonic()
            with pytest.raises(RuntimeError, match='library is busy'):
                service.search((workspace,), 'history', timeout=.05)
            assert monotonic() - started < 1.0
            assert service.current is previous
        finally:
            writer.rollback()
    assert service.copy(previous.hits[0], 'quote') == 'history'
