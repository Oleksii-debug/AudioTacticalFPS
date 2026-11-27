"""Real library/source/citation behavior across opaque continuation pages."""
from dataclasses import replace
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.search.pagination import CursorStore


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Paged source research')
    raw = '\f'.join(f'Original section {i}: needle evidence in Київ.' for i in range(137))
    source = app.add_text(workspace, 'Original source', raw, authors=('Synthetic author',))
    assert source['state'] == 'complete'
    return app, workspace, source, raw


def test_every_result_beyond_first_hundred_keeps_copy_and_source_identity(library):
    app, workspace, imported, raw = library
    cursor, found, pages = None, [], 0
    while True:
        page = app.search_page(workspace, 'needle', limit=17, cursor=cursor)
        assert 1 <= len(page) <= 17
        pages += 1
        for hit in page:
            assert hit.verified and hit.revision == imported['id']
            assert hit.workspace == workspace and hit.quote == 'needle'
            assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
            context = app.context(workspace, hit.ref, hit.revision).pages[0]
            assert context.raw_text in raw
            found.append((context.anchor.locator, context.anchor.start, context.anchor.end))
        if page.next_cursor is None:
            break
        assert page.next_cursor not in raw
        cursor = page.next_cursor
    assert pages == 9 and len(found) == len(set(found)) == 137
    assert app.assets.resolve(imported['asset']).read_bytes() == raw.encode('utf-8')


def test_legacy_search_contract_and_first_page_results_remain_equivalent(library):
    app, workspace, _, _ = library
    old = app.search(workspace, 'needle', limit=17)
    new = app.search_page(workspace, 'needle', limit=17)
    assert isinstance(old, tuple) and len(new) == len(old)
    assert [replace(hit, ref='') for hit in new] == [replace(hit, ref='') for hit in old]


@pytest.mark.parametrize('option,value', [
    ('query', 'evidence'), ('mode', 'word'), ('limit', 2),
    ('result_mode', 'short'), ('title', 'Original'),
])
def test_cursor_cannot_change_query_or_scope_options(library, option, value):
    app, workspace, _, _ = library
    page = app.search_page(workspace, 'needle', limit=1)
    options = dict(query='needle', limit=1, cursor=page.next_cursor)
    options[option] = value
    selections = dict(app._selections)
    with pytest.raises(ValueError, match='continuation'):
        app.search_page(workspace, **options)
    assert app._selections == selections


def test_tokens_are_not_authority_in_another_workspace_or_reopened_app(library):
    app, workspace, _, _ = library
    page = app.search_page(workspace, 'needle', limit=1)
    other = app.create_workspace('Other private scope')
    for target, scope in ((app, other), (LibraryApp(app.root), workspace)):
        with pytest.raises(ValueError, match='continuation'):
            target.search_page(scope, 'needle', limit=1, cursor=page.next_cursor)


@pytest.mark.parametrize('change', ['new-source', 'metadata', 'collection'])
def test_current_catalog_change_rejects_cursor_without_partial_selection(library, change):
    app, workspace, imported, _ = library
    options = {}
    if change == 'collection':
        collection = app.create_collection(workspace, 'Selected collection')
        with app.catalog_transaction(workspace) as catalog:
            catalog.assign(collection, imported['edition'])
        options['collection'] = collection
    first = app.search_page(workspace, 'needle', limit=1, **options)
    previous = dict(app._selections)
    if change == 'new-source':
        app.add_text(workspace, 'New source', 'An unrelated source is now in the selected scope.')
    elif change == 'metadata':
        with app.write(workspace) as db:
            db.execute('UPDATE catalog_work SET title=? WHERE workspace=?',
                       ('Reviewed source metadata', workspace))
    else:
        with app.catalog_transaction(workspace) as catalog:
            catalog.remove(collection, imported['edition'])
    with pytest.raises(ValueError, match='changed'):
        app.search_page(workspace, 'needle', limit=1, cursor=first.next_cursor, **options)
    assert app._selections == previous


def test_cancellation_during_attestation_preserves_cursor_and_prior_quote(library, monkeypatch):
    app, workspace, _, _ = library
    page = app.search_page(workspace, 'needle', limit=1)
    selections, cursors = dict(app._selections), dict(app._search_cursors._values)
    stopped = Event()
    actual = app._loaders
    def loaders(*args, **kwargs):
        load, metadata, attest = actual(*args, **kwargs)
        def cancelled_after_attestation(*values):
            result = attest(*values)
            stopped.set()
            return result
        return load, metadata, cancelled_after_attestation
    monkeypatch.setattr(app, '_loaders', loaders)
    with pytest.raises(TimeoutError):
        app.search_page(workspace, 'needle', limit=1, cursor=page.next_cursor,
                        cancelled=stopped.is_set)
    assert app._selections == selections and dict(app._search_cursors._values) == cursors
    monkeypatch.setattr(app, '_loaders', actual)
    assert app.copy(workspace, page[0].ref, page[0].revision, 'quote') == 'needle'
    assert app.search_page(workspace, 'needle', limit=1, cursor=page.next_cursor)[0].page != page[0].page


def test_token_table_is_bounded_and_evicted_cursor_requires_new_search(library):
    app, workspace, _, _ = library
    app._search_cursors = CursorStore(maximum=2)
    first = app.search_page(workspace, 'needle', limit=1)
    second = app.search_page(workspace, 'needle', limit=1, cursor=first.next_cursor)
    third = app.search_page(workspace, 'needle', limit=1, cursor=second.next_cursor)
    assert len(app._search_cursors._values) == 2
    with pytest.raises(ValueError, match='expired'):
        app.search_page(workspace, 'needle', limit=1, cursor=first.next_cursor)
    assert app.search_page(workspace, 'needle', limit=1, cursor=third.next_cursor)


def test_current_source_verification_is_still_required_after_first_page(library):
    app, workspace, imported, _ = library
    first = app.search_page(workspace, 'needle', limit=1)
    original = app.assets.resolve(imported['asset'])
    original.write_bytes(b'Tampered retained original.')
    with pytest.raises(ValueError):
        app.search_page(workspace, 'needle', limit=1, cursor=first.next_cursor)


@pytest.mark.parametrize('mode', ['thematic', 'semantic'])
def test_ranked_continuation_is_local_to_bounded_windows_and_keeps_source_attestation(library, mode):
    app, workspace, imported, original = library
    cursor, results, notices = None, [], []
    for _ in range(20):
        page = app.search_page(workspace, 'needle', mode=mode, limit=17, page_budget=17,
                               cursor=cursor, on_status=notices.append)
        assert page.scanned_segments <= 17 and len(page) <= 17
        assert 'local to each window' in page.ordering
        assert page.scope_editions == 1
        results.extend(page)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert cursor is None and len(results) == 137
    for row in (results[0], results[-1]):
        assert row.verified and row.revision == imported['id']
        assert app.copy(workspace, row.ref, row.revision, 'quote') == row.quote
        context = app.context(workspace, row.ref, row.revision).pages[0]
        assert context.raw_text in original
    assert app.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
    if mode == 'semantic':
        assert notices and all('keyword' in message for message in notices)
