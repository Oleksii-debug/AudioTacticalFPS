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


def test_cancel_before_search_does_not_read_the_catalog(research, monkeypatch):
    app, workspace = research

    def unexpected_catalog(*args, **kwargs):
        pytest.fail('An already cancelled search must not start a catalog scan.')

    monkeypatch.setattr(application, 'browse_editions', unexpected_catalog)
    with pytest.raises(TimeoutError):
        app.search(workspace, 'railway', cancelled=lambda: True)
    assert app._selections == {}


def test_cancel_interrupts_native_catalog_work_and_is_latched(research, monkeypatch):
    app, workspace = research
    original = application.browse_editions
    in_catalog = False
    completed_catalog = False

    def slow_catalog(catalog, **options):
        nonlocal in_catalog, completed_catalog
        in_catalog = True
        try:
            catalog.db.execute('WITH RECURSIVE n(v) AS '
                '(SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<20000) '
                'SELECT sum(v) FROM n').fetchone()
            completed_catalog = True
        finally:
            # The cancellation source is intentionally transient. The whole
            # operation must remember it even after SQLite unwinds this call.
            in_catalog = False
        return original(catalog, **options)

    monkeypatch.setattr(application, 'browse_editions', slow_catalog)
    with pytest.raises(TimeoutError):
        app.search(workspace, 'railway', cancelled=lambda: in_catalog)
    assert not completed_catalog
    assert app._selections == {}
    monkeypatch.setattr(application, 'browse_editions', original)
    assert app.search(workspace, 'archives')[0].verified


def test_cancel_during_attestation_publishes_no_partial_result(research, monkeypatch):
    app, workspace = research
    previous, = app.search(workspace, 'history')
    previous_selections = dict(app._selections)
    stopped = Event()
    original = app._loaders

    def loaders(db, scope, **options):
        load, metadata, attest = original(db, scope, **options)

        def cancel_after_attestation(*args):
            verified = attest(*args)
            stopped.set()
            return verified

        return load, metadata, cancel_after_attestation

    monkeypatch.setattr(app, '_loaders', loaders)
    with pytest.raises(TimeoutError):
        app.search(workspace, 'railway', cancelled=stopped.is_set)
    assert app._selections == previous_selections
    assert app.copy(workspace, previous.ref, previous.revision, 'quote') == 'history'


def test_catalog_time_is_charged_to_the_same_search_deadline(research, monkeypatch):
    app, workspace = research
    current = [100.0]
    original = application.browse_editions

    def catalog(catalog, **options):
        rows = original(catalog, **options)
        current[0] += 2
        return rows

    monkeypatch.setattr(application, 'monotonic', lambda: current[0], raising=False)
    monkeypatch.setattr(application, 'browse_editions', catalog)
    with pytest.raises(TimeoutError):
        app.search(workspace, 'railway', timeout=1.0)
    assert app._selections == {}


def test_coarse_clock_does_not_expand_valid_search_timeout(research, monkeypatch):
    app, workspace = research
    # On a coarse clock, successive reads can be equal. Adding a deadline and
    # subtracting this representable value produces 60.00000000000001, which
    # the underlying literal search correctly rejects as above its limit.
    monkeypatch.setattr(application, 'monotonic', lambda: 61.001)
    hit, = app.search(workspace, 'history')
    assert hit.quote == 'history'
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'history'


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf'), True, '5'])
def test_invalid_operation_budget_is_rejected(research, timeout):
    app, workspace = research
    with pytest.raises(ValueError, match='budget'):
        app.search(workspace, 'railway', timeout=timeout)


def test_saved_query_cancellation_during_original_verification(research, monkeypatch):
    app, workspace = research
    topic = app.create_topic(workspace, 'Sources')
    hit, = app.search(workspace, 'history')
    app.save_hit(workspace, topic, hit.ref, hit.revision)
    query = app.save_query(workspace, topic, 'History search', 'history')
    stopped = Event()
    original = app._loaders

    def loaders(db, scope, **options):
        load, metadata, attest = original(db, scope, **options)

        def cancel_after_source(*args):
            source = load(*args)
            stopped.set()
            return source

        return cancel_after_source, metadata, attest

    monkeypatch.setattr(app, '_loaders', loaders)
    with pytest.raises(TimeoutError):
        app.run_query(workspace, topic, query, cancelled=stopped.is_set)
    monkeypatch.setattr(app, '_loaders', original)
    assert app.run_query(workspace, topic, query)[0].evidence.anchor.quote == 'history'


def test_unavailable_model_reports_fallback_even_without_results(research):
    app, workspace = research
    messages = []
    assert app.search(workspace, 'unrelated botanical source', mode='semantic',
                      on_status=messages.append) == ()
    assert messages == ['Local model unavailable; keyword search used.']


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
                # Isolate immediate lock reporting from deadline expiry. A
                # preempted Windows runner may consume 50 ms before SQLite
                # reports BUSY, in which case TimeoutError is correctly used.
                # The independent <1 s assertion still rejects SQLite's old
                # five-second busy wait; deadline tests remain separate.
                app.search(workspace, 'history', mode=mode, timeout=5)
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
