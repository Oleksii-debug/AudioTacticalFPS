"""Reject excessive catalog reports before materializing existing read views."""
import pytest

from carr_library.application import LibraryApp
from carr_library.catalog import insights
from carr_library.catalog.membership import Catalog


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'report-budget-library')
    return app, app.create_workspace('Budget report')


def _edition(catalog, identity, *, title='Source', authors=('Author',),
             language='en', edition=None, volume=None):
    """Real catalog writes; no source parser or large allocation is required."""
    catalog.add_work('work-' + identity, title, authors)
    catalog.add_edition(identity, 'work-' + identity,
        {'sha256': 'a' * 64, 'mime': 'text/plain', 'size': 1},
        language=language, edition=edition, volume=volume)


def _must_not_materialize(*args, **kwargs):
    pytest.fail('A materializing catalog reader ran before the budget rejected the report')


def test_author_lists_rejected_before_browse_materializes_them(library, monkeypatch):
    app, workspace = library
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        for number in range(2):
            _edition(catalog, str(number),
                     authors=tuple(f'Author {i:03d} ' + 'x' * 90 for i in range(100)))
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)
    monkeypatch.setattr(insights, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='(?i)(metadata|limit|budget)'):
        insights.analyze_catalog(app, workspace)


@pytest.mark.parametrize('field', ('title', 'authors', 'language', 'edition', 'volume'))
def test_selected_metadata_budget_counts_utf8_bytes_before_browse(library, monkeypatch, field):
    app, workspace = library
    # 400 code points fit the scaled limit; 1,200 UTF-8 bytes do not.
    value = '\u754c' * 400
    with app.write(workspace) as db:
        _edition(Catalog(db, workspace), 'source',
                 **{field: (value,) if field == 'authors' else value})
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)
    monkeypatch.setattr(insights, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='(?i)(metadata|limit|budget)'):
        insights.analyze_catalog(app, workspace)


def test_source_count_rejected_before_browse(library, monkeypatch):
    app, workspace = library
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        _edition(catalog, 'one')
        _edition(catalog, 'two')
    monkeypatch.setattr(insights, 'MAX_SOURCES', 1)
    monkeypatch.setattr(insights, 'browse_editions', _must_not_materialize)

    with pytest.raises(ValueError, match='(?i)(sources|limit)'):
        insights.analyze_catalog(app, workspace)


def test_collection_names_rejected_before_collection_view(library, monkeypatch):
    app, workspace = library
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        catalog.create_collection('one', '\u754c' * 200)
        catalog.create_collection('two', '\u8a9e' * 200)
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)
    monkeypatch.setattr(insights, 'active_collections', _must_not_materialize)

    with app.read(workspace) as db:
        with pytest.raises(ValueError, match='(?i)(metadata|limit|budget)'):
            insights.report_collections(Catalog(db, workspace))


def test_collection_count_rejected_before_collection_view(library, monkeypatch):
    app, workspace = library
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        for number in range(3):
            catalog.create_collection(str(number), f'Collection {number}')
    monkeypatch.setattr(insights, 'MAX_COLLECTIONS', 2)
    monkeypatch.setattr(insights, 'active_collections', _must_not_materialize)

    with app.read(workspace) as db:
        with pytest.raises(ValueError, match='(?i)(collections|limit)'):
            insights.report_collections(Catalog(db, workspace))


def test_selected_collection_excludes_other_editions_from_preflight(library, monkeypatch):
    app, workspace = library
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        catalog.create_collection('selected', 'Selected')
        _edition(catalog, 'included')
        catalog.assign('selected', 'included')
        for number in range(2):
            _edition(catalog, f'excluded-{number}', authors=('x' * 2000,))
    monkeypatch.setattr(insights, 'MAX_SOURCES', 1)
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)

    report = insights.analyze_catalog(app, workspace, collection='selected')

    assert report.collection_id == 'selected'
    assert tuple(source.edition_id for source in report.sources) == ('included',)
    assert report.sources[0].authors == ('Author',)
    assert report.sources[0].collections == ('Selected',)


def test_preflight_counts_and_text_are_isolated_by_workspace(library, monkeypatch):
    app, workspace = library
    foreign = app.create_workspace('Other library')
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        catalog.create_collection('selected', 'Selected')
        _edition(catalog, 'included')
        catalog.assign('selected', 'included')
    with app.write(foreign) as db:
        catalog = Catalog(db, foreign)
        for number in range(2):
            catalog.create_collection(str(number), str(number) + 'x' * 2000)
            _edition(catalog, f'excluded-{number}', authors=('x' * 2000,))
    monkeypatch.setattr(insights, 'MAX_SOURCES', 1)
    monkeypatch.setattr(insights, 'MAX_COLLECTIONS', 1)
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 1024)

    with app.read(workspace) as db:
        assert insights.report_collections(Catalog(db, workspace)) == (('selected', 'Selected'),)
    report = insights.analyze_catalog(app, workspace)

    assert report.workspace == workspace
    assert tuple(source.edition_id for source in report.sources) == ('included',)
    assert report.sources[0].collections == ('Selected',)


def test_archived_collections_do_not_consume_preflight_count_or_bytes(library, monkeypatch):
    app, workspace = library
    with app.write(workspace) as db:
        catalog = Catalog(db, workspace)
        catalog.create_collection('active', 'Active')
        for number in range(2):
            identity = f'archived-{number}'
            catalog.create_collection(identity, str(number) + 'x' * 2000)
            catalog.archive(identity)
    monkeypatch.setattr(insights, 'MAX_COLLECTIONS', 1)
    monkeypatch.setattr(insights, 'MAX_METADATA_BYTES', 256)

    with app.read(workspace) as db:
        assert insights.report_collections(Catalog(db, workspace)) == (('active', 'Active'),)
    report = insights.analyze_catalog(app, workspace)

    assert report.sources == ()
