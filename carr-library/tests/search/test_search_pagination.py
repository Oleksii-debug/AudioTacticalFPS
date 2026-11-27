"""Bounded pages over real SQLite catalogs and retained synthetic source bytes.

The bulk fixture uses the existing registration/revision transaction primitives.
It is not a throughput benchmark or a claim about a real research corpus.
"""
from contextlib import closing
from hashlib import sha256
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.citations.text_attestation import CONFIG
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.search import literal, pagination
from carr_library.search.federated import FederatedSearch
from carr_library.search.pagination import SearchPageBudget
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Page, Storage


def publish_text_corpus(app, workspace, *, count=1, text='Needle source text.',
                        prefix='edition', collection=None):
    """Publish exact synthetic source identities without faking an attestor."""
    raw = text.encode('utf-8')
    digest = sha256(raw).hexdigest()
    app.assets.retain(raw, 'text/plain', digest)
    manifest = dict(sha256=digest, mime='text/plain', size=len(raw))
    identities = tuple(f'{prefix}-{number:05d}' for number in range(count))
    records = tuple(Registration(
        f'work-{identity}', identity, manifest, f'Source {identity}',
        ('Synthetic researcher',), 'en', 'Synthetic edition', None,
        () if collection is None else (collection,)) for identity in identities)
    with exclusive_writer(app.root), closing(Storage(
            app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        with storage.transaction():
            LibraryApp._authorize(storage.db, workspace)
            register_batch(Catalog(storage.db, workspace), records)
            for record in records:
                revision = sha256((workspace + ':' + record.edition_id + ':' + digest).encode()).hexdigest()
                storage.stage_revision(workspace, record.edition_id, revision, digest,
                                       CONFIG, tuple(Page(segment) for segment in text.split('\f')))
                storage.db.execute(
                    'INSERT INTO source_metadata '
                    '(workspace,edition,revision,author,title,volume,edition_label) VALUES(?,?,?,?,?,?,?)',
                    (workspace, record.edition_id, revision, record.authors[0],
                     record.title, record.volume, record.edition))
        storage.check()
    return identities, digest


@pytest.fixture
def corpus(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Synthetic paged source')
    collection = app.create_collection(workspace, 'Selected sources')
    identities, digest = publish_text_corpus(
        app, workspace, count=3, text='Needle first sentence.\fNeedle second sentence.',
        collection=collection)
    return app, workspace, collection, identities, digest


def pages(app, workspace, query='Needle', **options):
    cursor = None
    for _ in range(1000):
        page = app.search_page(workspace, query, cursor=cursor, **options)
        yield page
        cursor = page.next_cursor
        if cursor is None:
            return
    pytest.fail('The finite synthetic search never completed.')


def test_more_than_500_editions_are_complete_stable_and_bounded(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('503 synthetic editions')
    expected, _ = publish_text_corpus(app, workspace, count=503)
    actual_search, batches = literal.search, []

    def observed(db, selected, query, **options):
        batches.append((tuple(options['edition_ids']), tuple(options['page_ids'])))
        return actual_search(db, selected, query, **options)

    monkeypatch.setattr(literal, 'search', observed)
    found, snapshots, previous = [], set(), None
    for page in pages(app, workspace, limit=37):
        assert len(page.results) <= 37
        assert page.total_editions == page.total_source_pages == 503
        assert 0 <= page.source_pages_examined <= 503
        snapshots.add(page.snapshot)
        if previous is not None:
            with pytest.raises(ValueError, match='Select a result'):
                app.copy(workspace, previous.ref, previous.revision, 'quote')
        if page.results:
            current = page.results[0]
            assert app.copy(workspace, current.ref, current.revision, 'quote') == 'Needle'
            previous = current
        assert all(hit.verified and hit.workspace == workspace for hit in page.results)
        found.extend(hit.edition_id for hit in page.results)
        assert sum(map(len, app._page_selection_refs.values())) <= 37 and len(app._selections) <= 37
    assert found == list(expected)
    assert len(set(found)) == 503 and len(snapshots) == 1
    assert batches and all(len(editions) <= 500 and 0 < len(ids) <= 100 for editions, ids in batches)
    assert page.source_pages_examined == 503 and page.next_cursor is None


@pytest.mark.parametrize('mode', ('phrase', 'word', 'terms', 'advanced'))
def test_literal_batches_cross_catalog_and_source_boundaries_without_gaps(corpus, mode):
    app, workspace, collection, identities, _ = corpus
    policy = SearchPageBudget(editions=2, pages=1)
    query = '"Needle" AND NOT "absent"' if mode == 'advanced' else 'Needle'
    observed = []
    for page in pages(app, workspace, query, mode=mode, collection=collection,
                      limit=1, page_budget=policy):
        observed.extend((row.edition_id, row.page, row.quote) for row in page.results)
    assert len(observed) == 6
    assert [row[0] for row in observed] == [identity for identity in identities for _ in range(2)]
    assert all(row[2] == 'Needle' for row in observed)
    assert len(set(observed)) == 6 and page.next_cursor is None


def test_keyset_passes_the_old_10000_offset_ceiling_without_collecting_all_matches(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Synthetic repeated matches')
    identities, _ = publish_text_corpus(app, workspace, text='Needle ' * 10003)
    seen, after = 0, None
    with app.read(workspace) as db:
        page_id, = db.execute('SELECT id FROM active_page WHERE workspace=?', (workspace,)).fetchone()
        while True:
            rows = literal.search(db, workspace, 'Needle', edition_ids=identities,
                                  page_ids=(page_id,), limit=100, after=after, timeout=60)
            assert len(rows) <= 100
            for row in rows:
                assert row['start'] == seen * 7 and row['end'] == seen * 7 + 6
                assert row['raw_quote'] == 'Needle'
                seen += 1
            if not rows:
                break
            row = rows[-1]
            after = (row['edition_id'], 1, row['page_id'], row['start'], row['end'])
        with pytest.raises(ValueError):
            literal.search(db, workspace, 'Needle', edition_ids=identities, offset=10001)
    assert seen == 10003


def test_short_presentations_do_not_repeat_the_same_span_across_pages(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Repeated words in a sentence')
    publish_text_corpus(app, workspace, text='Needle Needle Needle.')
    rows = [row for page in pages(app, workspace, limit=1, result_mode='short')
            for row in page.results]
    assert len(rows) == 1 and rows[0].quote == 'Needle Needle Needle.'


@pytest.mark.parametrize('mode', ('thematic', 'semantic'))
def test_ranked_pages_preserve_existing_batch_order_without_global_ranking(corpus, mode):
    app, workspace, _, _, _ = corpus
    policy = SearchPageBudget(pages=2)
    all_rows, notices = [], []
    for page in pages(app, workspace, mode=mode, limit=1, page_budget=policy,
                      on_status=notices.append):
        all_rows.extend((row.edition_id, row.page, row.quote) for row in page.results)
        assert 'local to each batch' in page.ordering and 'never a global score' in page.ordering
    assert len(all_rows) == len(set(all_rows)) == 6
    if mode == 'semantic':
        assert notices and all('Local model unavailable' in message for message in notices)
    else:
        assert notices == []


@pytest.mark.parametrize('changes', (
    dict(query='different'), dict(mode='word'), dict(limit=2),
    dict(result_mode='short'), dict(title='Source'), dict(language='en'),
    dict(page_budget=SearchPageBudget(pages=1)),
))
def test_cursor_cannot_change_query_filters_presentation_or_budget(corpus, changes):
    app, workspace, _, _, _ = corpus
    first = app.search_page(workspace, 'Needle', limit=1)
    options = dict(query='Needle', limit=1, cursor=first.next_cursor)
    options.update(changes)
    before = dict(app._selections)
    with pytest.raises(ValueError, match='another query'):
        app.search_page(workspace, **options)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Needle'


@pytest.mark.parametrize('token', ('', ' ', 'not-base64!', 'e30=', 'A' * 8193, 1))
def test_malformed_cursor_is_rejected_before_reading(corpus, monkeypatch, token):
    app, workspace, *_ = corpus
    def forbidden(*args, **kwargs):
        pytest.fail('Malformed cursor reached the database.')
    monkeypatch.setattr(app, '_search_read', forbidden)
    with pytest.raises(ValueError):
        app.search_page(workspace, 'Needle', cursor=token)


@pytest.mark.parametrize('change', ('metadata', 'membership', 'additional-source'))
def test_snapshot_change_refuses_continuation_without_retargeting_previous_source(corpus, change):
    app, workspace, collection, identities, _ = corpus
    first = app.search_page(workspace, 'Needle', collection=collection, limit=1)
    before = dict(app._selections)
    if change == 'metadata':
        app.update_metadata(workspace, identities[-1], title='Changed title')
    elif change == 'membership':
        with app.catalog_transaction(workspace) as catalog:
            catalog.remove(collection, identities[-1])
    else:
        app.add_text(workspace, 'A newly published source', 'Needle new source.',
                     collections=(collection,), work_id='new-work', edition_id='new-edition')
    with pytest.raises(ValueError, match='changed'):
        app.search_page(workspace, 'Needle', collection=collection, limit=1,
                        cursor=first.next_cursor)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Needle'


def test_cancel_and_work_budget_fail_without_withdrawing_completed_page(corpus, monkeypatch):
    app, workspace, *_ = corpus
    first = app.search_page(workspace, 'Needle', limit=1)
    before = dict(app._selections)
    with pytest.raises(ValueError, match='fingerprint'):
        app.search_page(workspace, 'Needle', page_budget=SearchPageBudget(fingerprint_rows=1))
    with pytest.raises(ValueError, match='character budget'):
        app.search_page(workspace, 'Needle', page_budget=SearchPageBudget(characters=1))
    stopped = Event()
    real = pagination._fingerprint
    def cancel_after_snapshot(*args, **kwargs):
        value = real(*args, **kwargs)
        stopped.set()
        return value
    monkeypatch.setattr(pagination, '_fingerprint', cancel_after_snapshot)
    with pytest.raises(TimeoutError):
        app.search_page(workspace, 'Needle', cursor=first.next_cursor, limit=1,
                        cancelled=stopped.is_set)
    assert stopped.is_set() and app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Needle'


def test_retained_original_is_rechecked_for_page_copy_and_save(corpus):
    app, workspace, _, _, digest = corpus
    topic = app.create_topic(workspace, 'Research')
    first = app.search_page(workspace, 'Needle', limit=1)
    hit = first.results[0]
    before = dict(app._selections)
    path = app.assets.resolve(digest)
    raw = path.read_bytes()
    try:
        path.write_bytes(raw.replace(b'Needle', b'Changed', 1))
        for action in (
            lambda: app.search_page(workspace, 'Needle', limit=1, cursor=first.next_cursor),
            lambda: app.copy(workspace, hit.ref, hit.revision, 'quote'),
            lambda: app.save_hit(workspace, topic, hit.ref, hit.revision),
        ):
            with pytest.raises(ValueError, match='retained asset'):
                action()
        assert app._selections == before
        assert app.evidence(workspace, topic) == ()
    finally:
        path.write_bytes(raw)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Needle'


def test_more_than_20_libraries_continue_in_explicit_order_with_only_current_handles(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    selected = tuple(app.create_workspace('Same displayed name') for _ in range(25))
    for workspace in selected:
        publish_text_corpus(app, workspace)
    service = FederatedSearch(app)
    cursor, found, previous = None, [], None
    for expected_number, workspace in enumerate(selected, 1):
        page = service.search_page(selected, 'Needle', limit_per_library=1, cursor=cursor)
        assert page.page_number == expected_number and page.total_libraries == 25
        assert page.workspaces == selected and len(page.hits) == 1
        hit = page.hits[0]
        assert hit.workspace == workspace and service.copy(hit, 'quote') == 'Needle'
        if previous is not None:
            with pytest.raises(ValueError, match='latest completed'):
                service.copy(previous, 'quote')
        found.append(hit.workspace)
        previous, cursor = hit, page.next_cursor
        assert app._selections == {}
    assert tuple(found) == selected and cursor is None


def test_empty_library_work_slices_continue_past_20_without_partial_authority(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    selected = tuple(app.create_workspace(f'Synthetic {i}') for i in range(23))
    publish_text_corpus(app, selected[-1])
    service = FederatedSearch(app)
    first = service.search_page(selected, 'Needle')
    assert first.hits == () and first.libraries_examined == 20 and first.next_cursor
    second = service.search_page(selected, 'Needle', cursor=first.next_cursor)
    assert [hit.workspace for hit in second.hits] == [selected[-1]]
    assert second.libraries_examined == 23 and second.next_cursor is None


@pytest.mark.parametrize('change', ('reorder', 'rename-later', 'new-source-later', 'archive-later'))
def test_federated_cursor_binds_all_selected_libraries_including_later_ones(tmp_path, change):
    app = LibraryApp(tmp_path / 'private')
    selected = tuple(app.create_workspace(f'Library {i}') for i in range(2))
    for workspace in selected:
        publish_text_corpus(app, workspace)
    service = FederatedSearch(app)
    first = service.search_page(selected, 'Needle', limit_per_library=1)
    original_session = service.current
    if change == 'reorder':
        selected = tuple(reversed(selected))
    elif change == 'rename-later':
        app.rename_workspace(selected[-1], 'Changed name')
    elif change == 'new-source-later':
        publish_text_corpus(app, selected[-1], prefix='additional')
    else:
        app.archive_workspace(selected[-1])
    with pytest.raises(ValueError):
        service.search_page(selected, 'Needle', limit_per_library=1, cursor=first.next_cursor)
    assert service.current is original_session
    assert service.copy(first.hits[0], 'quote') == 'Needle'


def test_federated_failure_preserves_host_cache_and_uses_bounded_scope_metadata(corpus):
    app, workspace, *_ = corpus
    local = app.search(workspace, 'Needle', limit=1)[0]
    before = dict(app._selections)
    service = FederatedSearch(app)
    page = service.search_page((workspace,), 'Needle', limit_per_library=1)
    with pytest.raises(ValueError, match='metadata budget'):
        service.search_page((workspace,), 'Needle', scope_budget_bytes=1)
    with pytest.raises(TimeoutError):
        service.search_page((workspace,), 'Needle', limit_per_library=1,
                            cursor=page.next_cursor, cancelled=lambda: True)
    assert service.current is page.session
    assert app._selections == before
    assert app.copy(workspace, local.ref, local.revision, 'quote') == 'Needle'


def test_completed_page_does_not_approve_a_cursor_as_a_quote_capability(corpus):
    app, workspace, _, _, _ = corpus
    first = app.search_page(workspace, 'Needle', limit=1)
    with pytest.raises(ValueError, match='Select a result'):
        app.copy(workspace, first.next_cursor, first.results[0].revision, 'quote')
    reopened = LibraryApp(app.root)
    second = reopened.search_page(workspace, 'Needle', limit=1, cursor=first.next_cursor)
    assert second.page_number == 2 and second.results[0].verified
    assert reopened.copy(workspace, second.results[0].ref, second.results[0].revision, 'quote') == 'Needle'


@pytest.mark.parametrize('separate_workspace', (False, True))
def test_unrelated_page_scope_retains_its_source_actions(corpus, separate_workspace):
    app, workspace, *_ = corpus
    first = app.search_page(workspace, 'Needle', limit=1)
    first_hit = first.results[0]
    other = app.create_workspace('Other independent page') if separate_workspace else workspace
    if separate_workspace:
        publish_text_corpus(app, other)
    query = 'Needle' if separate_workspace else 'sentence'
    independent = app.search_page(other, query, limit=1)
    independent_hit = independent.results[0]
    assert app.copy(workspace, first_hit.ref, first_hit.revision, 'quote') == 'Needle'
    assert app.copy(other, independent_hit.ref, independent_hit.revision, 'quote') == query
    following = app.search_page(workspace, 'Needle', limit=1, cursor=first.next_cursor)
    with pytest.raises(ValueError, match='Select a result'):
        app.copy(workspace, first_hit.ref, first_hit.revision, 'quote')
    assert following.results and app.copy(
        other, independent_hit.ref, independent_hit.revision, 'quote') == query
    assert len(app._page_selection_refs) == 2


@pytest.mark.parametrize('invalid', (
    dict(title='t' * 4001), dict(author='a' * 4001),
    dict(language='l' * 4001), dict(edition='e' * 4001),
    dict(workspace='w' * 201), dict(collection='c' * 201),
    dict(authorized_topic='q' * 201),
    dict(title=''), dict(author=''), dict(language=''), dict(edition=''),
))
def test_scope_validation_precedes_cursor_json_allocation(corpus, monkeypatch, invalid):
    app, workspace, *_ = corpus
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid scope was serialized before existing metadata/identity validation.')
    monkeypatch.setattr(pagination, 'binding', forbidden)
    options = {'workspace': workspace, 'query': 'Needle', **invalid}
    with pytest.raises(ValueError):
        app.search_page(**options)


def test_pre_cancelled_search_does_not_hash_scope_or_read_sources(corpus, monkeypatch):
    app, workspace, *_ = corpus
    def forbidden(*args, **kwargs):
        pytest.fail('Pre-cancelled search performed scope work.')
    monkeypatch.setattr(pagination, 'binding', forbidden)
    monkeypatch.setattr(app, '_search_read', forbidden)
    with pytest.raises(TimeoutError):
        app.search_page(workspace, 'Needle', cancelled=lambda: True)
