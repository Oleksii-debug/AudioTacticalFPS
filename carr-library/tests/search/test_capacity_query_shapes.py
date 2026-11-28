"""Execute current services against the frozen pre-optimization SQL semantics."""
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.membership import Catalog
from carr_library.catalog.read_views import EditionView, browse_editions
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.search.pagination import SearchPageBudget, _fingerprint, _json
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Page, Storage


# Actual parameterized queries from production baseline 498bf205; deliberately
# independent of the candidate's optional predicates and explicit scoped joins.
BASELINE_CATALOG_SQL = '''WITH selected AS (
 SELECT e.workspace,e.id,e.work,e.asset,w.title,e.language,e.edition,e.volume
 FROM catalog_edition e JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work
 WHERE e.workspace=:scope AND (:after IS NULL OR e.id COLLATE BINARY > :after)
 AND (:edition_id IS NULL OR e.id=:edition_id)
 AND (:collection IS NULL OR EXISTS (SELECT 1 FROM catalog_membership m
   WHERE m.workspace=e.workspace AND m.edition=e.id AND m.collection=:collection))
 AND (:title IS NULL OR instr(w.title,:title)>0)
 AND (:author IS NULL OR EXISTS (SELECT 1 FROM catalog_author a
   WHERE a.workspace=e.workspace AND a.work=e.work AND instr(a.name,:author)>0))
 AND (:language IS NULL OR e.language=:language)
 AND (:edition IS NULL OR e.edition=:edition)
 ORDER BY e.id COLLATE BINARY LIMIT :limit
) SELECT s.id,s.work,s.asset,s.title,s.language,s.edition,s.volume,a.name
 FROM selected s LEFT JOIN catalog_author a ON a.workspace=s.workspace AND a.work=s.work
 ORDER BY s.id COLLATE BINARY,a.position'''


def baseline_browse(db, workspace, **options):
    parameters = dict(scope=workspace, after=None, edition_id=None, collection=None,
                      title=None, author=None, language=None, edition=None, limit=100)
    parameters.update(options)
    grouped = {}
    for row in db.execute(BASELINE_CATALOG_SQL, parameters):
        fields = tuple(row)[:7]
        names = grouped.setdefault(fields, [])
        if row[7] is not None:
            names.append(row[7])
    return tuple(EditionView(*fields, tuple(names)) for fields, names in grouped.items())


def baseline_fingerprint(db, workspace, filters, batch):
    digest = sha256(b'carr-search-snapshot/1\n')
    after = None
    editions = pages = 0
    while True:
        views = baseline_browse(db, workspace, after=after, limit=batch, **filters)
        if not views:
            break
        for view in views:
            digest.update(_json(('edition', asdict(view))) + b'\n')
            editions += 1
        ids = tuple(row.edition_id for row in views)
        sql = ('SELECT p.id,p.edition,p.revision,p.ordinal,p.physical,p.printed,'
               'p.verified,p.digest,length(p.raw),p.asset,m.author,m.title,m.volume,m.edition_label '
               'FROM active_page p LEFT JOIN source_metadata m '
               'ON m.workspace=p.workspace AND m.edition=p.edition AND m.revision=p.revision '
               'WHERE p.workspace=? AND p.edition IN (' + ','.join('?' for _ in ids) +
               ') ORDER BY p.edition COLLATE BINARY,p.ordinal,p.id')
        for row in db.execute(sql, (workspace, *ids)):
            digest.update(_json(('page', tuple(row))) + b'\n')
            pages += 1
        after = views[-1].edition_id
    return digest.hexdigest(), editions, pages


@pytest.fixture
def corpus(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    scopes, collections = {}, {}
    for name, count in (('main', 7), ('foreign', 3)):
        workspace = app.create_workspace(name)
        scopes[name] = workspace
        collections[name] = {key: app.create_collection(workspace, key) for key in ('one', 'two')}
        for number in range(count):
            metadata = dict(title="Alpha %_'" if number % 2 == 0 else 'Beta',
                            authors=('Zoë', 'Ada') if number % 2 == 0 else (),
                            language=('uk', 'en', None)[number % 3],
                            edition=('First', 'Second', None)[number % 3],
                            work_id=f'work-{number:02d}', edition_id=f'e-{number:02d}',
                            collections=(collections[name]['one' if number % 2 == 0 else 'two'],))
            result = app.add_text(workspace, metadata.pop('title'),
                f'Needle {name} {number}.\fSecond Needle {name} {number}.', **metadata)
            assert result['state'] == 'complete' and result['verified'] is True
    return app, scopes, collections


CATALOG_FILTERS = (
    {}, {'after': None}, {'after': 'e-02'}, {'after': 'e-99'},
    {'edition_id': None}, {'edition_id': 'e-02'}, {'edition_id': 'missing'},
    {'after': 'e-02', 'edition_id': 'e-02'}, {'after': 'e-01', 'edition_id': 'e-02'},
    {'collection': 'one'}, {'collection': 'two'}, {'title': 'Alpha'}, {'title': "%_'"},
    {'title': "' OR 1=1 --"}, {'author': 'Ada'}, {'author': 'Zoë'},
    {'language': 'uk'}, {'language': 'en'}, {'edition': 'First'}, {'edition': 'Second'},
    {'title': 'Alpha', 'author': 'Ada', 'language': 'uk', 'edition': 'First', 'collection': 'one'},
    {'after': 'e-01', 'limit': 1}, {'edition_id': 'e-02', 'author': 'Ada', 'limit': 1},
)


@pytest.mark.parametrize('scope_name', ('main', 'foreign'))
@pytest.mark.parametrize('filters', CATALOG_FILTERS)
def test_all_catalogue_filters_preserve_baseline_rows_and_author_order(corpus, scope_name, filters):
    app, scopes, collections = corpus
    selected = dict(filters)
    if selected.get('collection'):
        selected['collection'] = collections[scope_name][selected['collection']]
    with app.read(scopes[scope_name]) as db:
        expected = baseline_browse(db, scopes[scope_name], **selected)
        actual = browse_editions(Catalog(db, scopes[scope_name]), **selected)
        assert actual == expected


@pytest.mark.parametrize('scope_name', ('main', 'foreign'))
@pytest.mark.parametrize('filters', ({}, {'title': 'Alpha'}, {'title': 'missing'},
    {'author': 'Ada'}, {'language': 'uk'}, {'edition': 'Second'}, {'collection': 'one'},
    {'collection': 'two', 'language': 'en'},
    {'title': 'Alpha', 'author': 'Zoë', 'language': 'uk', 'edition': 'First'}))
def test_complete_fingerprint_bytes_and_counts_equal_baseline(corpus, scope_name, filters):
    app, scopes, collections = corpus
    selected = dict(filters)
    if selected.get('collection'):
        selected['collection'] = collections[scope_name][selected['collection']]
    with app._search_read(scopes[scope_name], 60, lambda: False) as (db, budget):
        expected = baseline_fingerprint(db, scopes[scope_name], selected, 2)
        actual = _fingerprint(db, Catalog(db, scopes[scope_name]), selected,
                              SearchPageBudget(editions=2, pages=1), budget)
        assert actual == expected


@pytest.mark.parametrize('changed_edition', ('e-00', 'e-06'))
def test_active_revision_change_in_first_or_later_batch_rejects_old_cursor(corpus, changed_edition):
    app, scopes, _ = corpus
    workspace = scopes['main']
    policy = SearchPageBudget(editions=2, pages=1)
    first = app.search_page(workspace, 'Needle', limit=1, page_budget=policy)
    selections = dict(app._selections)
    new_revision = sha256((workspace + changed_edition + 'new active revision').encode()).hexdigest()
    with exclusive_writer(app.root), closing(Storage(
            app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        with storage.transaction():
            old = storage.db.execute('SELECT r.id,r.asset,r.config FROM revision r JOIN active a '
                'ON a.revision=r.id WHERE a.workspace=? AND a.edition=?',
                (workspace, changed_edition)).fetchone()
            pages = tuple(Page(row[0], row[1], row[2], bool(row[3])) for row in storage.db.execute(
                'SELECT raw,physical,printed,verified FROM page WHERE revision=? ORDER BY ordinal', (old[0],)))
            metadata = storage.db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                'WHERE workspace=? AND edition=? AND revision=?', (workspace, changed_edition, old[0])).fetchone()
            storage.stage_revision(workspace, changed_edition, new_revision, old[1], old[2], pages)
            storage.db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                (workspace, changed_edition, new_revision, *metadata))
        storage.check()
    with pytest.raises(ValueError, match='changed'):
        app.search_page(workspace, 'Needle', limit=1, cursor=first.next_cursor, page_budget=policy)
    assert app._selections == selections
    resumed = app.search_page(workspace, 'Needle', limit=1, page_budget=policy)
    assert resumed.snapshot != first.snapshot
    assert resumed.results[0].workspace == workspace and resumed.results[0].verified

