"""Complete scoped order, actual retained originals, and bounded model batches.

The deterministic embedding fixture exercises the real OfflineSemanticRanker
control/cache/scoring implementation. It is explicitly not MiniLM quality or a
large-corpus performance measurement; native model evidence is a separate gate.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import os
from pathlib import Path
import runpy
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.search.paging import SearchResources
from carr_library.search import ranked_scope
from carr_library.search.semantic_windows import TOKEN_LIMIT, iter_source_windows, source_windows


def ranker_fixture(root):
    fixture = runpy.run_path(str(Path(__file__).with_name("test_semantic_cache.py")))
    return fixture["fixture_ranker"](root)


def all_pages(app, workspace, query, *, mode="thematic", limit=17, **options):
    cursor, offset, results, pages = None, 0, [], []
    while True:
        page = app.search_page(workspace, query, mode=mode, ranking_scope="all",
                               cursor=cursor, offset=offset, limit=limit, **options)
        assert page.ranking_scope == "all" and page.next_source_cursor is None
        assert len(page.results) <= limit
        assert page.total_results is not None
        pages.append(page)
        results.extend(page.results)
        if not page.has_more_results:
            break
        cursor, offset = page.cursor, offset + limit
    assert len(results) == pages[-1].total_results
    return results, pages


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / "library")
    workspace = app.create_workspace("Selected library")
    yield app, workspace
    assert app.close_search()


def test_full_scope_bm25_is_exact_existing_order_before_page_limit(library):
    app, workspace = library
    # The highest relevance is deliberately after the first source window.
    bodies = ["Railway " + "ordinary " * (number % 7 + 1) for number in range(140)]
    bodies[137] = "Railway railway railway."
    app.add_text(workspace, "Source A", "\f".join(bodies), edition_id="a", work_id="one")
    app.add_text(workspace, "Source B", "Railway bridge archive.", edition_id="b", work_id="two")
    app.add_text(workspace, "Source C", "An unrelated botanical account.", edition_id="c")
    expected = app.search(workspace, "railway", mode="thematic", limit=100)
    before = app.database.read_bytes()
    results, pages = all_pages(app, workspace, "railway", limit=23)
    assert [(row.edition_id, row.page, row.quote) for row in results[:100]] == [
        (row.edition_id, row.page, row.quote) for row in expected]
    assert len(results) == 141 and len(pages) == 7
    assert pages[0].source_pages == 142 and pages[0].source_editions == 3
    assert all(row.verified and app.copy(workspace, row.ref, row.revision, "quote") == row.quote for row in results)
    assert app.database.read_bytes() == before
    assert pages[0].index_bytes > 0 and pages[0].updated_index_rows == 142
    assert len(app._selections) <= 2000


def test_other_libraries_and_unselected_collection_rows_do_not_change_scores(library):
    app, workspace = library
    collection = app.create_collection(workspace, "Only these originals")
    first = app.add_text(workspace, "Included A", "Needle needle archive.", collections=(collection,))
    app.add_text(workspace, "Included B", "Needle ordinary ordinary source.", collections=(collection,))
    page = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", collection=collection)
    order = next(iter(app._search_cursor_owner._ranked.values()))
    with closing(sqlite3.connect(order.path)) as db:
        expected_scores = db.execute("SELECT page_id,score FROM result ORDER BY position").fetchall()
    expected = [(row.edition_id, row.quote) for row in page.results]
    assert app.close_search()
    foreign = app.create_workspace("Never selected")
    app.add_text(foreign, "Foreign", "\f".join("Needle " * 100 for _ in range(20)))
    app.add_text(workspace, "Not in collection", "\f".join("Needle " * 100 for _ in range(20)))
    again = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", collection=collection)
    current = next(iter(app._search_cursor_owner._ranked.values()))
    with closing(sqlite3.connect(current.path)) as db:
        assert db.execute("SELECT page_id,score FROM result ORDER BY position").fetchall() == expected_scores
    assert [(row.edition_id, row.quote) for row in again.results] == expected
    assert again.source_pages == 2 and again.updated_index_rows == 0
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, first["edition"])
    with pytest.raises(ValueError):
        app.copy(workspace, page.results[0].ref, page.results[0].revision, "quote")


def test_persistent_fts_updates_only_new_rows_after_reopen(library):
    app, workspace = library
    app.add_text(workspace, "First", "Needle first.\fNeedle second.", edition_id="a")
    first = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    assert first.updated_index_rows == 2
    assert app.close_search()
    cache_files = list((app.root / "search-cache").glob("scope-v1-*.sqlite"))
    assert len(cache_files) == 1
    with closing(sqlite3.connect(cache_files[0])) as db:
        assert db.execute("SELECT count(*) FROM source").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM scoped").fetchone()[0] == 2
    reopened = LibraryApp(app.root)
    try:
        again = reopened.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
        assert again.updated_index_rows == 0 and again.source_pages == 2
        assert reopened.close_search()
        reopened.add_text(workspace, "Third", "Needle third.", edition_id="b")
        added = reopened.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
        assert added.updated_index_rows == 1 and added.source_pages == 3
        assert [row.quote for row in added.results] == ["Needle first.", "Needle third.", "Needle second."]
    finally:
        assert reopened.close_search()


@pytest.mark.parametrize("damage", ["bad_sqlite", "extra_trigger", "raw_cache_text", "fts_only"])
def test_derived_cache_is_revalidated_or_rebuilt_without_authorizing_foreign_text(library, damage):
    app, workspace = library
    app.add_text(workspace, "Actual", "Needle original source.")
    initial = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    assert app.close_search()
    cache, = (app.root / "search-cache").glob("scope-v1-*.sqlite")
    originals = app.database.read_bytes()
    if damage == "bad_sqlite":
        cache.write_bytes(b"Not a database.")
    else:
        with sqlite3.connect(cache) as db:
            page_id, text = db.execute("SELECT id,text FROM source").fetchone()
            if damage == "extra_trigger":
                db.execute("CREATE TRIGGER unexpected AFTER INSERT ON source BEGIN DELETE FROM source; END")
            elif damage == "raw_cache_text":
                db.execute("INSERT INTO scoped(scoped,rowid,text) VALUES('delete',?,?)", (page_id, text))
                db.execute("UPDATE source SET text='Foreign cache assertion.' WHERE id=?", (page_id,))
                db.execute("INSERT INTO scoped(rowid,text) VALUES(?,?)", (page_id, "Foreign cache assertion."))
            else:
                db.execute("INSERT INTO scoped(scoped,rowid,text) VALUES('delete',?,?)", (page_id, text))
    result = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    assert [row.quote for row in result.results] == ["Needle original source."]
    assert result.updated_index_rows == 1
    assert app.copy(workspace, result.results[0].ref, result.results[0].revision, "quote") == initial.results[0].quote
    assert app.database.read_bytes() == originals


@pytest.mark.parametrize("change", ["query", "mode", "metadata_filter", "memory", "scope_kind", "model"])
def test_global_cursor_binds_query_ranker_and_resource_contract(library, change):
    app, workspace = library
    app.add_text(workspace, "Many", "\f".join("Needle source." for _ in range(4)))
    first = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", limit=1)
    options = dict(mode="thematic", ranking_scope="all", limit=1, offset=1)
    query = "needle"
    if change == "query":
        query = "source"
    elif change == "mode":
        options["mode"] = "semantic"
    elif change == "metadata_filter":
        options["title"] = "Many"
    elif change == "memory":
        options["resources"] = SearchResources(memory_bytes=128 * 1024**2)
    elif change == "scope_kind":
        options["ranking_scope"] = "window"
    else:
        app.semantic_ranker = ranker_fixture(app.root)
    before = dict(app._selections)
    with pytest.raises(ValueError, match="scope|cursor|model"):
        app.search_page(workspace, query, cursor=first.cursor, **options)
    assert app._selections == before


def test_next_global_page_rechecks_active_collection_and_database_commit(library):
    app, workspace = library
    collection = app.create_collection(workspace, "Selected")
    saved = app.add_text(workspace, "Many", "\f".join("Needle source." for _ in range(4)),
                         collections=(collection,))
    first = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", limit=1,
                            collection=collection)
    before = dict(app._selections)
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, saved["edition"])
    with pytest.raises(ValueError, match="changed|cursor"):
        app.search_page(workspace, "needle", mode="thematic", ranking_scope="all",
                        collection=collection, cursor=first.cursor, offset=1)
    assert app._selections == before
    assert not list((app.root / "search-cache").glob(".active-*"))


def test_cancellation_after_real_fts_index_keeps_old_actions_and_discards_private_order(library, monkeypatch):
    app, workspace = library
    app.add_text(workspace, "Source", "Needle source.")
    earlier = app.search(workspace, "needle")
    before = dict(app._selections)
    actual = ranked_scope._lexical
    stop = Event()

    def ranked_then_cancel(*args, **kwargs):
        value = actual(*args, **kwargs)
        stop.set()
        return value
    monkeypatch.setattr(ranked_scope, "_lexical", ranked_then_cancel)
    with pytest.raises(TimeoutError):
        app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", cancelled=stop.is_set)
    assert app._selections == before
    assert app.copy(workspace, earlier[0].ref, earlier[0].revision, "quote") == "Needle"
    assert not app._search_cursor_owner._ranked
    assert not list((app.root / "search-cache").glob(".active-*"))


def test_close_during_real_global_work_retains_one_owner_until_worker_return(library, monkeypatch):
    app, workspace = library
    app.add_text(workspace, "Source", "Needle source.")
    entered, release = Event(), Event()
    actual = ranked_scope._lexical

    def held(*args, **kwargs):
        value = actual(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return value
    monkeypatch.setattr(ranked_scope, "_lexical", held)
    before = dict(app._selections)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(app.search_page, workspace, "needle", mode="thematic", ranking_scope="all")
        try:
            assert entered.wait(10)
            owner = app._search_cursor_owner
            assert app.close_search() is False and owner.closed
            with pytest.raises(RuntimeError, match="releasing"):
                app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
        finally:
            release.set()
        with pytest.raises((TimeoutError, ValueError)):
            future.result(timeout=10)
    assert app._search_cursor_owner is owner and owner._db is None
    assert app._selections == before
    assert not list((app.root / "search-cache").glob(".active-*"))


@pytest.mark.parametrize("change", ["file", "filter"])
def test_private_order_cannot_be_replaced_or_escape_current_catalog_filter(library, change):
    app, workspace = library
    app.add_text(workspace, "Included", "Needle admitted source.\fNeedle second page.", edition_id="a")
    app.add_text(workspace, "Excluded", "Needle foreign filtered source.", edition_id="b")
    page = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", title="Included", limit=1)
    order = next(iter(app._search_cursor_owner._ranked.values()))
    before = dict(app._selections)
    if change == "file":
        copied = order.directory / "replacement"
        retained = order.path.read_bytes()
        copied.write_bytes(retained)
        try:
            os.replace(copied, order.path)
        except PermissionError:
            # Windows may refuse replacing the actually retained SQLite handle.
            # That is an observed native refusal, never simulated acceptance.
            assert os.name == "nt" and order.path.read_bytes() == retained
            following = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all",
                title="Included", cursor=page.cursor, offset=1)
            assert following.results and all(row.edition_id == "a" for row in following.results)
            assert copied.read_bytes() == retained
            return
    else:
        with app.read(workspace) as db:
            wrong = db.execute("SELECT id FROM active_page WHERE workspace=? AND edition='b'", (workspace,)).fetchone()[0]
        with sqlite3.connect(order.path) as db:
            db.execute("UPDATE result SET page_id=? WHERE position=2", (wrong,))
        # Even an injected trusted-looking file stamp cannot substitute catalog
        # authority. This exercises the independent fresh filter check itself.
        order._result_stamp = ranked_scope._stamp(order.path.stat())
    with pytest.raises(ValueError, match="order changed|selected catalog scope"):
        app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", title="Included",
                        cursor=page.cursor, offset=1)
    assert app._selections == before


def test_large_offset_does_not_allocate_or_overflow_sqlite(library):
    app, workspace = library
    app.add_text(workspace, "Actual", "Needle source.")
    first = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    huge = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all",
                           cursor=first.cursor, offset=10**100)
    assert huge.results == () and not huge.has_more_results and huge.total_results == 1


def test_selected_disk_allowance_refuses_incomplete_global_order(library):
    app, workspace = library
    raw = "\f".join("Needle " + "ordinary " * 12_000 for _ in range(80))
    app.add_text(workspace, "Actual bounded pages", raw)
    before = dict(app._selections)
    with pytest.raises(ValueError, match="disk allowance"):
        app.search_page(workspace, "needle", mode="thematic", ranking_scope="all",
                        resources=SearchResources(memory_bytes=16 * 1024**2, disk_bytes=16 * 1024**2))
    assert app._selections == before
    assert not list((app.root / "search-cache").glob(".active-*"))


@pytest.mark.parametrize("accepted", [True, False])
def test_actual_ranker_small_scope_matches_existing_hybrid_and_excerpt_behavior(tmp_path, accepted):
    ranker = ranker_fixture(tmp_path)
    ranker.minimum_similarity = 0.0 if accepted else 1.0
    app = LibraryApp(tmp_path / "library", semantic_ranker=ranker)
    workspace = app.create_workspace("Small exact hybrid oracle")
    app.add_text(workspace, "One", "ordinary " * 300 + "The distinct railway archive.",
                 edition_id="a", work_id="one")
    app.add_text(workspace, "Another edition", "A railway source.", edition_id="b", work_id="one")
    app.add_text(workspace, "Another work", "Railway bridge source.", edition_id="c", work_id="two")
    try:
        old = app.search(workspace, "distinct railway archive", mode="semantic")
        new, pages = all_pages(app, workspace, "distinct railway archive", mode="semantic", limit=1)
        assert [(row.edition_id, row.quote) for row in new] == [(row.edition_id, row.quote) for row in old]
        assert all(row.verified and app.copy(workspace, row.ref, row.revision, "quote") == row.quote for row in new)
        assert pages[0].total_results == len(old)
    finally:
        assert app.close_search()


def test_more_than_500_model_candidates_are_all_scored_and_paged(tmp_path, monkeypatch):
    ranker = ranker_fixture(tmp_path)
    app = LibraryApp(tmp_path / "library", semantic_ranker=ranker)
    workspace = app.create_workspace("Complete model batches")
    original = "\f".join(f"Authored source {number:04d} contains original words." for number in range(607))
    app.add_text(workspace, "607 actual source pages", original)
    actual = ranker.scores
    batches, keys = [], set()

    def observed(query, candidates, **kwargs):
        batches.append(len(candidates))
        assert len(candidates) <= 500
        for key, text in candidates:
            assert key not in keys and text in original
            keys.add(key)
        return actual(query, candidates, **kwargs)
    monkeypatch.setattr(ranker, "scores", observed)
    try:
        results, pages = all_pages(app, workspace, "unmentioned query", mode="semantic", limit=100)
        assert batches == [500, 107]
        assert len(keys) == len(results) == len({row.quote for row in results}) == 607
        assert len(pages) == 7 and pages[0].source_pages == 607
        assert all(row.verified for row in results)
        assert all(len(call) <= 16 for call in ranker.engine.calls)
        assert len(app._selections) <= 2000
    finally:
        assert app.close_search()


def test_scores_preserve_rank_and_refuse_unbounded_candidate_consumption(tmp_path):
    ranker = ranker_fixture(tmp_path)
    candidates = tuple((("w", str(i), "r", "p"), f"Actual source {i}.") for i in range(4))
    scored = ranker.scores("Actual source", candidates)
    assert ranker.rank("Actual source", candidates) == [key for key, _ in scored]
    assert all(-1.000001 <= value <= 1.000001 for _, value in scored)
    consumed = []
    def unbounded():
        for i in range(10_000):
            consumed.append(i)
            yield (("w", str(i), "r", "p"), "Actual source.")
    with pytest.raises(ValueError, match="candidate budget"):
        ranker.scores("Actual source", unbounded())
    assert consumed == list(range(501))


def test_streamed_windows_cover_long_unicode_without_total_500_cap(tmp_path):
    tokenizer = ranker_fixture(tmp_path).tokenizer
    raw = ("Київ cafe\u0301 👩\u200d🔬 railway " * 20_000) + "Exact tail."
    spans = tuple(iter_source_windows(tokenizer, raw))
    assert len(spans) > 500
    assert spans[0][0] == 0 and spans[-1][1] == len(raw)
    assert "".join(raw[a:b] for a, b in spans) == raw
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    assert all(len(tokenizer.encode(raw[a:b]).ids) <= TOKEN_LIMIT for a, b in spans)
    short = "ordinary " * 300 + "Exact tail."
    assert tuple(iter_source_windows(tokenizer, short)) == source_windows(tokenizer, short)


def test_model_change_during_real_scoring_does_not_publish_a_page(tmp_path, monkeypatch):
    ranker = ranker_fixture(tmp_path)
    app = LibraryApp(tmp_path / "library", semantic_ranker=ranker)
    workspace = app.create_workspace("Stable local engine")
    app.add_text(workspace, "Source", "Needle source.")
    old = app.search(workspace, "needle")
    before = dict(app._selections)
    actual = ranker.scores
    def score_then_change(*args, **kwargs):
        result = actual(*args, **kwargs)
        ranker.minimum_similarity = 0.5
        return result
    monkeypatch.setattr(ranker, "scores", score_then_change)
    try:
        with pytest.raises(ValueError, match="model changed"):
            app.search_page(workspace, "needle", mode="semantic", ranking_scope="all")
        assert app._selections == before
        assert app.copy(workspace, old[0].ref, old[0].revision, "quote") == "Needle"
        assert not list((app.root / "search-cache").glob(".active-*"))
    finally:
        assert app.close_search()


def test_result_pages_use_the_same_completed_sqlite_handle(library, monkeypatch):
    app, workspace = library
    app.add_text(workspace, "Source", "Needle first.\fNeedle second.")
    page = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", limit=1)
    order = next(iter(app._search_cursor_owner._ranked.values()))
    connection = order._result_db
    assert connection is not None
    actual = sqlite3.connect
    attempts = []
    def no_result_reopen(database, *args, **kwargs):
        if str(database).split("?", 1)[0] in (str(order.path), order.path.as_uri()):
            attempts.append(str(database))
            pytest.fail("Pagination must not reopen the checked result pathname.")
        return actual(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", no_result_reopen)
    following = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all",
                                cursor=page.cursor, offset=1)
    assert following.results[0].quote == "Needle second."
    assert order._result_db is connection and not attempts
    assert app.close_search()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_actual_directory_cleanup_keeps_retry_owner_and_releases_guard(
        library, monkeypatch, interruption):
    app, workspace = library
    app.add_text(workspace, "Source", "Needle source.")
    app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    owner = app._search_cursor_owner
    order = next(iter(owner._ranked.values()))
    directory = order.directory
    directory_info = directory.stat()
    actual_unlink = os.unlink
    interrupted = []
    def interrupt_owned_unlink(path, *args, **kwargs):
        relative = str(path) == "results.sqlite" and kwargs.get("dir_fd") is not None
        if relative:
            info = os.fstat(kwargs["dir_fd"])
            relative = (info.st_dev, info.st_ino) == (directory_info.st_dev, directory_info.st_ino)
        target = Path(path) == directory / "results.sqlite" or relative
        if target and not interrupted:
            interrupted.append(str(path))
            assert order._result_db is None and directory.exists()
            raise interruption("Interrupted inside real owned-directory cleanup.")
        return actual_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", interrupt_owned_unlink)
        with pytest.raises(interruption):
            app.close_search()
    assert interrupted and owner.closed and directory.exists()
    cleanup = owner._cleanup_owner
    assert cleanup is not None and not cleanup.quiescent
    assert owner._guard.acquire(blocking=False)
    owner._guard.release()
    assert app.close_search() is True and cleanup.quiescent
    assert owner._db is None and not owner._ranked and not directory.exists()
    assert app.search_page(workspace, "needle", mode="thematic", ranking_scope="all").results


def test_failed_build_retains_an_interrupted_spool_cleanup(library, monkeypatch):
    app, workspace = library
    app.add_text(workspace, "Source", "Needle source.")
    before = dict(app._selections)
    real_close = ranked_scope.RankedScope.close
    interrupted, directories = [], []
    def failed_rank(*args, **kwargs):
        raise ValueError("Authored ranking failure before result publication.")
    def interrupt_first_close(order):
        directories.append(order.directory)
        if not interrupted:
            interrupted.append(True)
            assert order.directory.exists()
            raise KeyboardInterrupt("Authored interruption before actual spool close.")
        return real_close(order)
    with monkeypatch.context() as patch:
        patch.setattr(ranked_scope, "_lexical", failed_rank)
        patch.setattr(ranked_scope.RankedScope, "close", interrupt_first_close)
        with pytest.raises(ValueError, match="Authored ranking failure"):
            app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    owner = app._search_cursor_owner
    assert len(owner._ranked) == 1 and not owner._issued
    assert app._selections == before
    assert owner._guard.acquire(blocking=False)
    owner._guard.release()
    assert app.close_search()
    assert all(not path.exists() for path in directories)


def test_observer_close_error_after_active_worker_never_strands_admission(library, monkeypatch):
    app, workspace = library
    app.add_text(workspace, "Source", "Needle source.")
    entered, release = Event(), Event()
    actual_rank = ranked_scope._lexical
    def held(*args, **kwargs):
        answer = actual_rank(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return answer
    monkeypatch.setattr(ranked_scope, "_lexical", held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(app.search_page, workspace, "needle", mode="thematic", ranking_scope="all")
        try:
            assert entered.wait(10)
            owner = app._search_cursor_owner
            original = owner._db
            calls = []
            class Observer:
                def execute(self, *args, **kwargs):
                    return original.execute(*args, **kwargs)
                def close(self):
                    calls.append(True)
                    if len(calls) == 1:
                        raise OSError("Authored close failure on the actual held observer.")
                    original.close()
            owner._db = Observer()
            assert app.close_search() is False
        finally:
            release.set()
        with pytest.raises((TimeoutError, ValueError)):
            future.result(timeout=10)
    assert calls == [True] and owner._cleanup_owner.last_error is not None
    assert owner._guard.acquire(blocking=False)
    owner._guard.release()
    assert app.close_search() and calls == [True, True] and owner._db is None
