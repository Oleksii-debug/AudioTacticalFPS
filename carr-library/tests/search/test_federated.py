"""Selected-library sessions use real imports, snapshots and citation services.

Clock/cancellation hooks exercise orchestration, not model relevance or native
call preemption. Semantic-without-model tests assert the explicit host fallback.
"""
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest

import carr_library.application as application
from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    one, two = (app.create_workspace('Same library name') for _ in range(2))
    c1, c2 = (app.create_collection(workspace, 'Sources') for workspace in (one, two))
    t1, t2 = (app.create_topic(workspace, 'Research') for workspace in (one, two))
    sources = []
    for workspace, collection, author, language, edition in (
            (one, c1, 'Author One', 'uk', 'First'),
            (two, c2, 'Author Two', 'en', 'Second')):
        sources.append(app.add_text(workspace, 'Railway history',
            'Railway history and archives. Railway history continues.',
            authors=(author,), language=language, edition=edition,
            collections=(collection,), work_id='same-work', edition_id='same-edition'))
    return app, one, two, c1, c2, t1, t2, sources


def test_grouped_explicit_selection_dedup_caps_and_independent_host_refs(research):
    app, one, two, _, _, t1, _, _ = research
    host_hit = app.search(one, 'archives')[0]
    host_selections = app._selections
    # A full existing host cache must not be evicted by a federated search.
    selection = host_selections[host_hit.ref]
    host_selections.update((f'existing-{i}', selection) for i in range(1999))
    previous = dict(host_selections)
    controller = FederatedSearch(app)
    session = controller.search([two, one, two], 'Railway', limit_per_library=1)

    assert session.workspaces == (two, one)
    assert [group.library_name for group in session.groups] == ['Same library name'] * 2
    assert [len(group.hits) for group in session.groups] == [1, 1]
    assert [hit.workspace for hit in session.hits] == [two, one]
    assert all(hit.result.verified for hit in session.hits)
    assert app._selections is host_selections and app._selections == previous
    assert controller.current is session
    assert app.copy(one, host_hit.ref, host_hit.revision, 'quote') == 'archives'
    with pytest.raises(FrozenInstanceError):
        session.hits[0].result.workspace = one

    hit = session.groups[1].hits[0]
    assert 'archives' in controller.context(hit).pages[0].raw_text
    assert controller.copy(hit, 'quote') == 'Railway'
    assert controller.topics(hit)[0].id == t1
    evidence_id = controller.save_hit(hit, t1, 'My separate note')
    saved, = app.evidence(one, t1)
    assert saved.evidence_id == evidence_id and saved.note == 'My separate note'
    assert saved.anchor.identity.workspace == one and saved.anchor.quote == hit.result.quote
    assert app._selections == previous
    reopened = LibraryApp(app.root)
    assert reopened.evidence(one, t1) == (saved,)


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'thematic', 'semantic'])
def test_existing_modes_metadata_filters_and_empty_selected_group(research, mode):
    app, one, two, c1, c2, *_ = research
    controller = FederatedSearch(app)
    messages = []
    query = 'history' if mode == 'word' else 'Railway history'
    session = controller.search([one, two], query, mode=mode, result_mode='short',
        collections={one: c1, two: c2}, title='Railway', author='Author One',
        language='uk', edition='First', on_status=messages.append)
    assert session.groups[0].hits and session.groups[1].hits == ()
    for hit in session.hits:
        assert hit.result.author == 'Author One' and hit.workspace == one
        assert controller.copy(hit, 'quote') == hit.result.quote
    if mode == 'semantic':
        assert len(messages) == 2
        assert all('Local model unavailable; keyword search used.' in message for message in messages)
        assert one in messages[0] and two in messages[1]
    else:
        assert messages == []


def test_name_refresh_and_only_explicit_libraries_are_searched(research):
    app, one, two, *_ = research
    other = app.create_workspace('Unselected')
    app.add_text(other, 'Hidden here', 'A private unselected phrase.')
    controller = FederatedSearch(app)
    assert controller.search([one], 'unselected').hits == ()
    app.rename_workspace(one, 'Renamed library')
    session = controller.search([one, two], 'history')
    assert session.groups[0].library_name == 'Renamed library'
    assert session.groups[1].library_name == 'Same library name'
    assert controller.search([other], 'unselected').hits[0].workspace == other


def test_missing_last_library_preserves_completed_session_and_original_host(research, monkeypatch):
    app, one, _, *_ = research
    controller = FederatedSearch(app)
    previous = controller.search([one], 'archives')
    host_before = dict(app._selections)
    calls = []
    original = LibraryApp.search

    def tracked(self, workspace, *args, **kwargs):
        assert self is not app
        calls.append(workspace)
        return original(self, workspace, *args, **kwargs)

    monkeypatch.setattr(LibraryApp, 'search', tracked)
    with pytest.raises(ValueError, match='missing or archived'):
        controller.search([one, 'missing-last-library'], 'history')
    assert calls == [one]
    assert controller.current is previous and app._selections == host_before
    assert controller.copy(previous.hits[0], 'quote') == 'archives'


def test_cancellation_after_first_library_retains_previous_capabilities(research, monkeypatch):
    app, one, two, *_ = research
    controller = FederatedSearch(app)
    previous = controller.search([one], 'archives')
    host_before = dict(app._selections)
    stopped = Event()
    original = LibraryApp.search
    calls = []

    def tracked(self, workspace, *args, **kwargs):
        calls.append(workspace)
        rows = original(self, workspace, *args, **kwargs)
        stopped.set()
        return rows

    monkeypatch.setattr(LibraryApp, 'search', tracked)
    with pytest.raises(TimeoutError):
        controller.search([one, two], 'history', cancelled=stopped.is_set)
    assert calls == [one]
    assert controller.current is previous and app._selections == host_before
    assert controller.copy(previous.hits[0], 'quote') == 'archives'


def test_single_deadline_shrinks_across_libraries_and_rejects_final_late_result(research, monkeypatch):
    app, one, two, *_ = research
    controller = FederatedSearch(app)
    previous = controller.search([one], 'archives')
    original = LibraryApp.search
    clock, allowances = [100.0], []

    def charged(self, workspace, *args, **kwargs):
        allowances.append(kwargs['timeout'])
        rows = original(self, workspace, *args, **kwargs)
        clock[0] += 3.0
        return rows

    monkeypatch.setattr(application, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(LibraryApp, 'search', charged)
    with pytest.raises(TimeoutError):
        controller.search([one, two], 'history', timeout=5.0)
    assert allowances == [5.0, 2.0]
    assert controller.current is previous and app._selections == {}
    assert controller.copy(previous.hits[0], 'quote') == 'archives'


def test_name_lookup_time_consumes_the_same_deadline(research, monkeypatch):
    app, one, two, *_ = research
    controller = FederatedSearch(app)
    clock = [100.0]
    original = application.LibraryApp._search_read
    from contextlib import contextmanager

    @contextmanager
    def charged(self, workspace, timeout, cancelled):
        with original(self, workspace, timeout, cancelled) as state:
            yield state
            clock[0] += 2

    def forbidden(*args, **kwargs):
        pytest.fail('An expired name lookup must not start the source search.')

    monkeypatch.setattr(application, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(LibraryApp, '_search_read', charged)
    monkeypatch.setattr(LibraryApp, 'search', forbidden)
    with pytest.raises(TimeoutError):
        controller.search([one, two], 'history', timeout=1)
    assert controller.current is None and app._selections == {}


def test_coarse_clock_does_not_expand_maximum_host_budget(research, monkeypatch):
    app, one, two, *_ = research
    monkeypatch.setattr(application, 'monotonic', lambda: 61.001)
    controller = FederatedSearch(app)
    session = controller.search([one, two], 'history', timeout=60)
    assert session.hits and all(hit.result.verified for hit in session.hits)


def test_source_tampering_is_an_error_and_does_not_replace_previous_session(research):
    app, one, two, _, _, t1, _, sources = research
    controller = FederatedSearch(app)
    previous = controller.search([one], 'archives')
    path = app.assets.resolve(sources[1]['asset'])
    original = path.read_bytes()
    path.write_bytes(original.replace(b'Railway', b'Highway', 1))
    with pytest.raises(ValueError, match='retained asset digest mismatch'):
        controller.search([one, two], 'history')
    assert controller.current is previous and app._selections == {}
    for action in (lambda: controller.preview(previous.hits[0]),
                   lambda: controller.context(previous.hits[0]),
                   lambda: controller.copy(previous.hits[0]),
                   lambda: controller.save_hit(previous.hits[0], t1)):
        with pytest.raises(ValueError, match='retained asset digest mismatch'):
            action()
    path.write_bytes(original)
    assert controller.copy(previous.hits[0], 'quote') == 'archives'
    assert app.evidence(one, t1) == ()


def test_actions_reauthorize_collection_and_archived_workspace(research):
    app, one, two, c1, c2, t1, _, _ = research
    controller = FederatedSearch(app)
    session = controller.search([one, two], 'archives', collections={one: c1, two: c2})
    first, second = session.hits
    with app.catalog_transaction(one) as catalog:
        catalog.remove(c1, 'same-edition')
    for action in (lambda: controller.preview(first), lambda: controller.context(first),
                   lambda: controller.copy(first), lambda: controller.save_hit(first, t1)):
        with pytest.raises(ValueError, match='left the selected collection'):
            action()
    app.archive_workspace(two)
    for action in (lambda: controller.preview(second), lambda: controller.copy(second),
                   lambda: controller.topics(second)):
        with pytest.raises(ValueError, match='archived'):
            action()
    with pytest.raises(ValueError, match='archived'):
        controller.search([one, two], 'history')
    assert controller.current is session


def test_old_and_copied_tokens_cannot_drive_new_session_or_foreign_topic(research):
    app, one, two, _, _, t1, t2, _ = research
    controller = FederatedSearch(app)
    previous = controller.search([one], 'archives').hits[0]
    current = controller.search([two], 'history').hits[0]
    for hit in (previous, replace(current),
                replace(current, result=replace(current.result, workspace=one))):
        with pytest.raises(ValueError, match='latest completed'):
            controller.copy(hit)
    with pytest.raises(ValueError, match='unknown or archived topic'):
        controller.save_hit(current, t1)
    assert app.evidence(one, t1) == () and app.evidence(two, t2) == ()
    assert controller.save_hit(current, t2)
    controller.clear()
    assert controller.current is None
    with pytest.raises(ValueError, match='latest completed'):
        controller.preview(current)


def test_real_pdf_preview_cannot_be_copied_or_saved_as_verified(research, tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    app, one, two, _, _, _, topic, _ = research
    pdf = tmp_path / 'source.pdf'
    canvas = Canvas(str(pdf), invariant=1)
    canvas.drawString(72, 700, 'Unreviewed native PDF source')
    canvas.save()
    imported = app.import_file(two, pdf, title='Candidate source')
    assert imported['state'] == 'complete' and imported['verified'] is False
    controller = FederatedSearch(app)
    session = controller.search([one, two], 'Unreviewed')
    hit, = session.hits
    assert hit.workspace == two and hit.result.verified is False
    assert 'Unreviewed native PDF source' in controller.preview(hit)
    for action in (lambda: controller.context(hit), lambda: controller.copy(hit),
                   lambda: controller.save_hit(hit, topic)):
        with pytest.raises(ValueError, match='Unverified extraction'):
            action()
    assert app.evidence(two, topic) == () and app._selections == {}


@pytest.mark.parametrize('selection', [None, '', 'one', (), [], {}, [None], [True], [''],
                                      [' '], ['a\x00b'], ['\ud800'], list(map(str, range(21)))])
def test_invalid_or_implicit_selection_is_rejected_before_reads(research, selection, monkeypatch):
    app, *_ = research
    controller = FederatedSearch(app)

    def forbidden(*args, **kwargs):
        pytest.fail('An invalid or implicit selection must not read a library.')

    monkeypatch.setattr(LibraryApp, 'read', forbidden)
    with pytest.raises(ValueError):
        controller.search(selection, 'history')
    assert controller.current is None


@pytest.mark.parametrize('options', [dict(limit_per_library=0), dict(limit_per_library=101),
    dict(limit_per_library=True), dict(timeout=0), dict(timeout=61), dict(timeout=float('nan')),
    dict(timeout=True), dict(cancelled=False), dict(on_status=False), dict(mode='all'),
    dict(result_mode='unbounded'), dict(collections='all'), dict(collections={'foreign': 'collection'}),
    dict(title='')])
def test_invalid_options_keep_completed_session(research, options):
    app, one, *_ = research
    controller = FederatedSearch(app)
    previous = controller.search([one], 'archives')
    with pytest.raises(ValueError):
        controller.search([one], 'history', **options)
    assert controller.current is previous


def test_cancelled_search_never_initializes_a_second_database_or_reads(research, monkeypatch):
    app, one, *_ = research

    def forbidden(*args, **kwargs):
        pytest.fail('A composed search must not initialize another LibraryApp.')

    monkeypatch.setattr(LibraryApp, '__init__', forbidden)
    controller = FederatedSearch(app)
    assert controller.search([one], 'archives').hits
    previous = controller.current
    monkeypatch.setattr(LibraryApp, 'read', forbidden)
    with pytest.raises(TimeoutError):
        controller.search([one], 'history', cancelled=lambda: True)
    assert controller.current is previous


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'thematic', 'semantic'])
@pytest.mark.parametrize('query', [None, True, [], '', ' \t\r\n', 'x' * 501,
                                  'history\x00', 'history\x7f', 'history\ud800'])
def test_malformed_query_rejected_even_for_empty_corpus_before_host_calls(tmp_path, monkeypatch, mode, query):
    app = LibraryApp(tmp_path / 'empty-library')
    workspace = app.create_workspace('Empty selected library')
    controller = FederatedSearch(app)

    def forbidden(*args, **kwargs):
        pytest.fail('Malformed public queries must fail before a host call, even with no sources.')

    monkeypatch.setattr(LibraryApp, 'read', forbidden)
    monkeypatch.setattr(LibraryApp, 'search', forbidden)
    with pytest.raises(ValueError, match='nonblank search query'):
        controller.search([workspace], query, mode=mode)
    assert controller.current is None and app._selections == {}


def test_valid_query_boundary_and_allowed_whitespace(research):
    app, one, *_ = research
    controller = FederatedSearch(app)
    session = controller.search([one], 'Railway\t\r\nhistory')
    assert session.hits and controller.copy(session.hits[0], 'quote') == 'Railway history'
    boundary = controller.search([one], 'x' * 500)
    assert boundary.hits == () and controller.current is boundary
    with pytest.raises(ValueError, match='nonblank search query'):
        controller.search([one], 'x' * 501)
    assert controller.current is boundary
