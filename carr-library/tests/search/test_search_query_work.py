"""Real SQLite query-work and source-window compatibility regressions.

These use the existing Storage/Catalog APIs to isolate query planning. They
are not retained-book ingestion or 1k/10k/50k corpus benchmark evidence.
"""
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.catalog.read_views import browse_editions, get_edition
from carr_library.search import paging
from carr_library.storage.revisions import Page, Storage


MANIFEST = dict(sha256='a' * 64, mime='text/plain', size=8)
SCOPE = 'explicit-test-source-scope'


@contextmanager
def snapshot(path, workspace='main'):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        try:
            yield db, Catalog(db, workspace)
        finally:
            db.rollback()


def counted(db, operation):
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
        return 0
    db.set_progress_handler(progress, 1)
    try:
        result = operation()
    finally:
        db.set_progress_handler(None, 0)
    assert db.in_transaction
    return result, steps


def add(storage, edition, texts=None, *, workspace='main', selected=True):
    catalog = Catalog(storage.db, workspace)
    work = 'work-' + edition
    catalog.add_work(work, 'Selected source' if selected else 'Other source',
                     ('Олена', 'Žan') if selected else ('Other',))
    catalog.add_edition(edition, work, MANIFEST, language='uk' if selected else 'en',
                        edition='edition label')
    if texts is not None:
        storage.stage_revision(workspace, edition, workspace + '-' + edition,
                               MANIFEST['sha256'], 'synthetic SQL fixture',
                               (Page(text) for text in texts))


@pytest.fixture(scope='module')
def catalog_growth(tmp_path_factory):
    path = tmp_path_factory.mktemp('catalog-query-work') / 'library.db'
    operations = {
        'keyset': lambda catalog: browse_editions(catalog, after='z-before', limit=1),
        'identity': lambda catalog: (get_edition(catalog, 'z-target'),),
        'both': lambda catalog: browse_editions(
            catalog, after='z-before', edition_id='z-target', limit=1),
    }
    def measure():
        with snapshot(path) as (db, catalog):
            return {name: counted(db, lambda op=op: op(catalog))
                    for name, op in operations.items()}
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            catalog = Catalog(storage.db, 'main')
            catalog.add_work('work', 'Late exact title', ('Олена', 'Žan'))
            for number in range(100):
                catalog.add_edition(f'a-{number:05d}', 'work', MANIFEST)
            catalog.add_edition('z-target', 'work', MANIFEST)
        before = measure()
        with storage.transaction():
            catalog = Catalog(storage.db, 'main')
            for number in range(100, 20_100):
                catalog.add_edition(f'a-{number:05d}', 'work', MANIFEST)
        storage.check()
        after = measure()
    return before, after


@pytest.mark.parametrize('operation', ['keyset', 'identity', 'both'])
def test_catalog_keys_do_not_revisit_twenty_thousand_preceding_editions(
        catalog_growth, operation):
    before, after = catalog_growth
    small, small_steps = before[operation]
    large, large_steps = after[operation]
    assert small == large
    assert [item.edition_id for item in large] == ['z-target']
    assert large[0].authors == ('Олена', 'Žan')
    assert small_steps > 0
    # VM instructions, not elapsed time or a guessed query-plan string.
    assert large_steps <= small_steps * 2 + 2000, (small_steps, large_steps)


@pytest.fixture(scope='module')
def page_growth(tmp_path_factory):
    path = tmp_path_factory.mktemp('source-window-query-work') / 'library.db'
    def measure():
        with snapshot(path) as (db, catalog):
            first, first_steps = counted(
                db, lambda: paging.select_window(db, catalog, scope=SCOPE))
            continued, continued_steps = counted(
                db, lambda: paging.select_window(
                    db, catalog, scope=SCOPE, cursor=first.next_cursor))
            return {'first': (first, first_steps),
                    'continued': (continued, continued_steps)}
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            add(storage, 'a-selected', (f'Україна Ω exact {n}' for n in range(205)))
            add(storage, 'z-later', ('later source',))
        before = measure()
        storage.publish_revision(
            'main', 'z-later', 'later-expanded', MANIFEST['sha256'],
            'synthetic query-work growth',
            (Page(f'Unvisited later page {n}') for n in range(20_000)))
        storage.check()
        after = measure()
    return before, after


@pytest.mark.parametrize('window', ['first', 'continued'])
def test_window_work_is_independent_of_twenty_thousand_later_pages(page_growth, window):
    before, after = page_growth
    small, small_steps = before[window]
    large, large_steps = after[window]
    assert small == large
    assert len(large.page_ids) == paging.MAX_SOURCE_PAGES == 100
    assert tuple(large.editions) == ('a-selected', 'z-later')
    assert small_steps > 0
    assert large_steps <= small_steps * 2 + 2000, (small_steps, large_steps)


def reference_window(db, catalog, cursor=None, **filters):
    """Independent enumeration of the previous valid-database window wire."""
    current = cursor or {'schema': 'carr-source-search-cursor/1', 'scope': SCOPE,
                         'edition_after': None, 'after_page': None}
    views = browse_editions(catalog, after=current['edition_after'],
                            limit=500, **filters)
    allowed = {view.edition_id: view.work_id for view in views}
    if not views:
        return paging.SourceWindow({}, (), current, None, sha256(b'[]').hexdigest(), 0)
    previous = current['after_page']
    prior_key = None if previous is None else (previous[0], previous[2], previous[3])
    all_rows = [tuple(row) for row in db.execute(
        'SELECT id,edition,revision,ordinal,length(raw),digest FROM active_page '
        'WHERE workspace=? ORDER BY edition COLLATE BINARY,ordinal,id',
        (catalog.workspace,))
        if row['edition'] in allowed and (prior_key is None
            or (row['edition'], row['ordinal'], row['id']) > prior_key)]
    chosen, characters = [], 0
    for row in all_rows:
        if len(chosen) == 100 or characters + row[4] > paging.MAX_SOURCE_CHARACTERS:
            break
        chosen.append(row)
        characters += row[4]
    if len(chosen) < len(all_rows):
        last = chosen[-1]
        following = {**current, 'after_page': [last[1], last[2], last[3], last[0]]}
    elif browse_editions(catalog, after=views[-1].edition_id, limit=1, **filters):
        following = {**current, 'edition_after': views[-1].edition_id, 'after_page': None}
    else:
        following = None
    raw = json.dumps([SCOPE, tuple(allowed.items()), chosen], ensure_ascii=False,
                     sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return paging.SourceWindow(allowed, tuple(row[0] for row in chosen), current,
                               following, sha256(raw).hexdigest(), characters)


@pytest.mark.parametrize('filters', [
    {}, {'collection': 'picked'},
    {'title': 'Selected', 'author': 'Олена', 'language': 'uk', 'edition': 'edition label'},
])
def test_window_wire_matches_old_order_and_identity_across_unicode_and_filters(tmp_path, filters):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.executemany('INSERT INTO workspace VALUES(?)', [('main',), ('other',)])
            catalog = Catalog(storage.db, 'main')
            catalog.create_collection('picked', 'Explicit selection')
            # Deliberately insert out of BINARY order and retain empty editions.
            for edition, count, selected in [
                ('Ω', 7, True), ('a', 103, True), ('é', 2, False),
                ('A', 3, True), ('e\u0301', 4, True), ('Україна', 2, True),
                ('00-empty', None, True), ('z-empty', None, False),
            ]:
                add(storage, edition,
                    None if count is None else (f'{edition} original {n}' for n in range(count)),
                    selected=selected)
                if selected:
                    catalog.assign('picked', edition)
            add(storage, 'a', ('Excluded other workspace',), workspace='other')
        storage.check()
        with snapshot(path) as (db, catalog):
            current, seen = None, []
            for _ in range(5):
                wanted = reference_window(db, catalog, current, **filters)
                actual = paging.select_window(db, catalog, scope=SCOPE,
                                               cursor=current, **filters)
                assert actual == wanted
                seen.extend(actual.page_ids)
                current = actual.next_cursor
                if current is None:
                    break
            assert current is None
            assert len(seen) == len(set(seen))
            assert db.total_changes == 0



@pytest.mark.parametrize('later_edition', [False, True])
def test_exact_hundred_page_boundary_does_not_skip_or_invent_continuation(tmp_path, later_edition):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            add(storage, 'a-exact', (f'Exact {n}' for n in range(100)))
            add(storage, 'middle-empty')
            if later_edition:
                add(storage, 'z-next', ('Next edition original',))
        with snapshot(path) as (db, catalog):
            first = paging.select_window(db, catalog, scope=SCOPE)
            assert first == reference_window(db, catalog)
            assert len(first.page_ids) == 100
            if later_edition:
                assert first.next_cursor['after_page'][0] == 'a-exact'
                following = paging.select_window(db, catalog, scope=SCOPE, cursor=first.next_cursor)
                assert following == reference_window(db, catalog, first.next_cursor)
                assert len(following.page_ids) == 1 and following.next_cursor is None
                assert set(first.page_ids).isdisjoint(following.page_ids)
            else:
                assert first.next_cursor is None


def test_character_boundary_preserves_exact_cursor_and_window_digest(tmp_path, monkeypatch):
    path = tmp_path / 'library.db'
    assert paging.MAX_SOURCE_CHARACTERS == 5_000_000
    # A small boundary exercises the same actual SQLite lengths without
    # manufacturing a corpus-throughput or 5 MiB capacity claim.
    monkeypatch.setattr(paging, 'MAX_SOURCE_CHARACTERS', 23)
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            add(storage, 'a', ('α' * 11, 'Ω' * 12, 'tail'))
            add(storage, 'z-empty')
        with snapshot(path) as (db, catalog):
            first = paging.select_window(db, catalog, scope=SCOPE)
            assert first == reference_window(db, catalog)
            assert first.characters == 23 and len(first.page_ids) == 2
            following = paging.select_window(db, catalog, scope=SCOPE, cursor=first.next_cursor)
            assert following == reference_window(db, catalog, first.next_cursor)
            assert following.characters == 4 and following.next_cursor is None


def test_empty_catalog_window_still_advances_to_later_active_source(tmp_path):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            catalog = Catalog(storage.db, 'main')
            catalog.add_work('work', 'Empty catalog editions')
            for number in range(500):
                catalog.add_edition(f'a-{number:03d}', 'work', MANIFEST)
            add(storage, 'z-active', ('Retained later source',))
        with snapshot(path) as (db, catalog):
            first = paging.select_window(db, catalog, scope=SCOPE)
            assert first == reference_window(db, catalog)
            assert not first.page_ids and len(first.editions) == 500
            following = paging.select_window(db, catalog, scope=SCOPE, cursor=first.next_cursor)
            assert following == reference_window(db, catalog, first.next_cursor)
            assert len(following.page_ids) == 1 and following.next_cursor is None


@pytest.mark.parametrize('change', ['revision', 'ordinal', 'page_id'])
def test_keyed_continuation_still_rejects_stale_or_forged_page(tmp_path, change):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            add(storage, 'long', (f'Exact {n}' for n in range(102)))
        with snapshot(path) as (db, catalog):
            cursor = paging.select_window(db, catalog, scope=SCOPE).next_cursor
        if change == 'revision':
            storage.publish_revision('main', 'long', 'replacement', MANIFEST['sha256'],
                                     'changed extraction', [Page('Replacement')])
        else:
            cursor['after_page'][2 if change == 'ordinal' else 3] += 1
        with snapshot(path) as (db, catalog):
            with pytest.raises(ValueError, match='source changed'):
                paging.select_window(db, catalog, scope=SCOPE, cursor=cursor)
            assert db.total_changes == 0


def test_foreign_active_pointer_is_refused_instead_of_reinterpreted(tmp_path):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.executemany('INSERT INTO workspace VALUES(?)', [('main',), ('other',)])
            add(storage, 'selected', ('Selected text',))
            add(storage, 'selected', ('Foreign text',), workspace='other')
        # Actual external corruption bypasses the FK only in this negative.
        storage.db.execute('PRAGMA foreign_keys=OFF')
        storage.db.execute("UPDATE active SET revision='other-selected' WHERE workspace='main'")
        storage.db.execute('PRAGMA foreign_keys=ON')
        with snapshot(path) as (db, catalog):
            with pytest.raises(ValueError, match='active source'):
                paging.select_window(db, catalog, scope=SCOPE)
            assert db.total_changes == 0


def test_cancel_during_keyed_page_iteration_closes_cursor_and_keeps_snapshot(tmp_path):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            add(storage, 'long', (f'Exact {n}' for n in range(102)))
        with snapshot(path) as (db, catalog):
            checks = 0
            def cancelled():
                nonlocal checks
                checks += 1
                if checks == 8:
                    raise InterruptedError('selected source stopped')
            with pytest.raises(InterruptedError, match='selected source stopped'):
                paging.select_window(db, catalog, scope=SCOPE, check=cancelled)
            assert db.in_transaction and db.total_changes == 0
            complete = paging.select_window(db, catalog, scope=SCOPE)
            assert len(complete.page_ids) == 100 and complete.next_cursor is not None
