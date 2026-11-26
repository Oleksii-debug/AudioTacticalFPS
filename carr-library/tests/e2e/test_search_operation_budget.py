"""The desktop cancellation control covers the complete source search operation."""
from threading import Event

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

    def loaders(db, scope):
        load, metadata, attest = original(db, scope)

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

    def loaders(db, scope):
        load, metadata, attest = original(db, scope)

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
