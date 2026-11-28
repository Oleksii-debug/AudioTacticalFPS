"""Independent application/catalog correction tests on real disk-backed SQLite.

These tests exercise shared works with distinct editions. Catalog presentation,
active search citations and saved historical evidence must remain consistent.
"""
from contextlib import closing

import pytest

from carr_library.application import LibraryApp
from carr_library.storage.revisions import CURRENT_SCHEMA, Storage


@pytest.fixture
def catalog_app(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Metadata research')
    for eid, language, label, volume in (
        ('e1', 'en', 'First edition', 'I'),
        ('e2', 'uk', 'Second edition', 'II'),
    ):
        result = app.add_text(workspace, 'Original title',
            f'Exact historical passage for {eid}.', authors=('Original Author',),
            language=language, edition=label, volume=volume,
            work_id='shared-work', edition_id=eid)
        assert result['state'] == 'complete'
    return app, workspace


def state(app):
    with app.read() as db:
        return {name: tuple(tuple(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY rowid'))
                for name in ('catalog_work', 'catalog_author', 'catalog_edition',
                             'revision', 'page', 'active', 'source_metadata', 'research_evidence')}


def revise(app, workspace, **changes):
    metadata = dict(title='Corrected title', authors=('Corrected Author', 'Second Author'),
                    language='sk', edition='Revised first edition', volume='III')
    metadata.update(changes)
    return app.update_metadata(workspace, 'e1', **metadata)


def test_shared_work_correction_updates_all_active_citations_and_preserves_edition_fields(catalog_app):
    app, workspace = catalog_app
    old = {h.edition_id: h for h in app.search(workspace, 'historical passage')}
    revision = revise(app, workspace)
    views = {v.edition_id: v for v in app.browse(workspace)}
    assert all(v.title == 'Corrected title' and v.authors == ('Corrected Author', 'Second Author')
               for v in views.values())
    assert (views['e1'].language, views['e1'].edition, views['e1'].volume) == (
        'sk', 'Revised first edition', 'III')
    assert (views['e2'].language, views['e2'].edition, views['e2'].volume) == (
        'uk', 'Second edition', 'II')
    for mode in ('phrase', 'thematic'):
        hits = {h.edition_id: h for h in app.search(workspace, 'historical passage',
                author='Corrected Author', title='Corrected title', mode=mode)}
        assert set(hits) == {'e1', 'e2'}
        for eid, hit in hits.items():
            assert hit.title == views[eid].title
            assert hit.author == '; '.join(views[eid].authors)
            assert hit.edition == views[eid].edition
            assert hit.revision != old[eid].revision
            assert hit.quote in app.preview(workspace, hit.ref, hit.revision)
            assert hit.verified
        assert hits['e1'].revision == revision
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
        storage.check()


def test_edition_only_correction_leaves_sibling_revision_and_metadata_unchanged(catalog_app):
    app, workspace = catalog_app
    old = {h.edition_id: h for h in app.search(workspace, 'historical passage')}
    revise(app, workspace, title='Original title', authors=('Original Author',))
    new = {h.edition_id: h for h in app.search(workspace, 'historical passage')}
    assert new['e1'].revision != old['e1'].revision
    assert new['e2'].revision == old['e2'].revision
    assert new['e2'].citation == old['e2'].citation
    assert new['e2'].quote == old['e2'].quote


def test_correction_preserves_saved_historical_citations_originals_and_other_workspace(catalog_app):
    app, workspace = catalog_app
    other = app.create_workspace('Unrelated library')
    app.add_text(other, 'Other title', 'Exact historical passage elsewhere.',
                 authors=('Other Author',), work_id='shared-work', edition_id='e1')
    topic = app.create_topic(workspace, 'Historical evidence')
    old_hits = app.search(workspace, 'historical passage')
    for hit in old_hits:
        app.save_hit(workspace, topic, hit.ref, hit.revision, f'Note for {hit.edition_id}')
    saved = app.evidence(workspace, topic)
    exported = app.export_topic(workspace, topic, format='text')
    original_assets = {p.name: p.read_bytes() for p in app.assets_dir.iterdir() if p.is_file()}
    other_before = app.search(other, 'historical passage')[0]
    revise(app, workspace)
    reopened = LibraryApp(app.root)
    assert reopened.evidence(workspace, topic) == saved
    assert reopened.export_topic(workspace, topic, format='text') == exported
    assert all(item.anchor.metadata.title == 'Original title' for item in saved)
    assert {p.name: p.read_bytes() for p in app.assets_dir.iterdir() if p.is_file()} == original_assets
    other_after = reopened.search(other, 'historical passage')[0]
    assert (other_after.revision, other_after.citation) == (other_before.revision, other_before.citation)
    assert reopened.browse(other)[0].authors == ('Other Author',)


@pytest.mark.parametrize('invalid_authors', ['Alice', {'Alice': 1}, None, ('A',) * 101])
def test_author_collection_must_be_bounded_sequence_not_scalar(catalog_app, invalid_authors):
    app, workspace = catalog_app
    before = state(app)
    with pytest.raises(ValueError, match='authors|author'):
        revise(app, workspace, authors=invalid_authors)
    assert state(app) == before


def test_second_edition_publication_failure_rolls_back_entire_metadata_correction(catalog_app, monkeypatch):
    app, workspace = catalog_app
    before = state(app)
    original_stage = Storage.stage_revision
    calls = []
    def failing_stage(self, scope, edition, *args, **kwargs):
        calls.append(edition)
        if len(calls) == 2:
            raise RuntimeError('Injected second edition publication failure')
        return original_stage(self, scope, edition, *args, **kwargs)
    monkeypatch.setattr(Storage, 'stage_revision', failing_stage)
    with pytest.raises(RuntimeError, match='second edition publication failure'):
        revise(app, workspace)
    assert len(calls) == 2
    assert state(app) == before
    monkeypatch.setattr(Storage, 'stage_revision', original_stage)
    revise(app, workspace)
    assert all(h.title == 'Corrected title' for h in app.search(workspace, 'historical passage'))


def test_archived_workspace_and_unknown_edition_fail_without_partial_changes(catalog_app):
    app, workspace = catalog_app
    before = state(app)
    with pytest.raises(ValueError, match='unknown|imported edition'):
        app.update_metadata(workspace, 'missing', title='Changed')
    assert state(app) == before
    app.archive_workspace(workspace)
    before = state(app)
    with pytest.raises(ValueError, match='archived'):
        revise(app, workspace)
    assert state(app) == before


def test_expected_metadata_rejects_stale_edit_without_overwriting_new_values(catalog_app):
    app, workspace = catalog_app
    expected = app.edition_metadata(workspace, 'e1')
    newer = LibraryApp(app.root)
    revise(newer, workspace, title='Newer title', authors=('Олена', 'Žan'))
    before = state(app)
    with pytest.raises(ValueError, match='metadata changed'):
        revise(app, workspace, expected_metadata=expected)
    assert state(app) == before
    assert app.edition_metadata(workspace, 'e1').authors == ('Олена', 'Žan')


def test_expected_metadata_binds_edition_identity_and_accepts_current_snapshot(catalog_app):
    app, workspace = catalog_app
    before = state(app)
    with pytest.raises(ValueError, match='metadata changed'):
        revise(app, workspace, expected_metadata=app.edition_metadata(workspace, 'e2'))
    assert state(app) == before
    expected = app.edition_metadata(workspace, 'e1')
    revision = revise(app, workspace, expected_metadata=expected)
    assert app.edition_metadata(workspace, 'e1').title == 'Corrected title'
    hit = next(h for h in app.search(workspace, 'historical passage') if h.edition_id == 'e1')
    assert hit.revision == revision


@pytest.mark.parametrize('damaged_edition', ['e1', 'e2'])
def test_damaged_source_digest_cannot_be_resealed_by_metadata_edit(catalog_app, damaged_edition):
    app, workspace = catalog_app
    original = f'Exact historical passage for {damaged_edition}.'.encode('utf-8')
    damaged = f'Other historical passage for {damaged_edition}.'.encode('utf-8')
    database_bytes = app.database.read_bytes()
    assert len(original) == len(damaged) and original in database_bytes
    # A same-length byte change models disk damage without altering schema,
    # disabling immutability triggers or inventing a valid replacement digest.
    app.database.write_bytes(database_bytes.replace(original, damaged))
    with pytest.raises(ValueError, match='raw page digest mismatch'):
        app.search(workspace, 'historical passage')
    before = state(app)
    with pytest.raises(ValueError, match='source text has been damaged'):
        revise(app, workspace)
    # For e2 this also rolls back the already staged e1 revision and shared edits.
    assert state(app) == before
    assert all(view.title == 'Original title' for view in app.browse(workspace))
