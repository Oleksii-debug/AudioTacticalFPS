"""Actual pinned MiniLM inference, enabled with CARR_TEST_MODEL_DIR.

The model is prepared explicitly by the build operator; these tests do not
download files. No fixture score is presented as a held-out relevance study.
"""
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from carr_library.application import LibraryApp
from carr_library.semantic import APPROVED_MANIFEST, OfflineSemanticRanker, verify_model_bundle
from carr_library.search.semantic_windows import source_windows, TOKEN_LIMIT


@pytest.fixture(scope='module')
def ranker():
    directory = os.environ.get('CARR_TEST_MODEL_DIR')
    if not directory:
        pytest.skip('Set CARR_TEST_MODEL_DIR to the verified offline MiniLM bundle.')
    with patch('socket.socket.connect', side_effect=AssertionError('Network is forbidden during inference')), \
            patch('socket.create_connection', side_effect=AssertionError('Network is forbidden during inference')):
        import onnxruntime
        with patch('onnxruntime.disable_telemetry_events', wraps=onnxruntime.disable_telemetry_events) as disable:
            instance = OfflineSemanticRanker(directory)
            disable.assert_called_once()
        assert os.environ['HF_HUB_DISABLE_TELEMETRY'] == '1'
        assert os.environ['HF_HUB_OFFLINE'] == '1'
        yield instance


def test_actual_multilingual_ranker_and_complete_token_coverage(ranker):
    docs = ('Railway transport connected industrial cities.',
            'Залізниця з’єднувала промислові міста.',
            'Botanists studied flowers in a garden.',
            'У саду ботаніки досліджували квіти.')
    keys = tuple(('workspace', f'edition-{i}', 'revision', 'page') for i in range(4))
    ranked = ranker('How did trains link manufacturing towns?', tuple(zip(keys, docs)))
    assert set(ranked[:2]) == set(keys[:2])
    assert ranker.tokenizer.truncation is None and ranker.tokenizer.padding is None
    with pytest.raises(ValueError, match='complete token budget'):
        ranker('train', ((keys[0], 'word ' * 500),))


def test_actual_application_semantic_scope_and_original_quotes(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Research')
    foreign = app.create_workspace('Foreign')
    collection = app.create_collection(workspace, 'Selected')
    raw = 'Залізниця з’єднувала промислові міста.'
    imported = app.add_text(workspace, 'Railway', raw, collections=(collection,))
    app.add_text(workspace, 'Botany', 'Botanists studied flowers in a garden.')
    app.add_text(foreign, 'Excluded', 'Trains linked manufacturing towns in another private library.')
    hits = app.search(workspace, 'How did trains link manufacturing towns?', mode='semantic',
                      collection=collection)
    assert len(hits) == 1 and hits[0].quote == raw and hits[0].verified
    hit = hits[0]
    assert 'MiniLM' in hit.reason
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == raw
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, imported['edition'])
    assert app.search(workspace, 'trains manufacturing towns', mode='semantic', collection=collection) == ()
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_long_source_tail_is_not_silently_truncated(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Long documents')
    raw = ('Botanists studied flowers in a garden. ' * 80
           + '\r\nThe distinct railway archive describes trains connecting industrial cities.')
    windows = source_windows(ranker.tokenizer, raw)
    assert len(windows) > 1
    assert windows[0][0] == 0 and windows[-1][1] == len(raw)
    assert ''.join(raw[start:end] for start, end in windows) == raw
    assert all(len(ranker.tokenizer.encode(raw[start:end]).ids) <= TOKEN_LIMIT
               for start, end in windows)
    app.add_text(workspace, 'Long source', raw)
    hit, = app.search(workspace, 'distinct railway archive', mode='semantic')
    assert 'distinct railway archive' in hit.quote
    assert hit.quote in raw and app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert app.context(workspace, hit.ref, hit.revision).pages[0].anchor.start > 2000


def test_overlong_query_has_explicit_lexical_fallback(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Limits')
    app.add_text(workspace, 'Source', 'x is an exact source symbol.')
    hit, = app.search(workspace, 'x ' * 150, mode='semantic')
    assert 'using lexical thematic search' in hit.reason
    assert hit.quote == 'x is an exact source symbol.'
    with pytest.raises(TimeoutError):
        app.search(workspace, 'source', mode='semantic', cancelled=lambda: True)


def test_manifest_cannot_approve_another_model(ranker):
    changed = deepcopy(APPROVED_MANIFEST)
    changed['hashes']['model_optimized.onnx'] = 'a' * 64
    with pytest.raises(ValueError, match='not the approved'):
        verify_model_bundle(ranker.directory, changed)


def test_linked_model_directory_is_rejected(ranker, tmp_path):
    link = tmp_path / 'alias'
    try:
        link.symlink_to(ranker.directory, target_is_directory=True)
    except OSError:
        pytest.skip('This host cannot create a directory symlink.')
    with pytest.raises(ValueError, match='unlinked'):
        verify_model_bundle(link)


def test_actual_persistent_embeddings_reopen_without_reinference(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'cache-library', semantic_ranker=ranker)
    workspace = app.create_workspace('Persistent semantic cache')
    collection = app.create_collection(workspace, 'Authorized sources')
    raw = 'Залізниця з’єднувала промислові міста.'
    imported = app.add_text(workspace, 'Railway history', raw, collections=(collection,))
    query = 'How did trains link manufacturing towns?'
    first = app.search(workspace, query, mode='semantic', collection=collection)
    assert len(first) == 1 and first[0].quote == raw
    assert ranker.cache_statistics['embedded_texts'] >= 2
    reopened = LibraryApp(app.root, semantic_ranker=ranker)
    with patch.object(ranker.engine, 'embed', side_effect=AssertionError('Cache hit must not repeat inference')):
        again = reopened.search(workspace, query, mode='semantic', collection=collection)
        assert len(again) == 1 and again[0].quote == raw and again[0].verified
        assert ranker.cache_statistics['embedded_texts'] == 0
        assert ranker.cache_statistics['hits'] >= 2
        with reopened.catalog_transaction(workspace) as catalog:
            catalog.remove(collection, imported['edition'])
        assert reopened.search(workspace, query, mode='semantic', collection=collection) == ()
        with pytest.raises(ValueError):
            reopened.copy(workspace, again[0].ref, again[0].revision, 'quote')



def test_actual_semantic_snapshot_survives_concurrent_membership_revocation(ranker, tmp_path):
    """A committed writer during inference must not mix search snapshots.

    The first result is from the original authorized read transaction; its
    handle must not grant fresh access after the collection membership changes.
    This exercises actual inference, not a substitute ranker.
    """
    import sqlite3
    from contextlib import closing

    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Selected library')
    foreign = app.create_workspace('Excluded library')
    collection = app.create_collection(workspace, 'Selected')
    raw = 'Залізниця з’єднувала промислові міста.'
    imported = app.add_text(workspace, 'Railway', raw, collections=(collection,))
    app.add_text(workspace, 'Not in collection', 'Railways connected manufacturing towns.')
    app.add_text(foreign, 'Foreign source', 'Trains linked industrial cities.')
    with closing(sqlite3.connect(app.database, isolation_level=None)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'

    actual_rank = ranker.rank
    calls = []

    def rank_and_revoke(query, candidates, **kwargs):
        assert not calls
        assert len(candidates) == 1 and candidates[0][1] == raw
        ranked = actual_rank(query, candidates, **kwargs)
        assert ranked == [candidates[0][0]], 'Actual model must produce the expected hit.'
        # catalog_transaction opens a separate writer connection. WAL allows
        # commit while the original search reader remains in its snapshot.
        with app.catalog_transaction(workspace) as catalog:
            assert catalog.remove(collection, imported['edition'])
        calls.append(True)
        return ranked

    query = 'How did trains link manufacturing towns?'
    with patch.object(ranker, 'rank', side_effect=rank_and_revoke):
        hit, = app.search(workspace, query, mode='semantic', collection=collection)
    assert calls == [True]
    assert hit.quote == raw and hit.verified and 'MiniLM' in hit.reason
    # The old snapshot is coherent, but membership is freshly authorized on
    # every action. Neither copying nor opening context may use stale scope.
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError):
        app.context(workspace, hit.ref, hit.revision)
    assert app.search(workspace, query, mode='semantic', collection=collection) == ()
    # Removing membership is not deleting the source.
    fresh, = app.search(workspace, query, mode='semantic', title='Railway')
    assert fresh.quote == raw and app.copy(workspace, fresh.ref, fresh.revision, 'quote') == raw


def test_actual_full_scope_semantic_search_scores_more_than_500_sources(ranker, tmp_path):
    """Real pinned MiniLM batches; exact sources/order, not a relevance study."""
    from hashlib import sha256
    from carr_library.search.paging import SearchResources

    verify_model_bundle(ranker.directory)
    app = LibraryApp(tmp_path / "full-scope-library", semantic_ranker=ranker)
    workspace = app.create_workspace("More than one model candidate batch")
    foreign = app.create_workspace("Never selected")
    query = "railway transport"
    raw_pages = tuple(f"Railway transport linked industrial cities. Exact source {number:04d}."
                      for number in range(607))
    raw = "\f".join(raw_pages)
    saved = app.add_text(workspace, "607 retained source segments", raw, edition_id="selected")
    app.add_text(foreign, "Excluded original", "Railway transport contains PRIVATE_UNSELECTED_MARKER.")
    retained = app.assets.resolve(saved["asset"])
    original_sha = sha256(retained.read_bytes()).hexdigest()
    batches, scored, native_batches = [], set(), []
    actual_scores, actual_embed = ranker.scores, ranker.engine.embed

    def observe_scores(text, candidates, **kwargs):
        assert text == query and 0 < len(candidates) <= 500
        batches.append(len(candidates))
        for key, source in candidates:
            assert key[0] == workspace and key[1] == "selected"
            assert key not in scored and source in raw_pages
            assert "PRIVATE_UNSELECTED_MARKER" not in source
            scored.add(key)
        return actual_scores(text, candidates, **kwargs)

    def observe_native(texts, *args, **kwargs):
        native_batches.append(tuple(texts))
        assert 0 < len(texts) <= 16
        return actual_embed(texts, *args, **kwargs)

    def traverse(current):
        cursor, offset, result, pages = None, 0, [], []
        while True:
            page = current.search_page(workspace, query, mode="semantic", ranking_scope="all",
                cursor=cursor, offset=offset, limit=100, resources=SearchResources(timeout=300))
            assert page.ranking_scope == "all" and page.next_source_cursor is None
            assert page.total_results == page.source_pages == 607 and page.source_editions == 1
            assert all(row.workspace == workspace and row.edition_id == "selected"
                       and row.verified and row.quote in raw_pages for row in page.results)
            pages.append(page)
            result.extend(page.results)
            if not page.has_more_results:
                break
            cursor, offset = page.cursor, offset + 100
        assert len(pages) == 7 and len(result) == len({row.page for row in result}) == 607
        return result

    reopened = None
    try:
        with patch.object(ranker, "scores", side_effect=observe_scores), \
                patch.object(ranker.engine, "embed", side_effect=observe_native):
            cold = traverse(app)
        assert batches == [500, 107] and len(scored) == 607
        assert native_batches and sum(map(len, native_batches)) >= 608
        assert any("hybrid RRF" in row.reason for row in cold)
        assert {row.quote for row in cold} == set(raw_pages)
        for row in (cold[0], cold[-1]):
            assert app.copy(workspace, row.ref, row.revision, "quote") == row.quote
        assert sha256(retained.read_bytes()).hexdigest() == original_sha
        assert app.close_search()
        reopened = LibraryApp(app.root, semantic_ranker=ranker)
        with patch.object(ranker.engine, "embed", side_effect=AssertionError("Warm exact source cache repeated inference.")):
            warm = traverse(reopened)
        assert [(row.page, row.quote) for row in warm] == [(row.page, row.quote) for row in cold]
        assert sha256(retained.read_bytes()).hexdigest() == original_sha
        verify_model_bundle(ranker.directory)
    finally:
        assert app.close_search()
        if reopened is not None:
            assert reopened.close_search()
