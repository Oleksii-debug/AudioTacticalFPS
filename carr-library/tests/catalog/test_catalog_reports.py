"""Catalog report checks with real version-3 SQLite sources and read snapshots.

These are service tests, not native desktop or screen-reader acceptance.
"""
from contextlib import closing, contextmanager
from hashlib import sha256
import sqlite3

import pytest

from carr_library.catalog import catalog_reports
from carr_library.catalog.catalog_reports import catalog_report
from carr_library.catalog.membership import Catalog
from carr_library.storage.revisions import Page, Storage


@contextmanager
def snapshot(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            yield db
        finally:
            db.rollback()


def add_edition(storage, workspace, edition, *, work=None, title='Book', authors=('Author',),
                digest='a' * 64, mime='application/pdf', size=100,
                language='en', label='1'):
    catalog = Catalog(storage.db, workspace)
    work = work or edition
    if not storage.db.execute('SELECT 1 FROM catalog_work WHERE workspace=? AND id=?',
                              (workspace, work)).fetchone():
        catalog.add_work(work, title, authors)
    catalog.add_edition(edition, work, dict(sha256=digest, mime=mime, size=size),
                        language=language, edition=label)


def record_review(storage, workspace, edition, revision, physical, raw, *, active=1):
    storage.db.execute('INSERT INTO source_page_review VALUES(?,?,?,?,?,?,?,?,?)',
        (workspace, edition, revision, physical, sha256(raw.encode()).hexdigest(),
         'f' * 64, 'synthetic-renderer', 'synthetic-reviewer', active))


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'catalog.sqlite'
    with closing(Storage(path, schema_version=3)) as storage:
        assert storage.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        with storage.transaction():
            for workspace, name in (('a', 'Main library'), ('b', 'Private library'), ('empty', 'Empty')):
                storage.db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
                storage.db.execute('INSERT INTO workspace_meta(workspace,name) VALUES(?,?)',
                                   (workspace, name))
            add_edition(storage, 'a', 'e1', title='Історія', authors=('Олена', 'Žan'), language='uk')
            add_edition(storage, 'a', 'e2', title='Second edition', language=None, label=None)
            add_edition(storage, 'a', 'e3', title=None, authors=(), digest='b' * 64,
                        mime='text/plain', size=10, language='\u2003', label=None)
            add_edition(storage, 'a', 'e4', title='Archived membership', digest='c' * 64,
                        mime='application/epub+zip', size=40, label=None)
            add_edition(storage, 'b', 'e1', title='Secret title', authors=('Private author',))
            for workspace in ('a', 'b'):
                catalog = Catalog(storage.db, workspace)
                catalog.create_collection('c', workspace + ' selected collection')
                catalog.assign('c', 'e1')
            catalog = Catalog(storage.db, 'a')
            catalog.assign('c', 'e2')
            catalog.create_collection('empty-c', 'Empty collection')
            catalog.create_collection('archived-c', 'Archived collection')
            catalog.assign('archived-c', 'e4')
            catalog.archive('archived-c')
            Catalog(storage.db, 'b').create_collection('private-c', 'Private collection')

            storage.stage_revision('a', 'e1', 'old-revision', 'a' * 64, 'synthetic',
                                   [Page('Historical text', 1, 'old', True)])
            record_review(storage, 'a', 'e1', 'old-revision', 1, 'Historical text')
            storage.db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                ('a', 'e1', 'old-revision', 'Old author', 'Old title', None, 'Old edition'))
            storage.stage_revision('a', 'e1', 'active-revision', 'a' * 64, 'synthetic',
                                   [Page('Current text', 1, 'i', True), Page(' \t\n', 2)])
            record_review(storage, 'a', 'e1', 'active-revision', 1, 'Current text')
            record_review(storage, 'a', 'e1', 'active-revision', 2, 'Different old text')
            storage.db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                ('a', 'e1', 'active-revision', 'Олена; Žan', 'Історія', None, '1'))
            storage.stage_revision('a', 'e3', 'text-revision', 'b' * 64, 'synthetic',
                                   [Page('Original plain text')])
        yield storage, path


def report(path, workspace='a', **kwargs):
    with snapshot(path) as db:
        return catalog_report(db, workspace, **kwargs)


def test_complete_counts_distinguish_publication_text_index_review_and_metadata(library):
    _, path = library
    result = report(path)
    for expected in (
        'Registered editions: 4.', 'Distinct works: 4.',
        'Distinct registered originals: 3.',
        'Registered original bytes, counted once per digest: 150.',
        'Active collections in the whole library: 2.',
        'Editions with an active published revision: 2.',
        'Editions without an active published revision: 2.',
        'Raw pages: 3.', 'Pages containing nonblank text: 2.',
        'Editions containing nonblank text: 2.',
        'Indexed active pages matching stored raw text: 3.',
        'Editions with every active raw page indexed: 2.',
        'Pages with recorded verified printed labels: 1.',
        'Pages with active recorded reviews matching stored text digests: 1.',
        'Active review records with mismatched stored text digests: 1.',
        'Active revisions with frozen citation metadata: 1.',
        'Editions registered as plain-text sources: 1.',
        'Editions missing a title: 1.', 'Editions missing authors: 1.',
        'Editions missing a language: 2.', 'Editions missing an edition label: 3.',
    ):
        assert expected in result
    assert 'They do not certify quote accuracy or citation eligibility.' in result
    assert 'validate review provenance' in result
    assert 'Historical text' not in result
    assert 'old-revision' not in result
    assert 'Old title' not in result


def test_ordered_unicode_authors_and_edition_identities_are_kept(library):
    _, path = library
    result = report(path)
    assert 'Title: "Історія".' in result
    assert 'Authors: "Олена"; "Žan".' in result
    assert 'Language: "uk"; edition: "1"; volume: Unknown.' in result
    assert 'Edition "e1"; work "e1".' in result
    assert result.index('Edition "e1"; work') < result.index('Edition "e2"; work')
    assert 'Secret title' not in result
    assert 'Private author' not in result
    assert 'Private library' not in result


def test_selected_collection_totals_and_duplicates_do_not_mix_other_sources(library):
    _, path = library
    result = report(path, collection='c')
    inventory = result.split('Source inventory (selected scope)\n', 1)[1].split(
        'Collection coverage (whole authorized library)', 1)[0]
    assert 'Registered editions: 2.' in result
    assert 'Distinct registered originals: 1.' in result
    assert 'Raw pages: 2.' in result
    assert 'Editions missing authors: 0.' in result
    assert 'Duplicate candidate groups: 1.' in result
    assert 'Editions in candidate groups: 2.' in result
    assert 'Edition "e3";' not in inventory and 'Edition "e4";' not in inventory
    assert 'Library editions outside the selected collection: first 2 of 2.' in result
    assert 'Secret title' not in result
    other = report(path, 'b', collection='c')
    assert 'Registered editions: 1.' in other
    assert 'Duplicate candidate groups: 0.' in other
    assert 'Історія' not in other


def test_archived_membership_counts_as_uncollected_but_active_membership_does_not(library):
    _, path = library
    result = report(path)
    coverage = result.split('Collection coverage (whole authorized library)\n', 1)[1].split(
        'Exact-byte duplicate candidates', 1)[0]
    assert 'Library editions in no active collection: first 2 of 2.' in coverage
    assert 'Edition "e3"' in coverage and 'Edition "e4"' in coverage
    assert 'Edition "e1"' not in coverage and 'Edition "e2"' not in coverage


def test_unknown_archived_or_other_library_collection_rejected(library):
    _, path = library
    for collection in ('missing', 'archived-c', 'private-c'):
        with pytest.raises(ValueError, match='unknown or archived collection'):
            report(path, collection=collection)


def test_empty_library_and_empty_collection_are_explicit_not_unknown(library):
    _, path = library
    empty = report(path, 'empty')
    assert 'Registered editions: 0.' in empty
    assert 'Raw pages: 0.' in empty
    assert 'Edition details: first 0 of 0.' in empty
    assert 'Groups shown: first 0 of 0.' in empty
    selected = report(path, collection='empty-c')
    assert 'Registered editions: 0.' in selected
    assert 'Library editions outside the selected collection: first 4 of 4.' in selected


def test_missing_or_archived_workspace_rejected_even_on_direct_service_call(library):
    storage, path = library
    with pytest.raises(ValueError, match='missing or archived'):
        report(path, 'unknown')
    with storage.transaction():
        storage.db.execute("UPDATE workspace_meta SET archived=1 WHERE workspace='a'")
    with pytest.raises(ValueError, match='missing or archived'):
        report(path)


def test_raw_index_mismatch_and_missing_index_are_not_counted_as_indexed(library):
    storage, path = library
    with storage.transaction():
        page_id = storage.db.execute("SELECT id FROM page WHERE revision='active-revision' "
                                     "AND ordinal=1").fetchone()[0]
        storage.db.execute('DELETE FROM indexed WHERE id=?', (page_id,))
        storage.db.execute('INSERT INTO indexed VALUES(?,?)', (page_id, 'Stale search row'))
        storage.db.execute("DELETE FROM indexed WHERE id IN "
                           "(SELECT id FROM page WHERE revision='text-revision')")
    result = report(path)
    assert 'Pages containing nonblank text: 2.' in result
    assert 'Indexed active pages matching stored raw text: 1.' in result
    assert 'Editions with every active raw page indexed: 0.' in result


def test_revoked_reviews_are_not_counted_and_report_never_writes(library):
    storage, path = library
    with storage.transaction():
        storage.db.execute("UPDATE source_page_review SET active=0 WHERE revision='active-revision'")
    with snapshot(path) as db:
        before = tuple(tuple(row) for row in db.execute('SELECT * FROM source_page_review'))
        result = catalog_report(db, 'a')
        after = tuple(tuple(row) for row in db.execute('SELECT * FROM source_page_review'))
        assert db.total_changes == 0
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        assert before == after
    assert 'Pages with active recorded reviews matching stored text digests: 0.' in result
    assert 'Active review records with mismatched stored text digests: 0.' in result


def test_live_writer_does_not_change_counts_mid_snapshot(library):
    storage, path = library
    with snapshot(path) as db:
        before = catalog_report(db, 'a')
        with storage.transaction():
            add_edition(storage, 'a', 'e5', title='Published later')
            Catalog(storage.db, 'a').archive('c')
        assert catalog_report(db, 'a') == before
        assert 'Registered editions: 2.' in catalog_report(db, 'a', collection='c')
    assert 'Registered editions: 5.' in report(path)
    with pytest.raises(ValueError, match='archived'):
        report(path, collection='c')


def test_large_catalog_counts_every_edition_without_active_collections(library):
    storage, path = library
    with storage.transaction():
        for i in range(10001):
            add_edition(storage, 'empty', f'e{i:05d}', work='one-work', title='Shared original')
    result = report(path, 'empty', detail_limit=2)
    assert 'Registered editions: 10001.' in result
    assert 'Distinct works: 1.' in result
    assert 'Distinct registered originals: 1.' in result
    assert 'Registered original bytes, counted once per digest: 100.' in result
    assert 'Edition details: first 2 of 10001.' in result
    assert 'Library editions in no active collection: first 2 of 10001.' in result
    assert 'Duplicate candidate groups: 1.' in result
    assert 'Editions in candidate groups: 10001.' in result
    assert 'Additional edition records sharing an original: 10000.' in result
    assert 'Member editions shown: first 3 of 10001.' in result
    assert 'e00002' in result  # The explicitly bounded third duplicate member.
    assert 'e10000' not in result
    assert len(result) < 15000


def test_each_duplicate_group_is_bounded_and_total_is_complete(library):
    storage, path = library
    with storage.transaction():
        add_edition(storage, 'a', 'e5', digest='b' * 64, mime='text/plain', size=10)
    result = report(path, detail_limit=1)
    assert 'Duplicate candidate groups: 2.' in result
    assert 'Editions in candidate groups: 4.' in result
    duplicates = result.split('Exact-byte duplicate candidates (registered SHA-256)\n', 1)[1]
    assert 'Groups shown: first 1 of 2.' in duplicates
    assert 'SHA-256 ' + 'a' * 64 in duplicates
    assert 'SHA-256 ' + 'b' * 64 not in duplicates


def test_long_metadata_author_samples_and_control_text_have_explicit_bounds(library):
    storage, path = library
    with storage.transaction():
        add_edition(storage, 'empty', 'long', title='Ж' * 4000,
            authors=('Олена\nInvented heading', 'Žan', 'Third', 'Fourth', 'Fifth', 'Hidden sixth', 'Seventh'))
    result = report(path, 'empty', detail_limit=1)
    assert '[first 300 of 4000 characters]' in result
    assert '[first 5 of 7 authors, in stored order]' in result
    assert '"Олена\\nInvented heading"; "Žan"; "Third"; "Fourth"; "Fifth"' in result
    assert '\nInvented heading' not in result
    assert 'Hidden sixth' not in result
    assert len(result) < 12000


def test_cancellation_before_sql_returns_no_partial_report(library):
    _, path = library
    with snapshot(path) as db:
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(TimeoutError, match='cancelled'):
            catalog_report(db, 'a', cancelled=lambda: True)
        assert statements == []


def test_cancellation_interrupts_aggregate_sql_and_cleans_progress_handler(library):
    storage, path = library
    with storage.transaction():
        for i in range(300):
            add_edition(storage, 'empty', f'e{i:04d}', work='one-work')
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4
    with snapshot(path) as db:
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(TimeoutError, match='cancelled'):
            catalog_report(db, 'empty', cancelled=cancelled)
        assert any(statement.startswith('WITH scoped AS') for statement in statements)
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 305
        assert 'Registered editions: 300.' in catalog_report(db, 'empty', detail_limit=1)


def test_timeout_does_not_return_partial_output_and_reader_remains_usable(library, monkeypatch):
    _, path = library
    times = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(catalog_reports, 'monotonic', lambda: next(times, 2.0))
    with snapshot(path) as db:
        with pytest.raises(TimeoutError, match='timed out'):
            catalog_report(db, 'a', timeout=1)
        assert db.execute('SELECT 42').fetchone()[0] == 42


def test_failing_cancellation_callback_is_preserved_and_handler_removed(library):
    _, path = library
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise RuntimeError('Callback failure')
        return False
    with snapshot(path) as db:
        with pytest.raises(RuntimeError, match='Callback failure'):
            catalog_report(db, 'a', cancelled=cancelled)
        assert db.execute('SELECT 7').fetchone()[0] == 7


@pytest.mark.parametrize('limit', (0, 201, True, 1.5, None))
def test_invalid_detail_limit_rejected(library, limit):
    _, path = library
    with pytest.raises(ValueError, match='detail limit'):
        report(path, detail_limit=limit)


@pytest.mark.parametrize('timeout', (0, -1, 61, True, float('nan'), float('inf'), '10'))
def test_invalid_timeout_rejected(library, timeout):
    _, path = library
    with pytest.raises(ValueError, match='timeout'):
        report(path, timeout=timeout)


def test_invalid_cancel_callback_and_missing_snapshot_rejected(library):
    storage, path = library
    with pytest.raises(ValueError, match='callable'):
        report(path, cancelled=True)
    with pytest.raises(ValueError, match='caller snapshot'):
        catalog_report(storage.db, 'a')
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute('BEGIN')
        with pytest.raises(ValueError, match='foreign keys'):
            catalog_report(db, 'a')


def test_sqlite_errors_are_not_misreported_as_cancellation(library):
    _, path = library
    with snapshot(path) as db:
        def authorizer(operation, table, *_):
            if operation == sqlite3.SQLITE_READ and table == 'page':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError, match='prohibited'):
            catalog_report(db, 'a')
        db.set_authorizer(None)
        assert 'Registered editions: 4.' in catalog_report(db, 'a')
