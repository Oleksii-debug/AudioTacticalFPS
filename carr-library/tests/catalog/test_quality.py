"""Real scoped SQLite catalog checks; no model or source-file inference is needed."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.membership import Catalog
from carr_library.catalog import insights, quality


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    other = app.create_workspace('Other private library')
    return app, workspace, other


def add(catalog, identity, *, title='Book', authors=('Author',), language='en',
        asset=None, work=None, edition=None, volume=None):
    work = work or 'work-' + identity
    if not catalog.db.execute('SELECT 1 FROM catalog_work WHERE workspace=? AND id=?',
                              (catalog.workspace, work)).fetchone():
        catalog.add_work(work, title, authors)
    catalog.add_edition(identity, work, dict(sha256=asset or identity.encode().hex().ljust(64, '0'),
                                           mime='text/plain', size=10),
                        language=language, edition=edition, volume=volume)


def issues(snapshot, kind):
    return tuple(issue for issue in snapshot.issues if issue.category == kind)


def test_one_read_only_snapshot_reports_missing_metadata_and_distinct_counts(library):
    app, workspace, other = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'a', title=' ', authors=(), language=None)
        add(catalog, 'b', title='Complete', authors=('Олена', 'Žan'), language='uk', edition='First')
        add(catalog, 'c', work='work-b', language='sk', edition='Second')
        catalog.create_collection('c1', 'Filed books')
        catalog.create_collection('empty', 'Empty active collection')
        catalog.assign('c1', 'b')
        catalog.assign('c1', 'c')
    with app.catalog_transaction(other) as catalog:
        add(catalog, 'a', title='Private title', authors=('Private author',), language='secret')
        catalog.create_collection('foreign', 'Private collection')
    with app.read(workspace) as db:
        before = tuple(db.iterdump())
    result = quality.catalog_quality(app, workspace)
    assert result.summary == quality.QualitySummary(3, 2, 2, 2, 2, 3, 1)
    assert {issue.category for issue in result.issues} == {
        'missing_title', 'missing_authors', 'missing_language', 'unfiled'}
    assert all(issue.edition_ids == ('a',) for issue in result.issues)
    assert [row.edition_id for row in result.editions] == ['a', 'b', 'c']
    assert 'Private' not in repr(result)
    with app.read(workspace) as db:
        assert tuple(db.iterdump()) == before
    with pytest.raises(FrozenInstanceError):
        result.workspace = other


def test_active_collection_filter_and_archived_memberships_are_scoped(library):
    app, workspace, other = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'a')
        add(catalog, 'b', title='Second')
        catalog.create_collection('selected', 'Selected')
        catalog.create_collection('old', 'Archived')
        catalog.assign('selected', 'a')
        catalog.assign('old', 'b')
        catalog.archive('old')
    with app.catalog_transaction(other) as catalog:
        catalog.create_collection('foreign', 'Foreign')
    all_books = quality.catalog_quality(app, workspace)
    assert issues(all_books, 'unfiled')[0].edition_ids == ('b',)
    selected = quality.catalog_quality(app, workspace, collection='selected')
    assert [row.edition_id for row in selected.editions] == ['a']
    assert selected.summary.sources == selected.summary.collections == 1
    assert not issues(selected, 'unfiled')
    for collection in ('old', 'foreign', 'unknown', ''):
        with pytest.raises(ValueError):
            quality.catalog_quality(app, workspace, collection=collection)


def test_same_asset_groups_do_not_cross_workspace_or_claim_file_attestation(library):
    app, workspace, other = library
    digest = 'a' * 64
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'a', asset=digest, edition='First')
        add(catalog, 'b', asset=digest, title='Different catalog title', edition='Revised')
    with app.catalog_transaction(other) as catalog:
        add(catalog, 'private', asset=digest)
    result = quality.catalog_quality(app, workspace)
    group, = issues(result, 'same_asset')
    assert group.edition_ids == ('a', 'b')
    assert digest in group.reason and 'not re-verified' in group.reason
    assert result.summary.sources == 2 and result.summary.unique_assets == 1
    assert len(result.editions) == 2 and not issues(result, 'possible_duplicate')


def test_tentative_unicode_metadata_groups_preserve_author_order_and_editions(library):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'a', title='  Étude\tКиїв ', authors=('Žan', 'Олена'), language=' UK ',
            edition='First', volume='I')
        add(catalog, 'b', title='E\u0301TUDE  Київ', authors=('Z\u030can', ' ОЛЕНА '), language='uk',
            edition='Revised', volume='II')
        add(catalog, 'c', title='Étude Київ', authors=('Олена', 'Žan'), language='uk')
        add(catalog, 'd', title='Étude Київ', authors=('Žan', 'Олена'), language='sk')
        add(catalog, 'e', title='Etude Київ', authors=('Žan', 'Олена'), language='uk')
    result = quality.catalog_quality(app, workspace)
    group, = issues(result, 'possible_duplicate')
    assert group.edition_ids == ('a', 'b')
    assert 'tentative' in group.reason and 'Different editions' in group.reason
    assert result.editions[0].edition == 'First' and result.editions[1].edition == 'Revised'
    assert result.editions[0].volume == 'I' and result.editions[1].volume == 'II'
    assert result.summary.sources == 5 and result.summary.unique_assets == 5


def test_missing_metadata_never_becomes_a_duplicate_key(library):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        for identity, title, authors, language in (
                ('a', None, ('Author',), 'en'), ('b', ' ', ('Author',), 'en'),
                ('c', 'Book', (), 'en'), ('d', 'Book', (), 'en'),
                ('e', 'Book', ('Author',), None), ('f', 'Book', ('Author',), ' ')):
            add(catalog, identity, title=title, authors=authors, language=language)
    result = quality.catalog_quality(app, workspace)
    assert not issues(result, 'possible_duplicate')
    assert len(issues(result, 'missing_title')) == 2
    assert len(issues(result, 'missing_authors')) == 2
    assert len(issues(result, 'missing_language')) == 2


def test_archived_workspace_and_invalid_cancellation_fail_closed(library):
    app, workspace, _ = library
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='missing or archived'):
        quality.catalog_quality(app, workspace)
    with pytest.raises(ValueError, match='callback'):
        quality.catalog_quality(app, workspace, cancelled=None)


def test_empty_library_and_empty_active_collection_have_honest_zero_counts(library):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        catalog.create_collection('empty', 'Empty')
    for collection in (None, 'empty'):
        result = quality.catalog_quality(app, workspace, collection=collection)
        assert result.summary == quality.QualitySummary(0, 0, 0, 0, 1, 0, 0)
        assert result.issues == result.editions == ()


def test_all_pages_share_one_read_snapshot_and_keep_rows_after_first_500(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        for number in range(503):
            add(catalog, f'e{number:04}', title=f'Book {number}')
    original_read = app.read
    original_browse = quality.browse_editions
    reads, pages, dbs = [], [], []
    @contextmanager
    def read(scope):
        reads.append(scope)
        with original_read(scope) as db:
            yield db
    def browse(catalog, **options):
        assert catalog.db.in_transaction
        pages.append(options['after'])
        dbs.append(catalog.db)
        return original_browse(catalog, **options)
    monkeypatch.setattr(app, 'read', read)
    monkeypatch.setattr(quality, 'browse_editions', browse)
    result = quality.catalog_quality(app, workspace)
    assert reads == [workspace] and pages == [None, 'e0499']
    assert dbs[0] is dbs[1]
    assert result.summary.sources == 503 and result.editions[-1].edition_id == 'e0502'


def test_cancellation_before_and_during_read_never_publishes_partial_report(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'a')
    stopped = Event()
    stopped.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        quality.catalog_quality(app, workspace, cancelled=stopped.is_set)
    stopped.clear()
    original = quality.browse_editions
    def cancel_after_page(*args, **kwargs):
        rows = original(*args, **kwargs)
        stopped.set()
        return rows
    monkeypatch.setattr(quality, 'browse_editions', cancel_after_page)
    with pytest.raises(InterruptedError, match='no report'):
        quality.catalog_quality(app, workspace, cancelled=stopped.is_set)
    assert len(app.browse(workspace)) == 1


def test_catalog_bound_rejects_incomplete_summary_instead_of_silently_truncating(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'a')
        add(catalog, 'b')
    monkeypatch.setattr(quality, 'MAX_EDITIONS', 1)
    with pytest.raises(ValueError, match='Choose a collection'):
        quality.catalog_quality(app, workspace)


def _must_not_materialize(*args, **kwargs):
    pytest.fail('The report materialized catalog rows before rejecting its input budget')


def test_report_budget_rejects_real_large_author_catalog_before_browse(library, monkeypatch):
    app, workspace, _ = library
    # All values fit the catalog's existing per-work limits. Together these
    # real SQLite rows exceed the default report budget before any EditionView.
    authors = tuple(f'{number:03}' + '\u754c' * 997 for number in range(100))
    with app.catalog_transaction(workspace) as catalog:
        for number in range(100):
            add(catalog, f'heavy-{number:03}', authors=authors)
    with app.read(workspace) as db:
        size = db.execute('SELECT SUM(length(CAST(name AS BLOB))) '
                          'FROM catalog_author WHERE workspace=?', (workspace,)).fetchone()[0]
        assert size == 29_940_000
    monkeypatch.setattr(quality, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='metadata.*limit'):
        quality.catalog_quality(app, workspace)


@pytest.mark.parametrize('field', ('title', 'authors', 'language', 'edition', 'volume'))
def test_report_preflight_counts_utf8_metadata_before_browse(library, monkeypatch, field):
    app, workspace, _ = library
    value = '\u754c' * 400  # 400 code points, but 1,200 UTF-8 bytes.
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'source', **{field: (value,) if field == 'authors' else value})
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)
    monkeypatch.setattr(quality, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='metadata.*limit'):
        quality.catalog_quality(app, workspace)


def test_report_preflight_rejects_source_count_before_browse(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'one')
        add(catalog, 'two')
    monkeypatch.setattr(insights, 'MAX_SOURCES', 1)
    monkeypatch.setattr(quality, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='Too many sources'):
        quality.catalog_quality(app, workspace)


@pytest.mark.parametrize('bound', ('bytes', 'count'))
def test_report_preflight_rejects_collection_limit_before_view(library, monkeypatch, bound):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        catalog.create_collection('one', '\u754c' * 200)
        catalog.create_collection('two', '\u8a9e' * 200)
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024 if bound == 'bytes' else 4096)
    monkeypatch.setattr(insights, 'MAX_COLLECTIONS', 1 if bound == 'count' else 10)
    monkeypatch.setattr(insights, 'active_collections', _must_not_materialize)
    monkeypatch.setattr(quality, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='(?i)(metadata.*limit|too many active collections)'):
        quality.catalog_quality(app, workspace)


def test_report_budget_combines_collection_names_and_selected_metadata(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        catalog.create_collection('selected', '\u754c' * 200)
        add(catalog, 'source', title='\u8a9e' * 200)
        catalog.assign('selected', 'source')
    # Either part fits independently, while the complete snapshot does not.
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)
    monkeypatch.setattr(quality, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='metadata.*limit'):
        quality.catalog_quality(app, workspace, collection='selected')


def test_report_preflight_preserves_selected_scope_and_duplicate_hints(library, monkeypatch):
    app, workspace, other = library
    with app.catalog_transaction(workspace) as catalog:
        catalog.create_collection('selected', 'Selected')
        for identity, edition in (('a', 'First'), ('b', 'Revised')):
            add(catalog, identity, asset='a' * 64, edition=edition)
            catalog.assign('selected', identity)
        add(catalog, 'excluded', authors=('x' * 2000,))
        catalog.create_collection('archived', '\u754c' * 2000)
        catalog.archive('archived')
    with app.catalog_transaction(other) as catalog:
        for number in range(3):
            add(catalog, f'private-{number}', authors=('\u754c' * 2000,))
            catalog.create_collection(str(number), str(number) + '\u8a9e' * 2000)
    monkeypatch.setattr(insights, 'MAX_SOURCES', 2)
    monkeypatch.setattr(insights, 'MAX_COLLECTIONS', 1)
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)

    result = quality.catalog_quality(app, workspace, collection='selected')

    assert result.collections == (('selected', 'Selected'),)
    assert tuple(row.edition_id for row in result.editions) == ('a', 'b')
    assert tuple(row.edition for row in result.editions) == ('First', 'Revised')
    assert result.summary == quality.QualitySummary(2, 2, 1, 1, 1, 1, 2)
    assert issues(result, 'same_asset')[0].edition_ids == ('a', 'b')
    assert issues(result, 'possible_duplicate')[0].edition_ids == ('a', 'b')
    assert not issues(result, 'unfiled')
    assert 'private' not in repr(result)


@pytest.mark.parametrize('phase', ('preflight', 'browse'))
def test_sqlite_progress_cancellation_interrupts_actual_author_join(library, monkeypatch, phase):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        for number in range(20):
            add(catalog, f'e{number:04}', authors=tuple(f'Author {i}' for i in range(100)))
    calls, interrupted, active = 0, [], False
    def cancelled():
        nonlocal calls
        if not active:
            return False
        calls += 1
        return calls >= 2
    target = insights if phase == 'preflight' else quality
    name = '_preflight_editions' if phase == 'preflight' else 'browse_editions'
    original = getattr(target, name)
    def observe(*args, **kwargs):
        nonlocal active
        active = True
        try:
            return original(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            interrupted.append(exc.sqlite_errorcode)
            raise
        finally:
            active = False
    monkeypatch.setattr(target, name, observe)
    with pytest.raises(InterruptedError, match='cancelled'):
        quality.catalog_quality(app, workspace, cancelled=cancelled)
    assert interrupted == [sqlite3.SQLITE_INTERRUPT]



def test_utf8_metadata_preflight_rejects_before_materializing_authors(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'heavy', authors=('界' * 200,))
    monkeypatch.setattr(quality.insights, 'MAX_METADATA_BYTES', 500)
    monkeypatch.setattr(quality, 'browse_editions',
                        lambda *a, **k: pytest.fail('oversized metadata was materialized'))
    with pytest.raises(ValueError, match='metadata.*limit'):
        quality.catalog_quality(app, workspace)


def test_count_preflight_rejects_before_materializing_a_page(library, monkeypatch):
    app, workspace, _ = library
    with app.catalog_transaction(workspace) as catalog:
        add(catalog, 'one')
        add(catalog, 'two')
    monkeypatch.setattr(quality.insights, 'MAX_SOURCES', 1)
    monkeypatch.setattr(quality, 'browse_editions',
                        lambda *a, **k: pytest.fail('oversized catalog was materialized'))
    with pytest.raises(ValueError, match='Too many sources'):
        quality.catalog_quality(app, workspace)


def test_preflight_counts_only_authorized_selected_sources(library, monkeypatch):
    app, workspace, other = library
    with app.catalog_transaction(workspace) as catalog:
        catalog.create_collection('selected', 'Selected')
        add(catalog, 'small')
        add(catalog, 'excluded', authors=('界' * 200,))
        catalog.assign('selected', 'small')
    with app.catalog_transaction(other) as catalog:
        add(catalog, 'private', authors=('界' * 200,))
    monkeypatch.setattr(quality.insights, 'MAX_METADATA_BYTES', 500)
    selected = quality.catalog_quality(app, workspace, collection='selected')
    assert [row.edition_id for row in selected.editions] == ['small']
    with pytest.raises(ValueError, match='metadata.*limit'):
        quality.catalog_quality(app, workspace)
