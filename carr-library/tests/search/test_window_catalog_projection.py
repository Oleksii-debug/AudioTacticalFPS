"""Real SQLite identity projection and unchanged source-window semantics.

This isolates catalog query work. VM counts are not full-corpus throughput,
and these generated catalog rows do not claim retained-import qualification.
"""
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.catalog import read_views
from carr_library.search import paging
from carr_library.storage.revisions import Page, Storage

MANIFEST = {'sha256': 'a' * 64, 'mime': 'text/plain', 'size': 8}
SCOPE = 'fresh-projection-scope'


@contextmanager
def snapshot(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        try:
            yield db, Catalog(db, 'main')
        finally:
            db.rollback()


def add(storage, identity, *, texts=None, workspace='main', work=None):
    catalog = Catalog(storage.db, workspace)
    if work is None:
        work = 'work-' + identity
        catalog.add_work(work, 'Title %_ Україна ' + identity,
                         ('Олена', 'literal %_ Ω', 'Žan'))
    catalog.add_edition(identity, work, MANIFEST, language='uk', edition='Ω', volume='I')
    if texts is not None:
        storage.stage_revision(workspace, identity, workspace + '-' + identity,
                               MANIFEST['sha256'], 'Generated SQL fixture',
                               (Page(text) for text in texts))


def count_vm(db, callback):
    steps = 0
    def progress():
        nonlocal steps
        steps += 1
        return 0
    db.set_progress_handler(progress, 1)
    try:
        value = callback()
    finally:
        db.set_progress_handler(None, 0)
    return value, steps


def old_window(db, catalog, cursor=None, **filters):
    """Independent public-view enumeration of the previous window wire."""
    current = cursor or {'schema': 'carr-source-search-cursor/1', 'scope': SCOPE,
                         'edition_after': None, 'after_page': None}
    views = read_views.browse_editions(catalog, after=current['edition_after'], limit=500, **filters)
    allowed = {value.edition_id: value.work_id for value in views}
    if not views:
        return paging.SourceWindow({}, (), current, None, sha256(b'[]').hexdigest(), 0)
    previous = current['after_page']
    prior = None if previous is None else (previous[0], previous[2], previous[3])
    rows = [tuple(row) for row in db.execute(
        'SELECT id,edition,revision,ordinal,length(raw),digest FROM active_page '
        'WHERE workspace=? ORDER BY edition COLLATE BINARY,ordinal,id', (catalog.workspace,))
        if row['edition'] in allowed and (prior is None
            or (row['edition'], row['ordinal'], row['id']) > prior)]
    chosen, characters = [], 0
    for row in rows:
        if len(chosen) == 100 or characters + row[4] > paging.MAX_SOURCE_CHARACTERS:
            break
        chosen.append(row)
        characters += row[4]
    if len(chosen) < len(rows):
        last = chosen[-1]
        following = {**current, 'after_page': [last[1], last[2], last[3], last[0]]}
    elif read_views.browse_editions(catalog, after=views[-1].edition_id, limit=1, **filters):
        following = {**current, 'edition_after': views[-1].edition_id, 'after_page': None}
    else:
        following = None
    digest = sha256(paging.canonical([SCOPE, tuple(allowed.items()), chosen]).encode('utf-8')).hexdigest()
    return paging.SourceWindow(allowed, tuple(row[0] for row in chosen), current,
                               following, digest, characters)


@pytest.fixture(scope='module')
def unicode_catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp('window-projection') / 'library.db'
    records = []
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.executemany('INSERT INTO workspace VALUES(?)', [('main',), ('other',)])
            catalog = Catalog(storage.db, 'main')
            catalog.create_collection('picked', 'Selected originals')
            for number, identity in enumerate(['Ω', 'A', 'e\u0301', 'é', 'Україна', "x' OR 1=1 --"]):
                add(storage, identity, texts=(identity + ' exact',))
                records.append({'id': identity, 'work': 'work-' + identity,
                    'title': 'Title %_ Україна ' + identity,
                    'authors': ('Олена', 'literal %_ Ω', 'Žan'),
                    'language': 'uk', 'edition': 'Ω', 'picked': number % 2 == 0})
                if number % 2 == 0:
                    catalog.assign('picked', identity)
            catalog.add_work('none', None)
            catalog.add_edition('no-metadata', 'none', MANIFEST)
            records.append({'id': 'no-metadata', 'work': 'none', 'title': None,
                'authors': (), 'language': None, 'edition': None, 'picked': False})
            catalog.add_work('wildcard-work', 'Title XY Україна', ('literal AB Ω',))
            catalog.add_edition('wildcard-decoy', 'wildcard-work', MANIFEST,
                                language='en', edition='Other edition')
            records.append({'id': 'wildcard-decoy', 'work': 'wildcard-work',
                'title': 'Title XY Україна', 'authors': ('literal AB Ω',),
                'language': 'en', 'edition': 'Other edition', 'picked': False})
            add(storage, 'A', texts=('Different workspace',), workspace='other')
            add(storage, 'foreign-only', texts=('Excluded workspace',), workspace='other')
        storage.check()
    return path, records


@pytest.mark.parametrize('filters', [
    {}, {'after': 'A'}, {'edition_id': 'é'}, {'collection': 'picked'},
    {'author': 'literal %_'}, {'title': '%_'}, {'language': 'uk'},
    {'edition': 'Ω'}, {'title': '%_', 'author': 'Олена', 'language': 'uk', 'edition': 'Ω'},
    {'edition_id': "' OR 1=1 --"},
])
def test_private_keys_are_exact_public_view_projection_with_literal_filters(unicode_catalog, filters):
    path, records = unicode_catalog
    def selected(row):
        return ((filters.get('after') is None or row['id'] > filters['after'])
            and (filters.get('edition_id') is None or row['id'] == filters['edition_id'])
            and (filters.get('collection') is None or row['picked'])
            and (filters.get('title') is None or row['title'] is not None and filters['title'] in row['title'])
            and (filters.get('author') is None or any(filters['author'] in name for name in row['authors']))
            and all(filters.get(key) is None or row[key] == filters[key] for key in ('language', 'edition')))
    independent = tuple((row['id'], row['work']) for row in sorted(records, key=lambda row: row['id'])
                        if selected(row))
    with snapshot(path) as (db, catalog):
        expected = read_views.browse_editions(catalog, limit=500, **filters)
        actual = read_views._edition_keys(catalog, limit=500, **filters)
        assert actual == independent == tuple((row.edition_id, row.work_id) for row in expected)
        assert [row[0] for row in actual] == sorted(row[0] for row in actual)
        assert 'foreign-only' not in dict(actual)
        assert db.total_changes == 0 and db.in_transaction


@pytest.fixture(scope='module')
def dense_authors(tmp_path_factory):
    path = tmp_path_factory.mktemp('projection-vm-work') / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            catalog = Catalog(storage.db, 'main')
            catalog.add_work('shared', 'One exact work', ('Author 00',))
            for number in range(500):
                add(storage, f'edition-{number:04d}', work='shared')
        with snapshot(path) as (db, catalog):
            before = count_vm(db, lambda: read_views._edition_keys(catalog, limit=500))
        with storage.transaction():
            storage.db.executemany('INSERT INTO catalog_author VALUES(?,?,?,?)',
                (('main', 'shared', number, f'Author {number:02d}') for number in range(1, 64)))
        storage.check()
        with snapshot(path) as (db, catalog):
            identities = count_vm(db, lambda: read_views._edition_keys(catalog, limit=500))
            metadata = count_vm(db, lambda: read_views.browse_editions(catalog, limit=500))
            filtered = count_vm(db, lambda: read_views._edition_keys(catalog, author='Author 63', limit=500))
    return before, identities, metadata, filtered


def test_identity_query_does_not_repeat_unused_author_fanout(dense_authors):
    before, identities, metadata, filtered = dense_authors
    assert len(identities[0]) == 500 and identities[0] == before[0]
    assert identities[0] == tuple((row.edition_id, row.work_id) for row in metadata[0])
    assert all(len(row.authors) == 64 for row in metadata[0])
    assert before[1] > 0 and identities[1] <= before[1] * 2 + 1000
    assert identities[1] * 5 < metadata[1], (identities[1], metadata[1])
    assert filtered[0] == identities[0] and filtered[1] > identities[1]


def test_identity_query_preserves_sqlite_cancellation_and_following_snapshot_read(unicode_catalog):
    with snapshot(unicode_catalog[0]) as (db, catalog):
        db.set_progress_handler(lambda: 1, 1)
        try:
            with pytest.raises(sqlite3.OperationalError, match='interrupted'):
                read_views._edition_keys(catalog, limit=500)
        finally:
            db.set_progress_handler(None, 0)
        assert read_views._edition_keys(catalog, limit=500)
        assert db.in_transaction and db.total_changes == 0


@pytest.mark.parametrize('shape', ['unfinished', 'exact', 'empty'])
def test_every_window_keeps_old_wire_and_only_necessary_next_catalog_lookup(tmp_path, monkeypatch, shape):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            for number in range(501):
                count = (205 if shape == 'unfinished' else 100 if shape == 'exact' else 0) if number == 0 else (3 if number == 500 else 0)
                add(storage, f'edition-{number:04d}',
                    texts=(f'Exact {number} / {page}' for page in range(count)) if count else None)
        calls, real = [], paging._edition_keys
        def observe(*args, **kwargs):
            calls.append((kwargs.get('after'), kwargs.get('limit')))
            return real(*args, **kwargs)
        monkeypatch.setattr(paging, '_edition_keys', observe)
        with snapshot(path) as (db, catalog):
            cursor, seen = None, []
            for iteration in range(5):
                expected = old_window(db, catalog, cursor)
                before = len(calls)
                actual = paging.select_window(db, catalog, scope=SCOPE, cursor=cursor)
                assert actual == expected
                invoked = calls[before:]
                assert invoked[0] == (actual.cursor['edition_after'], 500)
                continuing = actual.next_cursor is not None and actual.next_cursor['after_page'] is not None
                assert len(invoked) == (1 if continuing else 2)
                if not continuing:
                    assert invoked[-1] == (max(actual.editions), 1)
                seen.extend(actual.page_ids)
                cursor = actual.next_cursor
                if cursor is None:
                    break
            assert cursor is None and len(seen) == len(set(seen))
            assert len(seen) == {'unfinished': 208, 'exact': 103, 'empty': 3}[shape]
            assert db.in_transaction and db.total_changes == 0


def test_cancellation_after_deferred_lookup_returns_no_window_and_preserves_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            add(storage, 'one', texts=('Exact original',))
        state, real = {'peeked': False}, paging._edition_keys
        def observe(*args, **kwargs):
            result = real(*args, **kwargs)
            if kwargs.get('limit') == 1:
                state['peeked'] = True
            return result
        monkeypatch.setattr(paging, '_edition_keys', observe)
        def check():
            if state['peeked']:
                raise InterruptedError('cancel after actual next-key read')
        with snapshot(path) as (db, catalog):
            with pytest.raises(InterruptedError, match='actual next-key read'):
                paging.select_window(db, catalog, scope=SCOPE, check=check)
            assert state['peeked'] and db.in_transaction and db.total_changes == 0
            assert paging.select_window(db, catalog, scope=SCOPE) == old_window(db, catalog)


@pytest.mark.parametrize('mutation', ['membership', 'archive', 'revision'])
def test_new_snapshot_never_adopts_stale_catalog_or_active_continuation(tmp_path, mutation):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            catalog = Catalog(storage.db, 'main')
            catalog.create_collection('picked', 'Current selected source')
            add(storage, 'first', texts=(f'Original {n}' for n in range(105)))
            add(storage, 'last', texts=('Later original',))
            catalog.assign('picked', 'first')
            catalog.assign('picked', 'last')
        with snapshot(path) as (db, catalog):
            first = paging.select_window(db, catalog, scope=SCOPE, collection='picked')
            assert first == old_window(db, catalog, collection='picked')
            assert first.next_cursor['after_page'][0] == 'first'
        if mutation == 'revision':
            storage.publish_revision('main', 'first', 'new-active-revision', MANIFEST['sha256'],
                                     'Replacement SQL fixture', (Page('Changed current original'),))
        else:
            with storage.transaction():
                catalog = Catalog(storage.db, 'main')
                if mutation == 'membership':
                    assert catalog.remove('picked', 'first')
                else:
                    catalog.archive('picked')
        with snapshot(path) as (db, catalog):
            with pytest.raises(ValueError, match='changed|archived'):
                paging.select_window(db, catalog, scope=SCOPE, cursor=first.next_cursor, collection='picked')
            assert db.in_transaction and db.total_changes == 0


@pytest.mark.parametrize('invalid', ['no-transaction', 'foreign-keys-off', 'missing-workspace',
                                     'archived-collection', 'limit-zero', 'limit-large',
                                     'empty-filter', 'invalid-identity'])
def test_private_projection_preserves_public_authorization_and_input_refusals(tmp_path, invalid):
    path = tmp_path / 'library.db'
    with closing(Storage(path, schema_version=2)) as storage:
        with storage.transaction():
            storage.db.execute("INSERT INTO workspace VALUES('main')")
            catalog = Catalog(storage.db, 'main')
            catalog.create_collection('picked', 'Archived selection')
            add(storage, 'first')
            catalog.assign('picked', 'first')
            catalog.archive('picked')
        with snapshot(path) as (db, catalog):
            options = {}
            if invalid == 'no-transaction':
                db.rollback()
            elif invalid == 'foreign-keys-off':
                db.rollback()
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('BEGIN')
            elif invalid == 'missing-workspace':
                catalog.workspace = 'unknown'
            else:
                options = {
                    'archived-collection': {'collection': 'picked'}, 'limit-zero': {'limit': 0},
                    'limit-large': {'limit': 501}, 'empty-filter': {'title': ''},
                    'invalid-identity': {'edition_id': ''},
                }[invalid]
            with pytest.raises(ValueError) as old:
                read_views.browse_editions(catalog, **options)
            with pytest.raises(ValueError) as new:
                read_views._edition_keys(catalog, **options)
            assert str(new.value) == str(old.value)
            assert db.total_changes == 0
