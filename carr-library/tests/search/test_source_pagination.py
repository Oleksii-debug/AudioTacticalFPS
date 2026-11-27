"""Real scoped SQLite catalogs and retained bytes, not fake search row counts."""
from contextlib import closing, contextmanager
from hashlib import sha256
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.citations.text_attestation import CONFIG
from carr_library.search.pagination import Continuation
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Page, Storage


def seed(app, workspace, count, *, text=None, collection=None):
    """Construct an actual sealed corpus; this does not time import supervision."""
    editions = []
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        with storage.transaction():
            for number in range(count):
                raw = text(number) if text else f'Original book {number:06d}. Railway evidence.\fНезмінний оригінал {number}.'
                data = raw.encode('utf-8')
                digest = sha256(data).hexdigest()
                app.assets.retain(data, 'text/plain', digest)
                edition, revision, work = uuid4().hex, uuid4().hex, uuid4().hex
                title = f'Synthetic source {number:06d}'
                register_batch(Catalog(storage.db, workspace), (Registration(work, edition,
                    dict(sha256=digest, size=len(data), mime='text/plain'), title=title,
                    authors=('Synthetic author',), language='uk',
                    collections=(collection,) if collection else ()),))
                storage.stage_revision(workspace, edition, revision, digest, CONFIG,
                                       (Page(part) for part in raw.split('\f')))
                storage.db.execute('INSERT INTO source_metadata '
                    '(workspace,edition,revision,author,title,volume,edition_label) VALUES(?,?,?,?,?,?,?)',
                    (workspace, edition, revision, 'Synthetic author', title, None, None))
                editions.append(edition)
    return editions


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'library')


def collect(app, workspace, query, **options):
    token, result, pages = None, [], []
    for _ in range(5000):
        page = app.search_page(workspace, query, cursor=token, **options)
        pages.append(page)
        result.extend(page.results)
        token = page.next_cursor
        if token is None:
            return result, pages
    raise AssertionError('Search did not make bounded continuation progress.')


def test_real_601_edition_catalog_all_results_reachable_and_exact(app):
    workspace = app.create_workspace('More than five hundred books')
    other = app.create_workspace('Isolated library')
    collection = app.create_collection(workspace, 'Selected originals')
    editions = seed(app, workspace, 601, collection=collection)
    seed(app, other, 2)
    rows, pages = collect(app, workspace, 'Railway', limit=37, page_budget=41,
                          collection=collection)
    assert len(rows) == len({r.edition_id for r in rows}) == 601
    assert {r.edition_id for r in rows} == set(editions)
    assert len(pages) > 10 and all(p.scanned_segments <= 41 for p in pages)
    assert all(p.scope_editions == 601 for p in pages)
    assert all(r.workspace == workspace and r.verified and r.quote == 'Railway' for r in rows)
    for row in (rows[0], rows[-1]):
        assert app.copy(workspace, row.ref, row.revision, 'quote') == 'Railway'
    assert app.search_page(other, 'Railway').scope_editions == 2


def test_one_source_more_than_one_hundred_matches_keeps_exact_app_bound_continuation(app):
    workspace = app.create_workspace('Repeated exact matches')
    seed(app, workspace, 1, text=lambda _: 'é ї Railway ' * 231)
    first = app.search_page(workspace, 'Railway', limit=37, page_budget=1)
    reopened = LibraryApp(app.root)
    with pytest.raises(ValueError, match='continuation'):
        reopened.search_page(workspace, 'Railway', limit=37, page_budget=1, cursor=first.next_cursor)
    token, count = first.next_cursor, len(first.results)
    spans = {(app._selections[row.ref].start, app._selections[row.ref].end) for row in first.results}
    while token:
        page = app.search_page(workspace, 'Railway', limit=37, page_budget=1, cursor=token)
        for row in page.results:
            selected = app._selections[row.ref]
            assert (selected.start, selected.end) not in spans
            spans.add((selected.start, selected.end))
            assert app.copy(workspace, row.ref, row.revision, 'quote') == 'Railway'
        count += len(page.results)
        token = page.next_cursor
    assert count == len(spans) == 231
    restarted, _ = collect(reopened, workspace, 'Railway', limit=37, page_budget=1)
    assert len(restarted) == 231


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'advanced', 'thematic', 'semantic'])
def test_empty_candidate_windows_are_not_reported_as_exhaustion(app, mode):
    workspace = app.create_workspace('Window continuation')
    seed(app, workspace, 1, text=lambda _: '\f'.join(['unrelated source'] * 9 + ['railway history']))
    notices = []
    rows, pages = collect(app, workspace, 'railway', mode=mode, limit=2,
                          page_budget=2, on_status=notices.append)
    assert len(rows) == 1 and rows[0].verified
    assert pages[0].results == () and pages[0].next_cursor
    assert pages[-1].complete
    if mode in ('thematic', 'semantic'):
        assert all('local to each window' in p.ordering for p in pages)
    if mode == 'semantic':
        assert notices and all('keyword' in notice for notice in notices)


@pytest.mark.parametrize('change', ['query', 'scope', 'filter', 'presentation', 'catalog'])
def test_cursor_cannot_rebind_scope_or_silently_mix_catalog_versions(app, change):
    workspace = app.create_workspace('Bound continuation')
    other = app.create_workspace('Other library')
    seed(app, workspace, 2)
    first = app.search_page(workspace, 'Railway', limit=1, page_budget=1)
    before = dict(app._selections)
    args = dict(cursor=first.next_cursor, limit=1, page_budget=1)
    chosen, query = workspace, 'Railway'
    if change == 'query':
        query = 'Original'
    elif change == 'scope':
        chosen = other
    elif change == 'filter':
        args['author'] = 'different author'
    elif change == 'presentation':
        args['result_mode'] = 'short'
    else:
        seed(app, workspace, 1, text=lambda _: 'New Railway source')
    with pytest.raises(ValueError, match='continuation|changed|snapshot'):
        app.search_page(chosen, query, **args)
    assert app._selections == before


def test_cancelled_page_preserves_previous_selection_and_cursor_retry(app):
    workspace = app.create_workspace('Cancelled page')
    seed(app, workspace, 3)
    first = app.search_page(workspace, 'Railway', limit=1, page_budget=1)
    before = dict(app._selections)
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 15
    with pytest.raises(TimeoutError, match='cancelled'):
        app.search_page(workspace, 'Railway', limit=1, page_budget=1, cursor=first.next_cursor, cancelled=cancelled)
    assert app._selections == before
    assert app.copy(workspace, first.results[0].ref, first.results[0].revision, 'quote') == 'Railway'
    resumed = app.search_page(workspace, 'Railway', limit=1, page_budget=1, cursor=first.next_cursor)
    # Each source has a second, nonmatching fragment. A bounded empty source
    # window must retain its continuation instead of pretending exhaustion.
    for _ in range(8):
        assert resumed.scanned_segments <= 1
        if resumed.results:
            break
        assert not resumed.complete and resumed.next_cursor
        resumed = app.search_page(workspace, 'Railway', limit=1, page_budget=1,
                                  cursor=resumed.next_cursor)
    else:
        pytest.fail('The original continuation did not reach the next matching source.')
    assert resumed.results[0].edition_id != first.results[0].edition_id


def test_changed_original_is_a_hard_integrity_error(app):
    workspace = app.create_workspace('Retained source guard')
    seed(app, workspace, 1)
    app.search_page(workspace, 'Railway', limit=1)
    original = next(app.assets_dir.glob('*.txt'))
    original.write_bytes(original.read_bytes().replace(b'Railway', b'Changed'))
    before = dict(app._selections)
    with pytest.raises(ValueError):
        app.search_page(workspace, 'Railway', page_budget=1)
    assert app._selections == before


def test_character_budget_does_not_load_next_oversized_source(app):
    workspace = app.create_workspace('Bounded characters')
    seed(app, workspace, 1, text=lambda _: '\f'.join(['Railway'] * 3))
    rows, pages = collect(app, workspace, 'Railway', character_budget=7)
    assert len(rows) == 3 and all(p.scanned_segments == 1 for p in pages)
    with pytest.raises(ValueError, match='character budget'):
        app.search_page(workspace, 'Railway', character_budget=6)


def test_forged_cursor_never_supplies_a_position_or_current_authorization(app):
    workspace = app.create_workspace('Strict continuation')
    seed(app, workspace, 1, text=lambda _: 'Railway Railway')
    first = app.search_page(workspace, 'Railway', limit=1, page_budget=1)
    before = dict(app._selections)
    tokens = tuple(app._search_cursors._values.items())
    for forged in ('{"position":[0,true,0,0]}', first.next_cursor + 'x', {'position': [0, 1]}, True):
        with pytest.raises(ValueError, match='continuation'):
            app.search_page(workspace, 'Railway', limit=1, page_budget=1, cursor=forged)
        assert app._selections == before
        assert tuple(app._search_cursors._values.items()) == tokens
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        app.search_page(workspace, 'Railway', limit=1, page_budget=1, cursor=first.next_cursor)


def test_presentation_deduplicates_adjacent_page_boundary(app):
    workspace = app.create_workspace('Presented sentence')
    seed(app, workspace, 1, text=lambda _: 'Railway and Railway and Railway.\nNext Railway sentence.')
    rows, pages = collect(app, workspace, 'Railway', limit=1, page_budget=1, result_mode='short')
    assert len({(r.page, r.quote) for r in rows}) == len(rows)
    assert pages[-1].complete


def test_mismatched_active_asset_is_not_silently_filtered_from_complete_search(app):
    workspace = app.create_workspace('Active source identity')
    edition, = seed(app, workspace, 1)
    previous, = app.search(workspace, 'Railway')
    before = dict(app._selections)
    raw = 'Railway source with another original identity.'
    data = raw.encode('utf-8')
    digest = sha256(data).hexdigest()
    app.assets.retain(data, 'text/plain', digest)
    revision = uuid4().hex
    # Normal Storage writes correctly refuse this composite foreign-key
    # violation. Model a damaged database without altering its schema/triggers.
    with closing(sqlite3.connect(app.database)) as db:
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 0
        db.execute('INSERT INTO catalog_asset VALUES(?,?,?)', (digest, 'text/plain', len(data)))
        db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', (revision, workspace, edition, digest, CONFIG))
        page = db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) '
                          'VALUES(?,1,NULL,NULL,0,?,?)', (revision, raw, digest)).lastrowid
        db.execute('INSERT INTO sealed VALUES(?)', (revision,))
        db.execute('UPDATE active SET revision=? WHERE workspace=? AND edition=?',
                   (revision, workspace, edition))
        db.execute('INSERT INTO indexed VALUES(?,?)', (page, raw))
        db.execute('INSERT INTO source_metadata '
                   '(workspace,edition,revision,author,title,volume,edition_label) VALUES(?,?,?,?,?,?,?)',
                   (workspace, edition, revision, 'Synthetic author', 'Inconsistent source', None, None))
        db.commit()
        assert db.execute('PRAGMA foreign_key_check').fetchall()
    with pytest.raises(ValueError):
        app.search(workspace, 'Railway')
    # The canonical retained-source authorization rejects this mismatch before
    # the later window scanner can inspect the inconsistent catalog identity.
    with pytest.raises(ValueError, match='outside the selected library'):
        app.search_page(workspace, 'Railway')
    assert app._selections == before
    assert app.copy(workspace, previous.ref, previous.revision, 'quote') == 'Railway'


def test_catalog_only_edition_is_allowed_without_an_active_source_revision(app):
    workspace = app.create_workspace('Pending catalog record')
    data = b'Pending source'
    digest = sha256(data).hexdigest()
    app.assets.retain(data, 'text/plain', digest)
    with app.catalog_transaction(workspace) as catalog:
        register_batch(catalog, (Registration(uuid4().hex, uuid4().hex,
            dict(sha256=digest, mime='text/plain', size=len(data)), title='Not extracted yet'),))
    page = app.search_page(workspace, 'Pending')
    assert page.complete and page.results == ()
    assert page.scanned_segments == 0 and page.scope_editions == 1


def test_complete_raw_integrity_precedes_window_candidate_and_cursor_pruning(app):
    workspace = app.create_workspace('Complete scoped integrity')
    edition, = seed(app, workspace, 1, text=lambda _: 'Railway first.\funrelated tail')
    first = app.search_page(workspace, 'Railway', limit=1, page_budget=1)
    before = dict(app._selections)
    tokens = tuple(app._search_cursors._values.items())
    with closing(sqlite3.connect(app.database)) as db:
        old = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                         (workspace, edition)).fetchone()[0]
        row = db.execute('SELECT asset,config FROM revision WHERE id=?', (old,)).fetchone()
        revision = uuid4().hex
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', (revision, workspace, edition, row[0], row[1]))
        originals = db.execute('SELECT ordinal,physical,printed,verified,raw,digest '
                               'FROM page WHERE revision=? ORDER BY ordinal', (old,)).fetchall()
        for ordinal, physical, printed, verified, raw, digest in originals:
            # A damaged second immutable segment has the old digest and never
            # matches the query. It lies beyond the one-segment result window.
            changed = raw if ordinal == 1 else raw.replace('unrelated', 'corrupted')
            page = db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) '
                'VALUES(?,?,?,?,?,?,?)', (revision, ordinal, physical, printed, verified, changed, digest)).lastrowid
            db.execute('INSERT INTO indexed VALUES(?,?)', (page, changed))
        db.execute('INSERT INTO sealed VALUES(?)', (revision,))
        db.execute('UPDATE active SET revision=? WHERE workspace=? AND edition=?',
                   (revision, workspace, edition))
        db.execute('INSERT INTO source_metadata (workspace,edition,revision,author,title,volume,edition_label) '
            'SELECT workspace,edition,?,author,title,volume,edition_label FROM source_metadata WHERE revision=?',
            (revision, old))
        db.commit()
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    for cursor in (None, first.next_cursor):
        with pytest.raises(ValueError, match='stored source text has been damaged'):
            app.search_page(workspace, 'Railway', limit=1, page_budget=1, cursor=cursor)
        assert app._selections == before
        assert tuple(app._search_cursors._values.items()) == tokens


def test_window_budget_changes_do_not_rebind_an_existing_opaque_position(app):
    workspace = app.create_workspace('Bounded page identity')
    seed(app, workspace, 1, text=lambda _: 'Railway Railway')
    first = app.search_page(workspace, 'Railway', limit=1, page_budget=1)
    for changes in ({'page_budget': 2}, {'character_budget': 100}):
        values = dict(limit=1, page_budget=1, cursor=first.next_cursor)
        values.update(changes)
        with pytest.raises(ValueError, match='continuation'):
            app.search_page(workspace, 'Railway', **values)


@pytest.mark.parametrize('kind', ['nul-suffix', 'blob'])
def test_malformed_raw_storage_is_refused_before_full_value_materialization(app, monkeypatch, kind):
    workspace = app.create_workspace('Pre-read admission')
    edition, = seed(app, workspace, 1, text=lambda _: 'Railway')
    malformed = 'R\0' + 'x' * 127
    if kind == 'blob':
        malformed = malformed.encode('utf-8')
    with closing(sqlite3.connect(app.database)) as db:
        old = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                         (workspace, edition)).fetchone()[0]
        asset, config = db.execute('SELECT asset,config FROM revision WHERE id=?', (old,)).fetchone()
        revision = uuid4().hex
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('INSERT INTO revision VALUES(?,?,?,?,?)', (revision, workspace, edition, asset, config))
        page_id = db.execute('INSERT INTO page(revision,ordinal,physical,printed,verified,raw,digest) '
            'VALUES(?,1,NULL,NULL,0,?,?)', (revision, malformed, asset)).lastrowid
        db.execute('INSERT INTO sealed VALUES(?)', (revision,))
        db.execute('UPDATE active SET revision=? WHERE workspace=? AND edition=?',
                   (revision, workspace, edition))
        db.execute('INSERT INTO source_metadata (workspace,edition,revision,author,title,volume,edition_label) '
            'SELECT workspace,edition,?,author,title,volume,edition_label FROM source_metadata WHERE revision=?',
            (revision, old))
        db.commit()
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('SELECT length(CAST(raw AS BLOB)) FROM page WHERE id=?', (page_id,)).fetchone()[0] > 4 * 5
        if kind == 'nul-suffix':
            assert db.execute('SELECT length(raw) FROM page WHERE id=?', (page_id,)).fetchone()[0] == 1
    traced, actual = [], LibraryApp._search_read
    @contextmanager
    def observed(candidate, *args, **kwargs):
        with actual(candidate, *args, **kwargs) as (db, budget):
            db.set_trace_callback(traced.append)
            try:
                yield db, budget
            finally:
                db.set_trace_callback(None)
    monkeypatch.setattr(LibraryApp, '_search_read', observed)
    with pytest.raises(ValueError, match='before search-page admission'):
        app.search_page(workspace, 'Railway', limit=1, page_budget=1, character_budget=5)
    assert not any(sql.lstrip().lower().startswith('select raw from page') for sql in traced)
    assert not app._selections and not app._search_cursors._values
