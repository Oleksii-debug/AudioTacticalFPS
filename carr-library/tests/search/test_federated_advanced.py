"""Advanced queries retain Search Center's real-source and session guarantees."""
from dataclasses import replace
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch


@pytest.fixture
def catalog(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    first, second, hidden = (app.create_workspace('History') for _ in range(3))
    collections = {workspace: app.create_collection(workspace, 'Primary sources')
                   for workspace in (first, second, hidden)}
    topics = {workspace: app.create_topic(workspace, 'Memory and identity')
              for workspace in (first, second)}
    texts = (
        'Cafe\u0301 archives connect memory and identity. The original Ukrainian account.',
        'Identity beside memory in the original Slovak account.',
        'Private memory and identity never selected.',
    )
    sources = []
    for workspace, raw, author, language in zip((first, second, hidden), texts,
            ('First author', 'Second author', 'Private author'), ('uk', 'sk', 'en')):
        sources.append(app.add_text(workspace, 'Same source title', raw,
            authors=(author,), language=language, edition='Reviewed edition',
            collections=(collections[workspace],), work_id='same-work',
            edition_id='same-edition'))
    app.add_text(first, 'Excluded', 'memory and identity amid war',
                 collections=(collections[first],))
    return app, first, second, hidden, collections, topics, texts, sources


def test_advanced_selected_libraries_preserve_exact_witnesses_and_saved_evidence(catalog):
    app, first, second, hidden, collections, topics, texts, _ = catalog
    host_hit = app.search(first, 'archives')[0]
    host_refs = dict(app._selections)
    service = FederatedSearch(app)
    session = service.search([second, first],
        '(memory NEAR/1 identity OR "café archives") AND NOT war',
        mode='advanced', result_mode='short', collections={
            first: collections[first], second: collections[second]})
    assert session.mode == 'advanced' and session.workspaces == (second, first)
    assert [hit.workspace for hit in session.hits] == [second, first]
    assert all(hit.workspace != hidden for hit in session.hits)
    assert app._selections == host_refs
    for hit in session.hits:
        raw = texts[0] if hit.workspace == first else texts[1]
        assert hit.result.verified is True
        assert hit.result.quote in raw
        assert 'Advanced search:' in hit.result.reason
        assert service.copy(hit, 'quote') == hit.result.quote
        assert service.context(hit).pages[0].raw_text == raw
        saved_id = service.save_hit(hit, topics[hit.workspace], 'Independent research note')
        saved, = LibraryApp(app.root).evidence(hit.workspace, topics[hit.workspace])
        assert saved.evidence_id == saved_id
        assert saved.anchor.identity.workspace == hit.workspace
        assert saved.anchor.quote == hit.result.quote
        assert saved.note == 'Independent research note'
    assert app.copy(first, host_hit.ref, host_hit.revision, 'quote') == 'archives'


def test_advanced_metadata_filters_and_per_library_collections_are_conjunctive(catalog):
    app, first, second, _, collections, *_ = catalog
    app.add_text(first, 'Outside chosen collection', 'memory identity',
                 authors=('First author',), language='uk', edition='Reviewed edition')
    service = FederatedSearch(app)
    session = service.search([first, second], 'memory AND identity AND NOT war',
        mode='advanced', title='Same source', author='First author', language='uk',
        edition='Reviewed edition', collections={first: collections[first],
                                                second: collections[second]})
    assert len(session.groups[0].hits) == 1 and session.groups[1].hits == ()
    assert session.hits[0].result.title == 'Same source title'
    assert session.hits[0].workspace == first


@pytest.mark.parametrize('query', [
    'memory OR', '(memory AND identity', 'NOT memory',
    'memory OR NOT identity', 'memory NEAR/101 identity',
    '"unclosed', '() AND memory',
])
def test_invalid_advanced_grammar_rejected_before_any_library_read(catalog, monkeypatch, query):
    app, first, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    refs = dict(app._selections)

    def forbidden(*args, **kwargs):
        pytest.fail('An invalid query must not open any library snapshot.')

    with monkeypatch.context() as patch:
        patch.setattr(LibraryApp, '_search_read', forbidden)
        with pytest.raises(ValueError):
            service.search([first], query, mode='advanced')
    assert service.current is previous and app._selections == refs
    assert service.copy(previous.hits[0], 'quote') == 'archives'


def test_advanced_cancel_between_libraries_does_not_publish_partial_results(catalog, monkeypatch):
    app, first, second, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    cancelled = Event()
    original = LibraryApp.search
    visited = []

    def interrupt_after_first(self, workspace, query, **options):
        result = original(self, workspace, query, **options)
        visited.append(workspace)
        cancelled.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(LibraryApp, 'search', interrupt_after_first)
        with pytest.raises(TimeoutError):
            service.search([first, second], 'memory NEAR/1 identity',
                           mode='advanced', cancelled=cancelled.is_set)
    assert visited == [first] and service.current is previous
    assert service.copy(previous.hits[0], 'quote') == 'archives'
    assert app._selections == {}


def test_advanced_handles_reauthorize_scope_and_do_not_affect_other_library(catalog):
    app, first, second, _, collections, topics, *_ = catalog
    service = FederatedSearch(app)
    session = service.search([first, second], 'memory AND identity AND NOT war',
        mode='advanced', collections={first: collections[first], second: collections[second]})
    removed, kept = session.hits
    with app.catalog_transaction(first) as store:
        store.remove(collections[first], 'same-edition')
    for action in (lambda: service.preview(removed), lambda: service.context(removed),
                   lambda: service.copy(removed),
                   lambda: service.save_hit(removed, topics[first])):
        with pytest.raises(ValueError, match='left the selected collection'):
            action()
    assert service.copy(kept, 'quote') == kept.result.quote
    assert app.evidence(first, topics[first]) == ()
    app.archive_workspace(second)
    with pytest.raises(ValueError, match='archived'):
        service.copy(kept, 'quote')


def test_advanced_failed_retained_source_check_keeps_completed_session(catalog):
    app, first, second, _, _, _, _, sources = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    source = app.assets.resolve(sources[1]['asset'])
    original = source.read_bytes()
    source.write_bytes(b'Source revision tampered outside the application.')
    with pytest.raises(ValueError, match='retained asset digest mismatch'):
        service.search([first, second], 'memory AND identity', mode='advanced')
    assert service.current is previous and app._selections == {}
    source.write_bytes(original)
    assert service.copy(previous.hits[0], 'quote') == 'archives'


def test_advanced_boolean_and_proximity_do_not_bridge_pages_or_libraries(catalog):
    app, first, second, *_ = catalog
    app.add_text(first, 'Two separate segments', 'rareanchor\fotheranchor')
    app.add_text(first, 'First separate library', 'leftanchor')
    app.add_text(second, 'Second separate library', 'rightanchor')
    service = FederatedSearch(app)
    for query in ('rareanchor AND otheranchor', 'rareanchor NEAR/100 otheranchor',
                  'leftanchor AND rightanchor'):
        assert service.search([first, second], query, mode='advanced').hits == ()


@pytest.mark.parametrize(('mode', 'query'), [
    ('advanced', 'memory AND identity'), ('terms', 'memory identity'),
])
def test_result_pages_beyond_one_hundred_keep_order_and_current_page_authority(tmp_path, mode, query):
    app = LibraryApp(tmp_path / 'large-library')
    workspaces = tuple(app.create_workspace('Source collection') for _ in range(2))
    raw_pages = tuple(tuple(f'memory identity original source page {number:03d}'
                          for number in range(count)) for count in (107, 103))
    for workspace, pages in zip(workspaces, raw_pages):
        app.add_text(workspace, 'Many source segments', '\f'.join(pages))
    old_host = app.search(workspaces[0], 'original', limit=1)[0]
    host_refs = dict(app._selections)
    service = FederatedSearch(app)
    first = service.search(workspaces, query, mode=mode, result_mode='short',
                           limit_per_library=100)
    assert first.offset_per_library == 0 and first.has_more is True
    assert tuple(len(group.hits) for group in first.groups) == (100, 100)
    for group, expected in zip(first.groups, raw_pages):
        assert tuple(hit.result.quote for hit in group.hits) == expected[:100]
    previous_hit = first.hits[0]
    assert service.copy(previous_hit, 'quote') == previous_hit.result.quote
    second = service.search(workspaces, query, mode=mode, result_mode='short',
                            limit_per_library=100, offset_per_library=100)
    assert second.offset_per_library == 100 and second.has_more is False
    assert tuple(len(group.hits) for group in second.groups) == (7, 3)
    for group, expected in zip(second.groups, raw_pages):
        assert tuple(hit.result.quote for hit in group.hits) == expected[100:]
        assert service.copy(group.hits[-1], 'quote') == expected[-1]
    with pytest.raises(ValueError, match='latest completed search session'):
        service.copy(previous_hit, 'quote')
    assert app._selections == host_refs
    assert app.copy(workspaces[0], old_host.ref, old_host.revision, 'quote') == 'original'


def test_full_final_page_uses_exact_lookahead_instead_of_guessing_more_results(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    app.add_text(workspace, 'Source', '\f'.join(f'alpha beta {i}' for i in range(10)))
    service = FederatedSearch(app)
    session = service.search([workspace], 'alpha AND beta', mode='advanced',
                             limit_per_library=10)
    assert len(session.hits) == 10 and session.has_more is False


def test_final_result_page_after_ten_thousand_matches_is_not_rejected(tmp_path):
    from contextlib import closing
    from hashlib import sha256

    from carr_library.application import _CatalogPublisher
    from carr_library.catalog.registration import Registration
    from carr_library.citations.text_attestation import CONFIG
    from carr_library.ingest.writer_guard import exclusive_writer
    from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Page, Storage

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large research collection')
    # This case tests retrieval past 10,000 authorized segments, not the ingest
    # journal. Publish the synthetic originals through the existing retention,
    # catalog and sealed-revision APIs, in one transaction per edition, without
    # 10,001 per-page extraction checkpoints and their durable writes.
    with exclusive_writer(app.root), closing(
            Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        for edition, count in enumerate([1000] * 10 + [1]):
            pages = tuple(f'alpha beta edition {edition:02d} page {page:03d}'
                          for page in range(count))
            data = '\f'.join(pages).encode('utf-8')
            digest = sha256(data).hexdigest()
            app.assets.retain(data, 'text/plain', digest)
            record = Registration(f'work-{edition:02d}', f'edition-{edition:02d}',
                {'sha256': digest, 'mime': 'text/plain', 'size': len(data)},
                f'Source {edition:02d}')
            _CatalogPublisher(storage, record).publish_revision(workspace, record.edition_id,
                f'revision-{edition:02d}', digest, CONFIG, (Page(raw) for raw in pages))
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM active_page WHERE workspace=?',
                          (workspace,)).fetchone()[0] == 10001
    service = FederatedSearch(app)
    session = service.search([workspace], 'alpha AND beta', mode='advanced',
        result_mode='short', limit_per_library=1, offset_per_library=10000)
    hit, = session.hits
    assert hit.result.quote == 'alpha beta edition 10 page 000'
    assert session.has_more is False and session.offset_per_library == 10000
    assert service.copy(hit, 'quote') == hit.result.quote


def test_very_large_offset_remains_a_bounded_scan_and_cancellable(catalog):
    app, first, *_ = catalog
    service = FederatedSearch(app)
    offset = 10 ** 100
    session = service.search([first], 'memory AND identity', mode='advanced',
                             offset_per_library=offset)
    assert session.hits == () and session.has_more is False
    assert session.offset_per_library == offset and app._selections == {}
    with pytest.raises(TimeoutError):
        service.search([first], 'memory AND identity', mode='advanced',
                       offset_per_library=offset, cancelled=lambda: True)
    assert service.current is session


def test_lookahead_isolated_from_two_thousand_retained_selection_handles(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspaces = tuple(app.create_workspace(f'Library {i}') for i in range(20))
    pages = '\f'.join(f'alpha beta source page {i}' for i in range(101))
    for workspace in workspaces:
        app.add_text(workspace, 'Source', pages)
    service = FederatedSearch(app)
    session = service.search(workspaces, 'alpha AND beta', mode='advanced',
                             limit_per_library=100)
    assert len(session.hits) == 2000 and session.has_more is True
    assert len(service._completed.app._selections) == 2000
    for hit in (session.hits[0], session.hits[99], session.hits[100], session.hits[-1]):
        assert service.copy(hit, 'quote') == hit.result.quote
    assert app._selections == {}


@pytest.mark.parametrize('offset', [True, -1, float('inf'), 1.0, None, '10'])
def test_invalid_page_offsets_do_not_replace_completed_session(catalog, offset):
    app, first, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    with pytest.raises(ValueError, match='result-page offset'):
        service.search([first], 'memory AND identity', mode='advanced',
                       offset_per_library=offset)
    assert service.current is previous
    assert service.copy(previous.hits[0], 'quote') == 'archives'


@pytest.mark.parametrize('mode', ['phrase', 'word', 'thematic', 'semantic'])
def test_nonpaged_modes_reject_offset_instead_of_repeating_first_page(catalog, mode):
    app, first, *_ = catalog
    service = FederatedSearch(app)
    with pytest.raises(ValueError, match='All words and Advanced'):
        service.search([first], 'memory', mode=mode, offset_per_library=1)
    assert service.current is None and app._selections == {}


def test_cancel_during_lookahead_retains_the_previous_completed_page(catalog, monkeypatch):
    app, first, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    original = LibraryApp.search
    cancelled = Event()
    calls = []

    def interrupt_lookahead(self, workspace, query, **options):
        rows = original(self, workspace, query, **options)
        calls.append((options.get('offset', 0), options['limit']))
        if options.get('offset'):
            cancelled.set()
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(LibraryApp, 'search', interrupt_lookahead)
        with pytest.raises(TimeoutError):
            service.search([first], 'memory AND identity', mode='advanced',
                           limit_per_library=1, cancelled=cancelled.is_set)
    assert calls == [(0, 1), (1, 1)]
    assert service.current is previous and app._selections == {}
    assert service.copy(previous.hits[0], 'quote') == 'archives'


def test_an_unavailable_new_page_rechecks_live_scope_without_publishing(catalog):
    app, first, _, _, collections, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'memory AND identity', mode='advanced',
        limit_per_library=1, collections={first: collections[first]})
    with app.catalog_transaction(first) as store:
        store.archive(collections[first])
    with pytest.raises(ValueError, match='archived'):
        service.search([first], 'memory AND identity', mode='advanced',
            limit_per_library=1, offset_per_library=1, collections={first: collections[first]})
    assert service.current is previous
    with pytest.raises(ValueError, match='archived'):
        service.copy(previous.hits[0], 'quote')


@pytest.mark.parametrize('defect', ['too-many', 'foreign', 'unowned', 'wrong-type', 'unbounded'])
def test_malformed_lookahead_results_cannot_replace_an_authorized_session(catalog, monkeypatch, defect):
    app, first, second, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    original = LibraryApp.search

    def invalid_probe(self, workspace, query, **options):
        rows = original(self, workspace, query, **options)
        if options.get('offset'):
            assert len(rows) == 1, 'The probe fixture must contain a genuine next result.'
            if defect == 'too-many':
                return rows + rows
            if defect == 'foreign':
                return (replace(rows[0], workspace=second),)
            if defect == 'unowned':
                return (replace(rows[0], ref='forged-unknown-reference'),)
            if defect == 'unbounded':
                def excessive_rows():
                    yield rows[0]
                    yield rows[0]
                    pytest.fail('Lookahead must stop after the first excess result.')
                return excessive_rows()
            return ('untyped fabricated source',)
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(LibraryApp, 'search', invalid_probe)
        with pytest.raises(ValueError, match='lookahead outside its selected scope'):
            service.search([first], 'memory AND identity', mode='advanced', limit_per_library=1)
    assert service.current is previous and app._selections == {}
    assert service.copy(previous.hits[0], 'quote') == 'archives'


def test_excessive_primary_results_cannot_replace_an_authorized_session(catalog, monkeypatch):
    app, first, *_ = catalog
    service = FederatedSearch(app)
    previous = service.search([first], 'archives')
    original = LibraryApp.search

    def overfull(self, workspace, query, **options):
        return original(self, workspace, query, **options) * 2

    with monkeypatch.context() as patch:
        patch.setattr(LibraryApp, 'search', overfull)
        with pytest.raises(ValueError, match='exceeded its result limit'):
            service.search([first], 'memory AND identity', mode='advanced', limit_per_library=1)
    assert service.current is previous and app._selections == {}
    assert service.copy(previous.hits[0], 'quote') == 'archives'
