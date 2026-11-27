"""Real academic-schema RIS updates preserve independent bibliography and historical research.

The RIS adapter originated before academic references were added to the storage
schema. These integration cases exercise the composed application, including
reopening and backup restoration, rather than substituting a catalog mock.
"""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.ris_apply import apply_ris_metadata, prepare_ris_review
from carr_library.catalog.ris_preview import preview_ris
from carr_library.library_backup import restore_backup
from carr_library.research.academic_library import AcademicLibrary
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Storage


RIS = (b'TY  - BOOK\nTI  - Reviewed catalog title\nAU  - Reviewed catalog author\n'
       b'LA  - uk\nER  -\n')


@pytest.fixture
def v4_case(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = app.create_workspace('Academic integration')
    other = app.create_workspace('Separate library')
    for workspace in (selected, other):
        for identity, language, label in (('first', 'en', 'First'),
                                           ('second', 'sk', 'Second')):
            result = app.add_text(workspace, 'Original catalog title',
                f'Historical railway evidence in {workspace} {identity}.',
                authors=('Original catalog author',), language=language,
                edition=label, volume='II', work_id='shared-work',
                edition_id=identity)
            assert result['state'] == 'complete'
    references = {}
    for workspace in (selected, other):
        with app.write(workspace) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION
            library = AcademicLibrary(db, workspace)
            for identity in ('first', 'second', None):
                reference = library.create(
                    title=f'Independent bibliography {identity}',
                    authors=('Independent reference author',), year='2026',
                    item_type='article', container_title='Research journal',
                    pages='10–24', doi=f'10.5555/{identity}',
                    edition_id=identity, notes=f'Private reading notes {workspace}',
                    tags=('History', 'Україна'), reading_status='reading',
                    progress=35, priority='high', rating=4)
                references[(workspace, identity)] = reference
            unlinked = references[(workspace, None)]
            references[(workspace, None)] = library.archive(
                unlinked.id, expected=unlinked)
    topic = app.create_topic(selected, 'Historical research')
    for hit in app.search(selected, 'railway evidence'):
        app.save_hit(selected, topic, hit.ref, hit.revision,
                     'Private evidence note, separate from bibliography')
    return app, selected, other, references, topic


def _references(app, expected):
    for (workspace, _), reference in expected.items():
        with app.read(workspace) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION
            assert AcademicLibrary(db, workspace).get(reference.id) == reference


def _revisions(app, workspace):
    with app.read(workspace) as db:
        return dict(db.execute(
            'SELECT edition,revision FROM active WHERE workspace=?', (workspace,)))


def _state(app):
    tables = ('catalog_work', 'catalog_author', 'catalog_edition', 'revision',
              'page', 'active', 'source_metadata', 'research_evidence',
              'academic_reference')
    with app.read() as db:
        return {table: tuple(tuple(row) for row in db.execute(
            f'SELECT * FROM {table} ORDER BY rowid')) for table in tables}


@pytest.mark.parametrize('selected_fields', (
    {'language': 'uk'},
    {'title': 'Reviewed catalog title', 'authors': ('Reviewed catalog author',)},
))
def test_ris_update_and_v4_backup_keep_references_and_evidence_independent(
        v4_case, tmp_path, selected_fields):
    app, workspace, other, references, topic = v4_case
    before = {row.edition_id: row for row in app.browse(workspace)}
    foreign = app.browse(other)
    revisions = _revisions(app, workspace)
    saved = app.evidence(workspace, topic)
    exported = app.export_topic(workspace, topic, format='text')
    originals = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    record, = preview_ris(RIS)
    reviewed = prepare_ris_review(app, workspace, 'first', record)

    revision = apply_ris_metadata(app, workspace, reviewed, selected_fields)

    reopened = LibraryApp(app.root)
    after = {row.edition_id: row for row in reopened.browse(workspace)}
    assert after['first'] == replace(before['first'], **selected_fields)
    shared = {key: value for key, value in selected_fields.items()
              if key in ('title', 'authors')}
    assert after['second'] == replace(before['second'], **shared)
    assert reopened.browse(other) == foreign
    current = _revisions(reopened, workspace)
    assert current['first'] == revision != revisions['first']
    assert (current['second'] != revisions['second']) is bool(shared)
    assert reopened.evidence(workspace, topic) == saved
    assert reopened.export_topic(workspace, topic, format='text') == exported
    assert {path.name: path.read_bytes() for path in reopened.assets_dir.iterdir()} == originals
    _references(reopened, references)

    backup = tmp_path / 'backup'
    digest = reopened.backup(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    _references(restored, references)
    assert restored.browse(workspace) == reopened.browse(workspace)
    assert restored.browse(other) == foreign
    assert restored.evidence(workspace, topic) == saved
    assert restored.export_topic(workspace, topic, format='text') == exported
    assert {path.name: path.read_bytes() for path in restored.assets_dir.iterdir()} == originals


def test_reference_edit_after_ris_review_is_preserved(v4_case):
    app, workspace, _, references, _ = v4_case
    record, = preview_ris(RIS)
    reviewed = prepare_ris_review(app, workspace, 'first', record)
    original = references[(workspace, 'first')]
    with app.write(workspace) as db:
        references[(workspace, 'first')] = AcademicLibrary(db, workspace).update(
            original.id, expected=original, notes='Newer independent reading notes',
            reading_status='read', progress=100)

    apply_ris_metadata(app, workspace, reviewed, {'language': 'uk'})

    _references(LibraryApp(app.root), references)


def test_failed_shared_ris_publication_rolls_back_with_populated_v4_tables(
        v4_case, monkeypatch):
    app, workspace, _, references, _ = v4_case
    record, = preview_ris(RIS)
    reviewed = prepare_ris_review(app, workspace, 'first', record)
    before = _state(app)
    stage = Storage.stage_revision
    calls = []

    def fail_after_first(self, scope, edition_id, *args, **kwargs):
        calls.append(edition_id)
        if len(calls) == 2:
            raise RuntimeError('Synthetic second-edition publication failure')
        return stage(self, scope, edition_id, *args, **kwargs)

    monkeypatch.setattr(Storage, 'stage_revision', fail_after_first)
    with pytest.raises(RuntimeError, match='second-edition publication failure'):
        apply_ris_metadata(app, workspace, reviewed,
                           {'title': 'Reviewed catalog title'})
    assert len(calls) == 2
    reopened = LibraryApp(app.root)
    assert _state(reopened) == before
    _references(reopened, references)
