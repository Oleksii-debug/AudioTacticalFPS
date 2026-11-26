"""Installed-package academic workflows with real sources, SQLite and backups."""
from dataclasses import replace
import json
from types import MappingProxyType

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.research.academic_library import AcademicLibrary


def test_academic_reference_roundtrip_preserves_source_evidence(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Академічна бібліотека')
    other = app.create_workspace('Separate project')
    topic = app.create_topic(workspace, 'Transport literature')
    imported = app.add_text(workspace, 'Retained source',
        'Railway archives preserve the original wording.\r\nДослівний український текст.',
        authors=('Original Author',), edition='First')
    hit, = app.search(workspace, 'original wording')
    evidence_id = app.save_hit(workspace, topic, hit.ref, hit.revision,
                               'Independent interpretation of this quotation.')
    before = app.evidence(workspace, topic)[0]
    original = app.assets.resolve(imported['asset']).read_bytes()

    with app.write(workspace) as db:
        references = AcademicLibrary(db, workspace)
        reference = references.create(title='Bibliographic description',
            authors=('Researcher, Olena',), item_type='article', year='2026',
            container_title='Synthetic Transport Review', volume='4', issue='2', pages='17–31',
            doi='https://doi.org/10.5555/SYNTHETIC', edition_id=imported['edition'])
        reference = references.update(reference.id, expected=reference,
            title='Edited bibliography only', reading_status='reading', priority='high',
            rating=4, progress=35, tags=('Transport', 'Україна'), notes='Private reading note.')
        standalone = references.create(title='A reference without a file',
            item_type='book', isbn='9780306406157', reading_status='unread')
    assert len(app.browse(workspace)) == 1
    assert app.evidence(workspace, topic)[0] == before
    assert app.assets.resolve(imported['asset']).read_bytes() == original
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert before.evidence_id == evidence_id

    reopened = LibraryApp(app.root)
    with reopened.read(workspace) as db:
        references = AcademicLibrary(db, workspace)
        assert references.get(reference.id) == reference
        assert references.get(standalone.id).edition_id is None
        assert references.list(status='reading', tag='Україна') == (reference,)
        assert 'Private reading note.' not in references.export(format='ris')
    with reopened.read(other) as db:
        references = AcademicLibrary(db, other)
        assert references.list() == ()
        with pytest.raises(ValueError):
            references.get(reference.id)

    backup = tmp_path / 'backup'
    checksum = reopened.backup(backup)
    restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(tmp_path / 'restored')
    with restored.read(workspace) as db:
        references = AcademicLibrary(db, workspace)
        assert references.get(reference.id) == reference
        assert references.get(standalone.id) == standalone
    assert restored.evidence(workspace, topic)[0] == before
    assert restored.assets.resolve(imported['asset']).read_bytes() == original


def test_academic_import_is_atomic_and_exports_preserve_metadata(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    one = app.create_workspace('Literature review')
    two = app.create_workspace('Import roundtrip')
    records = [
        {'id': 'external-a', 'type': 'article-journal', 'title': 'Українська бібліографія',
         'author': [{'family': 'Шевченко', 'given': 'Олена'}],
         'issued': {'date-parts': [[2026, 11, 1]]},
         'container-title': 'Synthetic Journal', 'volume': '12', 'issue': '3',
         'page': '101-115', 'DOI': '10.5555/example-a'},
        {'id': 'external-b', 'type': 'book', 'title': 'Research methods',
         'author': [{'literal': 'Synthetic Academy'}], 'issued': {'date-parts': [[2025]]},
         'publisher': 'Example Publisher', 'ISBN': '9780306406157'},
    ]
    data = json.dumps(records, ensure_ascii=False).encode('utf-8')
    with app.read(one) as db:
        references = AcademicLibrary(db, one)
        preview = references.import_preview(data, format='csl-json')
        assert len(preview.entries) == 2
        assert references.list() == ()
    # An invalid second source link must roll back the first inserted reference,
    # even when the caller catches the exception and commits its outer write.
    invalid = replace(preview.entries[1], fields=MappingProxyType(
        {**preview.entries[1].fields, 'edition_id': 'missing-edition'}))
    changed = replace(preview, entries=(preview.entries[0], invalid))
    with app.write(one) as db:
        references = AcademicLibrary(db, one)
        with pytest.raises(ValueError):
            references.import_selected(changed, (0, 1))
        assert references.list() == ()
        result = references.import_selected(preview, (0, 1))
        assert len(result.imported_ids) == 2
    with app.read(one) as db:
        references = AcademicLibrary(db, one)
        article = references.get(result.imported_ids[0])
        assert (article.volume, article.issue, article.pages, article.year) == ('12', '3', '101-115', '2026')
        ris = references.export(format='ris')
        csl = json.loads(references.export(format='csl-json'))
        assert {row['title'] for row in csl} == {record['title'] for record in records}
        assert article.title in references.export(format='bibtex')
    with app.read(two) as db:
        second_preview = AcademicLibrary(db, two).import_preview(ris.encode('utf-8'), format='ris')
        assert len(second_preview.entries) == 2
    with app.write(two) as db:
        imported = AcademicLibrary(db, two).import_selected(second_preview, (0, 1))
    with app.read(two) as db:
        rows = AcademicLibrary(db, two).list()
        article = next(row for row in rows if row.doi)
        assert (article.container_title, article.volume, article.issue, article.pages) == (
            'Synthetic Journal', '12', '3', '101-115')
        assert len(imported.imported_ids) == 2
    assert app.browse(one) == app.browse(two) == ()


def test_reading_views_paginate_without_losing_exported_records(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Reading list')
    with app.write(workspace) as db:
        references = AcademicLibrary(db, workspace)
        created = [references.create(title=f'Reference {number:03}',
            reading_status='reading', priority='high' if number % 2 else 'normal',
            tags=('Seminar',)) for number in range(205)]
    with app.read(workspace) as db:
        references = AcademicLibrary(db, workspace)
        first = references.list(status='reading', tag='Seminar', limit=200)
        second = references.list(status='reading', tag='Seminar', limit=200, offset=200)
        assert len(first) == 200 and len(second) == 5
        assert {row.id for row in first + second} == {row.id for row in created}
        assert len(json.loads(references.export(format='csl-json'))) == 205
        assert references.statistics()['reading'] == 205


def test_duplicate_review_and_stale_edit_never_erase_research(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Duplicate review')
    with app.write(workspace) as db:
        references = AcademicLibrary(db, workspace)
        first = references.create(title='Original record', doi='10.5555/EXAMPLE')
        second = references.create(title='Imported description', doi='https://doi.org/10.5555/example')
        groups = references.duplicates()
        assert any(set(group.reference_ids) == {first.id, second.id} and group.exact for group in groups)
        changed = references.update(first.id, expected=first, notes='A newly saved reading note.')
        with pytest.raises(ValueError, match='changed'):
            references.update(first.id, expected=first, notes='Stale draft')
        assert references.get(first.id) == changed
        archived = references.archive(second.id, expected=second)
        assert archived.archived
        assert len(references.list()) == 1
        assert len(references.list(include_archived=True)) == 2
        assert references.archive(second.id, False, expected=archived).id == second.id
        assert len(references.list()) == 2


def test_linked_document_opens_without_search_or_quotation_authority(tmp_path):
    from carr_library.reader.source_file import launch_source_file

    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Reading sources')
    imported = app.add_text(workspace, 'Linked source', 'The retained source stays exact.')
    with app.write(workspace) as db:
        reference = AcademicLibrary(db, workspace).create(title='Reading plan entry',
            edition_id=imported['edition'])
    source = app.prepare_edition_file(workspace, reference.edition_id)
    assert source.path.read_bytes() == b'The retained source stays exact.'
    opened = []
    launch_source_file(app, source, lambda path: opened.append(path) is None)
    assert opened == [str(source.path)]
    with pytest.raises(ValueError):
        app.copy(workspace, source.ref, source.identity.revision)
    source.path.write_text('Reader notes in the viewing copy.', encoding='utf-8')
    replacement = app.prepare_edition_file(workspace, reference.edition_id)
    assert replacement.path != source.path
    assert source.path.read_text() == 'Reader notes in the viewing copy.'
    assert replacement.path.read_bytes() == app.assets.resolve(imported['asset']).read_bytes()


def test_linked_document_checks_library_and_retained_bytes_again(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Authorized library')
    other = app.create_workspace('Different library')
    imported = app.add_text(workspace, 'Source', 'The retained source stays exact.')
    with pytest.raises(ValueError):
        app.prepare_edition_file(other, imported['edition'])
    with pytest.raises(InterruptedError):
        app.prepare_edition_file(workspace, imported['edition'], cancelled=lambda: True)
    app.assets.resolve(imported['asset']).write_bytes(b'Changed source bytes.')
    with pytest.raises(ValueError):
        app.prepare_edition_file(workspace, imported['edition'])

