"""Academic dialog controllers against real application SQLite transactions.

These checks exercise forms, scope, selections and exported content. Native wx
controls and actual focus are exercised separately by windows_academic_smoke.py.
"""
from dataclasses import replace
import json
from types import MappingProxyType

import pytest

from carr_library.application import LibraryApp
from carr_library.research.academic_library import AcademicLibrary
from carr_library.ui.academic_library_dialog import (
    AcademicController, PAGE_SIZE, form_values, initial_fields,
    book_link_labels, reference_details, reference_labels, selected_row, statistics_text,
)


@pytest.fixture
def controller(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Академічна бібліотека')
    return AcademicController(library, workspace)


def draft(**updates):
    values = initial_fields()
    values.update(title='Research methods', authors='Olena Researcher\nJan Researcher', tags='',
                  year='2026', progress='0', rating=0)
    values.update(updates)
    return values


def test_form_saves_metadata_and_reading_notes_without_changing_linked_source(controller):
    app, workspace = controller.library, controller.workspace
    imported = app.add_text(workspace, 'Original title', 'Exact source wording about railways.',
                            authors=('Original author',))
    hit, = app.search(workspace, 'railways')
    raw = draft(title='Bibliographic title', edition_id=imported['edition'],
                year='circa 1920', reading_status='reading', priority='high', rating=4,
                progress='35', volume='II', issue='7', pages='xi–xx',
                tags='Transport\nУкраїна\nTransport', notes='Private line one\r\nПриватний рядок два.')
    saved = controller.create(form_values(raw))
    assert saved.year == 'circa 1920'
    assert saved.tags == ('Transport', 'Україна')
    assert saved.progress == 35 and saved.rating == 4
    assert saved.notes == raw['notes']
    assert controller.list(status='reading', tag='Україна') == (saved,)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert app.browse(workspace)[0].title == 'Original title'
    assert initial_fields(saved)['notes'] == saved.notes
    reopened = AcademicController(LibraryApp(app.root), workspace)
    assert reopened.get(saved.id) == saved
    assert 'Pages:\nxi–xx' in reference_details(saved)


def test_form_preserves_equal_author_names_and_unknown_year(controller):
    values = form_values(draft(authors='Same Name\nSame Name', year=''))
    saved = controller.create(values)
    assert saved.authors == ('Same Name', 'Same Name') and saved.year == ''
    assert 'Unknown year' in reference_labels((saved,))[0]
    assert 'Year:\nUnknown' in reference_details(saved)


@pytest.mark.parametrize('changes', [
    {'title': ''}, {'progress': '20.5'}, {'progress': '-1'}, {'progress': '101'},
    {'rating': 6}, {'reading_status': 'complete'}, {'priority': None},
])
def test_invalid_form_retains_the_original_draft_and_saves_nothing(controller, changes):
    raw = draft(notes='Keep this unfinished note', **changes)
    before = dict(raw)
    with pytest.raises(ValueError):
        controller.create(form_values(raw))
    assert raw == before and controller.list() == ()


def test_editor_expected_reference_prevents_lost_updates(controller):
    first = controller.create(form_values(draft()))
    current = controller.update(first, {'notes': 'Saved from another window'})
    raw = draft(notes='Older unsaved form')
    with pytest.raises(ValueError, match='changed'):
        controller.update(first, form_values(raw))
    assert controller.get(first.id) == current
    assert raw['notes'] == 'Older unsaved form'
    with pytest.raises(ValueError, match='changed'):
        controller.archive(first)
    assert not controller.get(first.id).archived


def test_pinned_controller_rechecks_library_authorization(controller):
    row = controller.create(form_values(draft()))
    other = controller.library.create_workspace('Other library')
    foreign = AcademicController(controller.library, other)
    with pytest.raises(ValueError):
        foreign.get(row.id)
    with pytest.raises(ValueError):
        foreign.update(row, {'notes': 'Wrong library'})
    controller.library.archive_workspace(controller.workspace)
    with pytest.raises(ValueError):
        controller.create(form_values(draft(title='After archive')))
    controller.library.restore_workspace(controller.workspace)
    assert controller.get(row.id) == row


def test_import_dialog_selection_is_explicit_and_failed_batch_is_atomic(controller):
    data = json.dumps([
        {'type': 'book', 'title': 'First imported reference'},
        {'type': 'article-journal', 'title': 'Second imported reference', 'DOI': '10.5555/second'},
    ]).encode()
    preview = controller.preview(data, 'csl-json')
    assert controller.list() == ()
    invalid = replace(preview.entries[1], fields=MappingProxyType(
        {**preview.entries[1].fields, 'edition_id': 'missing-local-book'}))
    broken = replace(preview, entries=(preview.entries[0], invalid))
    with pytest.raises(ValueError):
        controller.import_selected(broken, (0, 1))
    assert controller.list() == ()
    result = controller.import_selected(preview, (1,))
    assert len(result.imported_ids) == 1
    assert controller.list()[0].title == 'Second imported reference'
    duplicate_preview = controller.preview(data, 'csl-json')
    assert duplicate_preview.entries[1].duplicate_ids == result.imported_ids
    skipped = controller.import_selected(duplicate_preview, (1,), skip_duplicates=True)
    assert skipped.imported_ids == () and skipped.skipped_indices == (1,)


@pytest.mark.parametrize('format', ['text', 'ris', 'bibtex', 'csl-json', 'csv'])
def test_export_scope_and_personal_notes_are_explicit(controller, format):
    first = controller.create(form_values(draft(title='Active reference', notes='PrivateNoteToken')))
    archived = controller.create(form_values(draft(title='Archived reference')))
    controller.archive(archived)
    foreign = controller.library.create_workspace('Other project')
    AcademicController(controller.library, foreign).create(form_values(draft(title='ForeignReferenceToken')))
    exported = controller.export(format=format)
    assert 'Active reference' in exported
    assert 'Archived reference' not in exported and 'ForeignReferenceToken' not in exported
    assert 'PrivateNoteToken' not in exported
    selected = controller.export(identity=first.id, format=format, include_notes=True)
    assert 'PrivateNoteToken' in selected


def test_export_uses_existing_destination_guard(controller, tmp_path):
    controller.create(form_values(draft()))
    target = tmp_path / 'bibliography.txt'
    controller.export_to(target)
    assert 'Research methods' in target.read_text()
    private = controller.library.root / 'do-not-overwrite.txt'
    private.write_text('Existing private file')
    with pytest.raises(ValueError):
        controller.export_to(private)
    assert private.read_text() == 'Existing private file'


def test_reference_pages_have_distinct_labels_and_all_active_export(controller):
    with controller.library.write(controller.workspace) as db:
        service = AcademicLibrary(db, controller.workspace)
        created = tuple(service.create(title='Equal title', authors=('Equal author',),
                                        reading_status='reading', tags=('Seminar',))
                        for _ in range(PAGE_SIZE + 3))
    first = controller.list(status='reading', tag='Seminar', limit=PAGE_SIZE + 1)
    second = controller.list(status='reading', tag='Seminar', limit=PAGE_SIZE + 1, offset=PAGE_SIZE)
    assert len(first) == PAGE_SIZE + 1 and len(second) == 3
    labels = reference_labels(first[:PAGE_SIZE]) + reference_labels(second, offset=PAGE_SIZE)
    assert len(labels) == len(set(labels)) == len(created)
    assert {row.id for row in first[:PAGE_SIZE]} | {row.id for row in second} == {row.id for row in created}
    assert len(json.loads(controller.export(format='csl-json'))) == len(created)


@pytest.mark.parametrize('index', [-1, 1, True, '0'])
def test_missing_reference_choice_never_defaults_to_last_reference(controller, index):
    row = controller.create(form_values(draft()))
    with pytest.raises(ValueError):
        selected_row((row,), index)


def test_linked_document_uses_verified_view_copy_and_fresh_reference(controller):
    app = controller.library
    imported = app.add_text(controller.workspace, 'Linked original', 'Source text remains exact.')
    initial = controller.from_book(imported['edition'])
    row = controller.create(initial)
    viewed = controller.prepare_document(row)
    original = app.assets.resolve(imported['asset'])
    assert viewed.path != original and viewed.path.read_bytes() == original.read_bytes()
    controller.update(row, {'title': 'Changed bibliography'})
    with pytest.raises(ValueError, match='changed'):
        controller.prepare_document(row)
    standalone = controller.create(form_values(draft(title='No local file')))
    with pytest.raises(ValueError, match='no linked'):
        controller.prepare_document(standalone)


def test_duplicate_suggestions_and_reading_overview_are_readable(controller):
    first = controller.create(form_values(draft(doi='10.5555/example', priority='high', reading_status='reading')))
    controller.create(form_values(draft(title='Alternate description', doi='https://doi.org/10.5555/EXAMPLE')))
    text = controller.duplicate_report()
    assert 'Research methods' in text and 'Alternate description' in text and 'Reason:' in text
    assert 'No references are merged or deleted automatically.' in text
    overview = statistics_text(controller.statistics())
    assert 'Active references: 2' in overview and 'Currently reading: 1' in overview
    archived = controller.archive(first)
    assert archived.archived
    assert len(controller.list()) == 1 and len(controller.list(include_archived=True)) == 2
    restored = controller.archive(archived, archived=False)
    assert not restored.archived and len(controller.list()) == 2


def test_local_book_choices_explain_editions_and_keep_the_selected_source(controller):
    app, workspace = controller.library, controller.workspace
    app.add_text(workspace, 'Same book', 'First edition text.', authors=('Same Author',),
                 edition='First', volume='I', language='en')
    second = app.add_text(workspace, 'Same book', 'Second edition source.', authors=('Same Author',),
                          edition='Second', volume='II', language='uk')
    books = controller.books()
    labels = book_link_labels(books)
    index = next(index for index, row in enumerate(books) if row.edition_id == second['edition'])
    assert 'Edition: Second' in labels[index]
    assert 'Volume: II' in labels[index] and 'Language: uk' in labels[index]
    selected = selected_row(books, index)
    reference = controller.create(controller.from_book(selected.edition_id))
    source = controller.prepare_document(reference)
    assert source.path.read_bytes() == app.assets.resolve(second['asset']).read_bytes()
    assert reference.edition_id == selected.edition_id == second['edition']
