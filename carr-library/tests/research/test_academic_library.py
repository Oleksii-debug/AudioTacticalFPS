"""Real SQLite workflows for metadata-only references and safe interchange."""
import csv
import io
import json
import sqlite3

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.research import academic_library as academic
from carr_library.research.academic_library import (
    AcademicLibrary, normalize_doi, normalize_isbn,
    validate_academic_database,
)
from carr_library.storage.academic_schema import ACADEMIC_SCHEMA
from carr_library.storage.revisions import SCHEMA_V3


@pytest.fixture
def db(tmp_path):
    connection = sqlite3.connect(tmp_path / 'academic.sqlite', isolation_level=None)
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA recursive_triggers=ON')
    connection.executescript(SCHEMA_V3 + ACADEMIC_SCHEMA + '\nPRAGMA user_version=4;')
    for identity in ('first', 'second'):
        connection.execute('INSERT INTO workspace VALUES(?)', (identity,))
        connection.execute('INSERT INTO workspace_meta(workspace,name) VALUES(?,?)', (identity, identity))
    connection.execute('BEGIN')
    yield connection
    connection.close()


@pytest.fixture
def library(db):
    return AcademicLibrary(db, 'first')


def test_reference_only_persists_without_inventing_a_source(db, library, tmp_path):
    saved = library.create(title='Огляд літератури', authors=('Šimko, Ján', 'Іваненко, Олена'),
        item_type='article', year='2024', container_title='Research', volume='12', issue='3',
        pages='10–24', doi='https://doi.org/10.1234/ABC', isbn='0-306-40615-2',
        reading_status='reading', progress=40, priority='high', rating=4,
        tags=('Methods', 'Огляд'), notes='Private reading note\nSecond line.')
    assert saved.doi == '10.1234/abc'
    assert saved.isbn == '0306406152'
    assert saved.edition_id is None
    assert db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    validate_academic_database(db)
    db.commit()
    second = sqlite3.connect(tmp_path / 'academic.sqlite', isolation_level=None)
    try:
        second.execute('PRAGMA foreign_keys=ON')
        second.execute('BEGIN')
        assert AcademicLibrary(second, 'first').get(saved.id) == saved
    finally:
        second.close()


def test_scoped_identity_and_real_document_link(db, library):
    catalog = Catalog(db, 'first')
    catalog.add_work('work', 'Existing source')
    catalog.add_edition('edition', 'work', dict(sha256='a' * 64, mime='text/plain', size=12))
    reference = library.create(title='Reference', edition_id='edition')
    assert reference.edition_id == 'edition'
    foreign = AcademicLibrary(db, 'second')
    with pytest.raises(ValueError, match='belong to this library'):
        foreign.create(title='Wrong link', edition_id='edition')
    with pytest.raises(ValueError, match='unavailable'):
        foreign.get(reference.id)
    assert foreign.list() == ()
    assert 'Reference' not in foreign.export()
    assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1


def test_optimistic_edit_and_archive_preserve_concurrent_notes(library):
    original = library.create(title='Before', notes='Draft')
    changed = library.update(original.id, expected=original, title='After', notes='Saved elsewhere')
    with pytest.raises(ValueError, match='changed while'):
        library.update(original.id, expected=original, notes='Stale overwrite')
    with pytest.raises(ValueError, match='changed while'):
        library.archive(original.id, expected=original)
    assert library.get(original.id) == changed
    archived = library.archive(original.id, expected=changed)
    assert archived.archived is True
    assert library.list() == ()
    restored = library.archive(original.id, False, expected=archived)
    assert restored.notes == 'Saved elsewhere'
    assert restored.created_utc == original.created_utc
    assert restored.updated_utc > changed.updated_utc


@pytest.mark.parametrize('change', [dict(rating=True), dict(progress=1.5), dict(progress=101),
    dict(reading_status='complete'), dict(priority='urgent'), dict(archived=1),
    dict(title='   '), dict(authors='Single string'), dict(tags=('same', 'same')),
    dict(url='file:///C:/private.txt'), dict(url='https://user:password@example.org'),
    dict(doi='10.١٢٣٤/abc'), dict(doi='10.１２３４/abc'),
    dict(isbn='٠٣٠٦٤٠٦١٥٢'), dict(isbn='０３０６４０６１５２'),
    dict(isbn='9780306406158'), dict(title='Spoof\u202eTitle'), dict(authors=('New\nAuthor',))])
def test_invalid_values_never_publish_partial_reference(library, change):
    with pytest.raises(ValueError):
        library.create(**{'title': 'Safe', **change})
    assert library.list() == ()


def test_normalized_identifiers_are_valid_and_do_not_guess():
    assert normalize_doi(' DOI:10.1234/ABCD ') == '10.1234/abcd'
    assert normalize_isbn('ISBN-13: 978-0-306-40615-7') == '9780306406157'
    assert normalize_isbn('0-8044-2957-X') == '080442957X'
    assert normalize_doi('') == normalize_isbn('') == ''


def test_search_tags_pagination_and_reading_sort_are_scoped(db, library):
    first = library.create(title='Альфа', authors=('ŠIMKO',), tags=('selected',), priority='low')
    second = library.create(title='Бета', notes='Straße', tags=('prefix"selected',),
                            reading_status='reading', priority='high')
    third = library.create(title='Гамма', tags=('selected', 'end'), year='2026')
    AcademicLibrary(db, 'second').create(title='Бета', notes='Straße', tags=('selected',))
    assert library.list(query='strasse') == (second,)
    assert library.list(query='šimko') == (first,)
    assert library.list(status='reading') == (second,)
    assert library.list(tag='selected') == (first, third)
    assert library.list(sort='priority')[0] == second
    whole = library.list()
    assert library.list(limit=2) + library.list(limit=2, offset=2) == whole
    assert library.list(query='no matches') == ()


def test_year_sort_puts_known_numeric_years_before_unknown_dates(library):
    old = library.create(title='Older', year='950')
    new = library.create(title='Newer', year='2026')
    unknown = library.create(title='Undated', year='n.d.')
    assert library.list(sort='year') == (new, old, unknown)


def test_duplicate_suggestions_are_explained_and_never_merge(db, library):
    one = library.create(title='Original', doi='10.1234/a', isbn='0306406152')
    two = library.create(title='Another edition', doi='https://doi.org/10.1234/A', isbn='9780306406157')
    third = library.create(title='Common title', authors=('Author',), year='2020')
    fourth = library.create(title='Common title!', authors=('Author',), year='2020')
    AcademicLibrary(db, 'second').create(title='Original', doi='10.1234/a')
    groups = library.duplicates()
    assert any(g.reason == 'Same DOI' and set(g.reference_ids) == {one.id, two.id} and g.exact for g in groups)
    assert any(g.reason == 'Same ISBN' and set(g.reference_ids) == {one.id, two.id} and g.exact for g in groups)
    assert any(not g.exact and set(g.reference_ids) == {third.id, fourth.id} for g in groups)
    assert len(library.list()) == 4
    library.archive(two.id)
    assert not any(g.exact for g in library.duplicates())


def test_sparse_metadata_never_creates_title_only_duplicate_groups(library):
    library.create(title='Introduction')
    library.create(title='Introduction')
    assert library.duplicates() == ()


def test_archived_workspace_checks_keep_references_but_deny_access(db, library):
    row = library.create(title='Keep me', notes='Retained')
    library.archive(row.id)
    db.execute("UPDATE workspace_meta SET archived=1 WHERE workspace='first'")
    validate_academic_database(db)
    with pytest.raises(ValueError, match='missing or archived'):
        library.get(row.id)
    assert db.execute('SELECT count(*) FROM academic_reference').fetchone()[0] == 1


@pytest.mark.parametrize('column,value', [('authors', '{}'), ('tags', '["a", "a"]'),
                                        ('search_text', 'forged search'), ('doi', '10.1234/UPPER')])
def test_full_validation_rejects_corrupted_metadata_and_search_cache(db, library, column, value):
    row = library.create(title='Valid')
    db.execute(f'UPDATE academic_reference SET {column}=? WHERE id=?', (value, row.id))
    with pytest.raises(ValueError):
        validate_academic_database(db)


def test_identity_and_delete_guards_keep_archival_history(db, library):
    row = library.create(title='Original')
    for sql in ('DELETE FROM academic_reference WHERE id=?',
                "UPDATE academic_reference SET workspace='second' WHERE id=?",
                "UPDATE academic_reference SET created_utc='2020-01-01T00:00:00.000000Z' WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, (row.id,))
    assert library.get(row.id) == row


def test_csl_preview_is_read_only_shows_unsupported_and_batch_duplicates(library):
    existing = library.create(title='Already present', doi='10.1234/one')
    data = json.dumps([
        {'type': 'article-journal', 'title': 'Paper', 'author': [{'family': 'Šimko', 'given': 'Ján'}],
         'issued': {'date-parts': [[2024, 5, 3]]}, 'DOI': '10.1234/ONE',
         'volume': 12, 'issue': 3, 'page': '10–24', 'attachments': [{'path': 'C:/private/book.pdf'}]},
        {'type': 'book', 'title': 'Other', 'DOI': '10.1234/one'}], ensure_ascii=False).encode()
    preview = library.import_preview(data, format='csl-json')
    assert len(library.list()) == 1
    assert preview.entries[0].authors == ('Šimko, Ján',)
    assert preview.entries[0].fields['year'] == '2024'
    assert preview.entries[0].fields['volume'] == '12'
    assert preview.entries[0].duplicate_ids == (existing.id,)
    assert preview.entries[0].duplicate_indices == (1,)
    assert any('attachments' in warning for warning in preview.entries[0].warnings)
    with pytest.raises(TypeError):
        preview.entries[0].fields['title'] = 'mutation'


def test_csl_selected_import_is_explicit_and_rechecks_duplicates(library):
    preview = library.import_preview(b'[{"title":"One","DOI":"10.1234/one"},{"title":"Two"}]',
                                     format='csl-json')
    first = library.create(title='Added after preview', doi='10.1234/one')
    result = library.import_selected(preview, (0, 1), skip_duplicates=True)
    assert len(result.imported_ids) == 1
    assert result.skipped_indices == (0,)
    assert result.duplicate_ids == (first.id,)
    assert library.get(result.imported_ids[0]).title == 'Two'
    assert len(library.list()) == 2


def test_import_selection_is_not_implicitly_authorized_for_another_library(db, library):
    preview = library.import_preview(b'[{"title":"One"}]', format='csl-json')
    with pytest.raises(ValueError, match='cross-library'):
        AcademicLibrary(db, 'second').import_selected(preview, (0,))
    with pytest.raises(ValueError, match='repeated'):
        library.import_selected(preview, (0, 0))
    with pytest.raises(ValueError, match='Select at least'):
        library.import_selected(preview, ())
    assert library.list() == ()


def test_failed_later_insert_rolls_back_whole_selected_batch(db, library, monkeypatch):
    before = library.create(title='Preserve previous work')
    preview = library.import_preview(b'[{"title":"First"},{"title":"Second"}]', format='csl-json')
    class ConstantIdentity:
        hex = 'same-identity'
    monkeypatch.setattr(academic, 'uuid4', ConstantIdentity)
    with pytest.raises(sqlite3.IntegrityError):
        library.import_selected(preview, (0, 1))
    assert db.in_transaction
    assert library.list() == (before,)
    # The caller can catch the failed batch and commit unrelated prior work.
    db.commit()
    db.execute('BEGIN')
    assert library.get(before.id) == before


@pytest.mark.parametrize('format,data', [
    ('ris', b'TY  - BOOK\nTI  - Incomplete\n'),
    ('ris', b'TY  - BOOK\nTI  - One\nER  -\nTY  - BOOK\nTI  - Incomplete'),
    ('ris', b'TY  - BOOK\nTI  - One\nTI  - Conflicting\nER  -\n'),
    ('csl-json', b'[{"title":"A","title":"B"}]'),
    ('csl-json', b'[{"title":"A"},{"title":false}]'),
    ('csl-json', b'[{"title":"A","issued":{"date-parts":[[true]]}}]'),
    ('csl-json', b'[{"title":"A","volume":NaN}]'),
    ('csl-json', b'[{"title":"A","URL":"file:///private"}]'),
    ('csl-json', b'[{"title":"A","type":"\\u202eunsafe"}]'),
    ('csl-json', b'[{"title":"A","bad\\nfield":"ignored"}]'),
    ('csl-json', b'\xff'),
])
def test_invalid_import_never_returns_a_partial_batch(library, format, data):
    with pytest.raises(ValueError):
        library.import_preview(data, format=format)
    assert library.list() == ()


def test_import_count_and_byte_limits_are_explicit(library, monkeypatch):
    monkeypatch.setattr(academic, 'MAX_IMPORT_BYTES', 100)
    with pytest.raises(ValueError, match='8 MiB'):
        library.import_preview(b' ' * 101, format='csl-json')
    monkeypatch.setattr(academic, 'MAX_IMPORT_RECORDS', 1)
    with pytest.raises(ValueError, match='2,000'):
        library.import_preview(b'[{"title":"One"},{"title":"Two"}]', format='csl-json')


def test_ris_retains_ordered_names_metadata_multiline_and_explicit_ignored_fields(library):
    data = ('TY  - JOUR\nTI  - Дослідження\nAU  - Šimko, Ján\nA1  - Research and Testing\n'
        'PY  - 2024\nY1  - 2024/05/03\nT2  - Journal\nVL  - 12\nIS  - 3\n'
        'SP  - 10\nEP  - 24\nDO  - 10.1234/ABC\nSN  - 1234-5678\n'
        'AB  - First line\n      Second line\nN1  - Private note\n      Next line\n'
        'UR  - https://example.org/a;b\nL1  - C:/private.pdf\nER  -\n').encode()
    preview = library.import_preview(data)
    entry = preview.entries[0]
    assert entry.authors == ('Šimko, Ján', 'Research and Testing')
    assert entry.fields['abstract'] == 'First line\nSecond line'
    assert entry.fields['notes'] == 'Private note\nNext line'
    assert entry.fields['pages'] == '10-24'
    assert entry.fields['url'] == 'https://example.org/a;b'
    assert entry.fields['isbn'] == ''
    assert any('ISSN' in warning for warning in entry.warnings)
    assert any('L1' in warning for warning in entry.warnings)
    assert len(library.import_selected(preview, (0,)).imported_ids) == 1


@pytest.mark.parametrize('format', ['ris', 'bibtex', 'csl-json', 'csv', 'text'])
def test_export_omits_personal_notes_unless_explicitly_selected(library, format):
    library.create(title='Public metadata', authors=('Ján Šimko',), notes='PRIVATESENTINEL')
    assert 'PRIVATESENTINEL' not in library.export(format=format)
    assert 'PRIVATESENTINEL' in library.export(format=format, include_notes=True)


def test_exports_use_selected_scope_and_do_not_silently_export_only_first_page(db, library):
    rows = tuple(library.create(title=f'Book {number}') for number in range(5))
    other = AcademicLibrary(db, 'second').create(title='PRIVATE_OTHER_LIBRARY')
    library.archive(rows[-1].id)
    all_rows = json.loads(library.export(format='csl-json'))
    assert {row['id'] for row in all_rows} == {row.id for row in rows[:-1]}
    one = json.loads(library.export((rows[2].id,), format='csl-json'))
    assert [row['id'] for row in one] == [rows[2].id]
    with pytest.raises(ValueError, match='unavailable'):
        library.export((other.id,), format='text')


def test_csv_untrusted_text_is_formula_safe_and_reversible(library):
    row = library.create(title='=HYPERLINK("https://example.org")', authors=('=2+3',),
        notes='\t=2+3\nUnicode: Україна', tags=('+formula',))
    result = tuple(csv.DictReader(io.StringIO(library.export(format='csv', include_notes=True))))
    assert len(result) == 1
    assert result[0]['title_json'].startswith('"')
    assert result[0]['notes_json'].startswith('"')
    assert json.loads(result[0]['title_json']) == row.title
    assert json.loads(result[0]['notes_json']) == row.notes
    assert json.loads(result[0]['authors_json']) == list(row.authors)


def test_ris_tag_injection_remains_inside_the_abstract_and_notes(library):
    original = library.create(title='Real item', authors=('A and B',),
        abstract='Before\nER  -\nTY  - BOOK\nTI  - Forged',
        notes='Private\nTY  - GEN\nER  -', url='https://example.org/a;b')
    exported = library.export(format='ris', include_notes=True)
    preview = library.import_preview(exported.encode())
    assert len(preview.entries) == 1
    values = preview.entries[0].fields
    assert values['title'] == original.title
    assert values['authors'] == original.authors
    assert values['abstract'] == original.abstract
    assert values['notes'] == original.notes
    assert values['url'] == original.url


def test_bibtex_escapes_syntax_and_does_not_invent_doctoral_degree(library):
    library.create(title=r'Unbalanced } \input{private}', authors=('Org and Partners',), item_type='thesis')
    result = library.export(format='bibtex')
    assert '\\input{private}' not in result
    assert '{\\textbraceright}' in result
    assert '@phdthesis' not in result
    assert 'howpublished = {Thesis}' in result
    assert sum(line.startswith('@') for line in result.splitlines()) == 1


def test_csl_literal_author_and_identifier_roundtrip(library):
    original = library.create(title='Unicode: Україна', authors=('Šimko, Ján', 'Research and Testing'),
        item_type='article', year='2025', container_title='Studies', volume='4', issue='2',
        pages='18–32', doi='10.1234/AbC', abstract='First\nSecond')
    preview = library.import_preview(library.export(format='csl-json').encode(), format='csl-json')
    fields = preview.entries[0].fields
    for name in ('title', 'authors', 'item_type', 'year', 'container_title', 'volume',
                 'issue', 'pages', 'doi', 'abstract'):
        assert fields[name] == getattr(original, name)


def test_statistics_separate_archived_records_from_active_reading_work(library):
    one = library.create(title='Unread')
    two = library.create(title='Reading', reading_status='reading', priority='high', year='2025')
    three = library.create(title='Done', reading_status='read', doi='10.1234/done', authors=('Author',))
    library.archive(one.id)
    stats = library.statistics()
    assert stats['total'] == 3 and stats['active'] == 2 and stats['archived'] == 1
    assert stats['reading'] == stats['read'] == stats['high_priority'] == 1
    assert stats['unread'] == 0
    assert stats['missing_identifier'] == stats['missing_author'] == stats['missing_year'] == 1


def test_doi_proxy_percent_decoding_is_once_and_bare_percent_is_literal():
    assert normalize_doi('https://doi.org/10.1000/ABC%2Fdef') == '10.1000/abc/def'
    assert normalize_doi('https://doi.org/10.1000/%C3%84') == '10.1000/Ä'
    assert normalize_doi('https://doi.org/10.1000/abc%252Fdef') == '10.1000/abc%2fdef'
    assert normalize_doi('10.1000/abc%2Fdef') == '10.1000/abc%2fdef'
    assert normalize_doi('10.1000/Ä') != normalize_doi('10.1000/ä')
    assert normalize_doi('10.23/Old') == '10.23/old'
    assert normalize_doi('10.500.100/Subprefix') == '10.500.100/subprefix'
    assert normalize_doi('10.1234567890123/Long') == '10.1234567890123/long'


@pytest.mark.parametrize('doi', ['https://doi.org.evil.example/10.1000/test',
    'https://user@doi.org/10.1000/test', 'https://doi.org:443/10.1000/test',
    'https://doi.org/10.1000/test?tracking=1', 'https://doi.org/10.1000/test#fragment',
    'https://doi.org/10.1000/a%2G', 'https://doi.org/10.1000/a%FF',
    'https://doi.org/10.1000/a%00'])
def test_doi_resolver_urls_reject_ambiguous_or_malformed_input(doi):
    with pytest.raises(ValueError):
        normalize_doi(doi)


def test_non_ascii_doi_case_never_causes_wrong_duplicate_skip(library):
    library.create(title='First paper', doi='10.1000/Ä')
    preview = library.import_preview(json.dumps([{'title': 'Second paper', 'DOI': '10.1000/ä'}]).encode(),
                                     format='csl-json')
    assert preview.entries[0].duplicate_ids == ()
    assert len(library.import_selected(preview, (0,), skip_duplicates=True).imported_ids) == 1
    assert library.duplicates() == ()


def test_ris_full_publication_title_precedes_journal_abbreviation_with_warning(library):
    data = (b'TY  - JOUR\nTI  - Paper\nT2  - Journal of Library Studies\n'
            b'JF  - Journal of Library Studies\nJO  - J. Libr. Stud.\nER  -\n')
    preview = library.import_preview(data)
    assert preview.entries[0].fields['container_title'] == 'Journal of Library Studies'
    assert any('J. Libr. Stud.' in warning for warning in preview.entries[0].warnings)


@pytest.mark.parametrize('operation', ['preview', 'duplicates', 'export', 'selected_import'])
def test_reference_scan_byte_budget_rejects_before_decoding(db, library, monkeypatch, operation):
    preview = library.import_preview(b'[{"title":"Incoming"}]', format='csl-json')
    for number in range(2):
        library.create(title=f'Reference {number}', notes='Ж' * 10000)
    before = db.total_changes
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 30000, raising=False)
    original = academic._row_reference
    decoded = []

    def decode(row):
        decoded.append(row)
        return original(row)

    monkeypatch.setattr(academic, '_row_reference', decode)
    with pytest.raises(ValueError, match='metadata exceeds'):
        if operation == 'preview':
            library.import_preview(b'TY  - BOOK\nTI  - Incoming\nER  -\n')
        elif operation == 'duplicates':
            library.duplicates()
        elif operation == 'export':
            library.export()
        else:
            library.import_selected(preview, (0,))
    assert decoded == []
    assert db.total_changes == before
    assert db.in_transaction
    assert db.execute('SELECT COUNT(*) FROM academic_reference').fetchone()[0] == 2


def test_reference_scan_count_budget_rejects_before_decoding(db, library, monkeypatch):
    for number in range(3):
        library.create(title=f'Reference {number}')
    monkeypatch.setattr(academic, 'MAX_REFERENCES', 2)
    original = academic._row_reference
    decoded = []

    def decode(row):
        decoded.append(row)
        return original(row)

    monkeypatch.setattr(academic, '_row_reference', decode)
    with pytest.raises(ValueError, match='references per library'):
        library.duplicates()
    assert decoded == []
    assert db.execute('SELECT COUNT(*) FROM academic_reference').fetchone()[0] == 3


def test_reference_scan_budget_excludes_foreign_and_archived_data(db, library, monkeypatch):
    active = library.create(title='Selected', doi='10.1234/selected')
    library.create(title='Archived', archived=True, notes='Ж' * 10000, doi='10.1234/selected')
    AcademicLibrary(db, 'second').create(title='Foreign', notes='Ж' * 10000, doi='10.1234/selected')
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 4096, raising=False)
    assert library._all() == (active,)
    preview = library.import_preview(b'[{"title":"Incoming","DOI":"10.1234/selected"}]',
                                     format='csl-json')
    assert preview.entries[0].duplicate_ids == (active.id,)
    assert library.duplicates() == ()
    assert [row['id'] for row in json.loads(library.export(format='csl-json'))] == [active.id]
    with pytest.raises(ValueError, match='metadata exceeds'):
        library._all(include_archived=True)


def test_reference_scan_counts_stored_utf8_bytes_with_an_inclusive_boundary(db, library, monkeypatch):
    row = library.create(title='Дослідження 📚', notes='Ж' * 200, authors=('Šimko, Ján',))
    stored = db.execute(f'SELECT {academic._SELECT} FROM academic_reference').fetchone()
    payload_size = sum(len(str(value).encode('utf-8')) for value in stored if value is not None)
    character_count = sum(len(str(value)) for value in stored if value is not None)
    assert character_count < payload_size
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', payload_size, raising=False)
    assert library._all() == (row,)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', payload_size - 1)
    with pytest.raises(ValueError, match='metadata exceeds'):
        library._all()
    assert library.get(row.id) == row


def test_reference_scan_count_boundary_and_empty_library(library, monkeypatch):
    monkeypatch.setattr(academic, 'MAX_REFERENCES', 2)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 0, raising=False)
    assert library._all() == ()
    assert library.duplicates() == ()
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 4096)
    one = library.create(title='One')
    two = library.create(title='Two')
    assert library._all() == tuple(sorted((one, two), key=lambda row: row.id))


def test_reference_scan_keeps_full_stored_metadata_validation(db, library, monkeypatch):
    row = library.create(title='Authentic')
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 4096, raising=False)
    db.execute('UPDATE academic_reference SET search_text=? WHERE id=?', ('changed', row.id))
    with pytest.raises(ValueError, match='metadata or search index has changed'):
        library.duplicates()


def test_duplicate_preview_budget_rejects_fanout_without_mutating_references(db, library, monkeypatch):
    for number in range(3):
        library.create(title=f'Existing {number}', doi='10.1234/shared')
    data = json.dumps([{'title': f'Incoming {number}', 'DOI': '10.1234/shared'}
                       for number in range(3)]).encode()
    before = db.total_changes
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 10, raising=False)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format='csl-json')
    assert db.total_changes == before
    assert db.in_transaction
    assert len(library.list()) == 3


def test_duplicate_preview_budget_preserves_sorted_deduplicated_matches(db, library, monkeypatch):
    shared = dict(title='Common title', authors=('Author',), year='2020',
                  doi='10.1234/shared', isbn='0306406152')
    first = library.create(**shared)
    second = library.create(**shared)
    AcademicLibrary(db, 'second').create(**shared)
    library.create(**shared, archived=True)
    data = json.dumps([{'type': 'book', 'title': 'Common title', 'author': [{'literal': 'Author'}],
                       'issued': {'date-parts': [[2020]]}, 'DOI': '10.1234/shared',
                       'ISBN': '9780306406157'}] * 2).encode()
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 24, raising=False)
    preview = library.import_preview(data, format='csl-json')
    for index, entry in enumerate(preview.entries):
        assert entry.duplicate_ids == tuple(sorted((first.id, second.id)))
        assert entry.duplicate_indices == (1 - index,)
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 23)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format='csl-json')


@pytest.mark.parametrize('format', ['ris', 'csl-json', 'bibtex'])
@pytest.mark.parametrize('slots', [4, 5])
def test_duplicate_preview_budget_rejects_before_constructing_the_first_entry(library, monkeypatch,
                                                                           format, slots):
    for number in range(3):
        library.create(title=f'Existing {number}', doi='10.1234/shared')
    if format == 'ris':
        data = ''.join(f'TY  - BOOK\nTI  - Incoming {number}\nDO  - 10.1234/shared\nER  -\n'
                       for number in range(2)).encode()
    elif format == 'bibtex':
        data = ''.join(f'@book{{incoming{number},title={{Incoming {number}}},doi={{10.1234/shared}}}}'
                       for number in range(2)).encode()
    else:
        data = json.dumps([{'title': f'Incoming {number}', 'DOI': '10.1234/shared'}
                           for number in range(2)]).encode()
    # Each row costs five slots. Even when the first row fits exactly, the
    # complete preflight must reject the later excess before creating any row.
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', slots, raising=False)

    def unexpected_entry(*args, **kwargs):
        pytest.fail('An oversized duplicate result was materialized before the guard.')

    monkeypatch.setattr(academic, 'ImportEntry', unexpected_entry)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format=format)


def test_duplicate_preview_accepts_maximum_nonduplicate_batch(library):
    data = json.dumps([{'title': f'Incoming {number}', 'DOI': f'10.1234/{number}'}
                       for number in range(academic.MAX_IMPORT_RECORDS)]).encode()
    preview = library.import_preview(data, format='csl-json')
    assert len(preview.entries) == academic.MAX_IMPORT_RECORDS
    assert all(entry.duplicate_ids == entry.duplicate_indices == () for entry in preview.entries)
    assert library.list() == ()


def test_duplicate_preview_default_budget_handles_many_matching_references(db, library):
    for number in range(2000):
        library.create(title=f'Existing {number}', doi='10.1234/shared')
    data = json.dumps([{'title': f'Incoming {number}', 'DOI': '10.1234/shared'}
                       for number in range(2000)]).encode()
    before = db.total_changes
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format='csl-json')
    assert db.total_changes == before
    assert db.execute('SELECT COUNT(*) FROM academic_reference').fetchone()[0] == 2000


@pytest.mark.parametrize('operation', ['preview', 'duplicates', 'export'])
def test_reference_payload_limit_precedes_materialization(db, library, monkeypatch, operation):
    for title in ('First reference', 'Second reference'):
        library.create(title=title, notes='Ж' * 10000)
    before = db.total_changes
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 30000)
    def unexpected_decoder(_row):
        pytest.fail('Oversized reference payload must be rejected before decoding full rows.')
    monkeypatch.setattr(academic, '_row_reference', unexpected_decoder)
    with pytest.raises(ValueError, match='metadata exceeds'):
        if operation == 'preview':
            library.import_preview(b'[{"title":"New reference"}]', format='csl-json')
        else:
            getattr(library, operation)()
    assert db.total_changes == before
    assert db.execute('SELECT COUNT(*) FROM academic_reference').fetchone()[0] == 2


def test_reference_count_limit_precedes_materialization(library, monkeypatch):
    library.create(title='First')
    library.create(title='Second')
    monkeypatch.setattr(academic, 'MAX_REFERENCES', 1)
    def unexpected_decoder(_row):
        pytest.fail('Reference count must be checked before full row materialization.')
    monkeypatch.setattr(academic, '_row_reference', unexpected_decoder)
    with pytest.raises(ValueError, match='at most'):
        library.duplicates()


def test_reference_payload_preflight_is_scoped_and_accepts_exact_boundary(db, library, monkeypatch):
    active = library.create(title='Keep', notes='Ж' * 10)
    archived = library.create(title='Archived', notes='Ж' * 10000)
    library.archive(archived.id)
    AcademicLibrary(db, 'second').create(title='Foreign', notes='Ж' * 10000)
    raw = db.execute(f'SELECT {academic._SELECT} FROM academic_reference WHERE id=?',
                     (active.id,)).fetchone()
    # SQLite stores numbers as their text representation for the byte preflight.
    size = sum(len(str(value).encode('utf-8')) for value in raw if value is not None)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', size)
    assert library._all() == (active,)
    with pytest.raises(ValueError, match='metadata exceeds'):
        library._all(include_archived=True)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', size - 1)
    with pytest.raises(ValueError, match='metadata exceeds'):
        library._all()


def test_duplicate_preview_budget_rejects_before_suggestion_allocation(db, library, monkeypatch):
    for title in ('Existing one', 'Existing two'):
        library.create(title=title, doi='10.1234/same')
    data = json.dumps([{'title': f'Incoming {number}', 'DOI': '10.1234/SAME'}
                       for number in range(10)]).encode()
    before = db.total_changes
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 11)
    def unexpected_entry(*_args):
        pytest.fail('The duplicate budget must be checked before constructing an entry.')
    monkeypatch.setattr(academic, 'ImportEntry', unexpected_entry)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format='csl-json')
    assert db.total_changes == before
    assert len(library.list()) == 2


def test_duplicate_preview_budget_preserves_overlapping_identifier_matches(library, monkeypatch):
    existing = [library.create(title='Same title', authors=('Author',), year='2020',
                               doi='10.1234/same', isbn='0306406152') for _ in range(2)]
    data = json.dumps([{'type': 'book', 'title': 'Same title', 'author': [{'literal': 'Author'}],
        'issued': {'date-parts': [[2020]]}, 'DOI': '10.1234/SAME', 'ISBN': '0306406152'}] * 2).encode()
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 24)
    preview = library.import_preview(data, format='csl-json')
    assert [entry.duplicate_ids for entry in preview.entries] == [
        tuple(sorted(row.id for row in existing))] * 2
    assert [entry.duplicate_indices for entry in preview.entries] == [(1,), (0,)]
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 23)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format='csl-json')
    assert len(library.list()) == 2


@pytest.mark.parametrize('format', ['ris', 'csl-json'])
def test_duplicate_preview_budget_rejects_fanout_without_writes(library, db, monkeypatch, format):
    original = tuple(library.create(title=f'Existing {number}', doi='10.1234/shared')
                     for number in range(4))
    if format == 'ris':
        data = ''.join(f'TY  - BOOK\nTI  - Incoming {n}\nDO  - 10.1234/shared\nER  -\n'
                       for n in range(3)).encode()
    else:
        data = json.dumps([{'title': f'Incoming {n}', 'DOI': '10.1234/shared'}
                           for n in range(3)]).encode()
    # Even this small valid batch exceeds a deliberately small aggregate
    # allowance: three entries each repeat four existing and two batch IDs.
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 16, raising=False)
    before = db.total_changes
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format=format)
    assert db.total_changes == before
    assert library.list() == original
    assert db.in_transaction


def test_duplicate_preview_preserves_complete_deduplicated_matches(library, db):
    fields = dict(title='Shared title', authors=('Author',), year='2026',
                  doi='10.1234/shared', isbn='0306406152')
    one = library.create(**fields)
    two = library.create(**fields)
    archived = library.create(**fields)
    library.archive(archived.id)
    AcademicLibrary(db, 'second').create(**fields)
    data = json.dumps([{'title': fields['title'], 'author': [{'literal': 'Author'}],
        'issued': {'date-parts': [[2026]]}, 'DOI': fields['doi'], 'ISBN': fields['isbn']}]
        * 3).encode()
    before = db.total_changes
    preview = library.import_preview(data, format='csl-json')
    for index, entry in enumerate(preview.entries):
        assert entry.duplicate_ids == tuple(sorted((one.id, two.id)))
        assert entry.duplicate_indices == tuple(n for n in range(3) if n != index)
    assert db.total_changes == before


def test_maximum_nonduplicate_preview_is_not_truncated(library):
    data = json.dumps([{'title': f'Independent paper {n}', 'DOI': f'10.1234/paper-{n}'}
                       for n in range(2000)]).encode()
    preview = library.import_preview(data, format='csl-json')
    assert len(preview.entries) == 2000
    assert all(not entry.duplicate_ids and not entry.duplicate_indices for entry in preview.entries)
    assert library.list() == ()


def test_duplicate_metadata_preflight_counts_utf8_before_decoding(library, db, monkeypatch):
    library.create(title='Reference', notes='ї' * 1000)
    # Count all stored columns with SQLite's UTF-8 byte lengths. The limit is
    # chosen above the character total, but below the true encoded size.
    character_size, byte_size = db.execute('SELECT '
        + '+'.join(f'coalesce(length({column}),0)' for column in academic._COLUMNS)
        + ',' + '+'.join(f'coalesce(length(CAST({column} AS BLOB)),0)'
                         for column in academic._COLUMNS)
        + " FROM academic_reference WHERE workspace='first'").fetchone()
    assert character_size < byte_size
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_METADATA_BYTES', character_size, raising=False)
    decoded = []
    original = academic._row_reference

    def record_decode(row):
        decoded.append(row)
        return original(row)

    monkeypatch.setattr(academic, '_row_reference', record_decode)
    before = db.total_changes
    with pytest.raises(ValueError, match='metadata.*duplicate'):
        library.import_preview(b'[{"title":"Incoming"}]', format='csl-json')
    assert not decoded, 'Reject oversized metadata before building Python reference objects.'
    assert db.total_changes == before


def test_duplicate_metadata_budget_excludes_other_libraries_and_archived_rows(library, db, monkeypatch):
    active = library.create(title='Active', doi='10.1234/shared')
    archived = library.create(title='Archived', notes='ї' * 1000)
    library.archive(archived.id)
    AcademicLibrary(db, 'second').create(title='Foreign', notes='ї' * 1000)
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_METADATA_BYTES', 1024, raising=False)
    preview = library.import_preview(b'[{"title":"Incoming","DOI":"10.1234/shared"}]',
                                     format='csl-json')
    assert preview.entries[0].duplicate_ids == (active.id,)


@pytest.mark.parametrize('column,value', [('notes', 'Changed without search index'),
    ('authors', '["Author", "Other"]'), ('search_text', 'forged'), ('doi', '10.1234/UPPER')])
def test_duplicate_lookup_keeps_complete_stored_metadata_validation(library, db, column, value):
    row = library.create(title='Existing', doi='10.1234/paper')
    db.execute(f'UPDATE academic_reference SET {column}=? WHERE id=?', (value, row.id))
    before = db.total_changes
    with pytest.raises(ValueError):
        library.import_preview(b'[{"title":"Incoming"}]', format='csl-json')
    assert db.total_changes == before


@pytest.mark.parametrize('operation', ['duplicates', 'preview', 'selected', 'export'])
def test_bulk_reference_byte_budget_rejects_before_decoding(db, library, monkeypatch, operation):
    data = b'[{"title":"New reference"}]'
    preview = library.import_preview(data, format='csl-json')
    library.create(title='Large note one', notes='Ж' * 10000)
    library.create(title='Large note two', notes='Ж' * 10000)
    before = tuple(db.execute('SELECT * FROM academic_reference ORDER BY id'))
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 30000, raising=False)

    def forbidden_decode(row):
        pytest.fail('The bulk metadata budget must be checked before decoding full references.')

    monkeypatch.setattr(academic, '_row_reference', forbidden_decode)
    actions = {
        'duplicates': library.duplicates,
        'preview': lambda: library.import_preview(data, format='csl-json'),
        'selected': lambda: library.import_selected(preview, (0,)),
        'export': library.export,
    }
    with pytest.raises(ValueError, match='metadata exceeds'):
        actions[operation]()
    assert tuple(db.execute('SELECT * FROM academic_reference ORDER BY id')) == before
    assert db.in_transaction


def test_reference_byte_budget_counts_utf8_and_allows_exact_threshold(db, library, monkeypatch):
    row = library.create(title='Українські матеріали', notes='Ж' * 10000)
    stored = db.execute('SELECT * FROM academic_reference WHERE id=?', (row.id,)).fetchone()
    stored_bytes = sum(len(str(value).encode('utf-8')) for value in stored if value is not None)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', stored_bytes, raising=False)
    assert library._all() == (row,)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', stored_bytes - 1)
    with pytest.raises(ValueError, match='metadata exceeds'):
        library._all()


def test_reference_budget_preserves_workspace_archive_and_identity_order(db, library, monkeypatch):
    first = library.create(title='First', doi='10.1234/a')
    second = library.create(title='Second', doi='10.1234/a')
    archived = library.create(title='Archived large note', notes='Ж' * 10000, archived=True)
    AcademicLibrary(db, 'second').create(title='Foreign large note', notes='Ж' * 10000)
    monkeypatch.setattr(academic, 'MAX_REFERENCE_SCAN_BYTES', 4000, raising=False)
    assert library._all() == tuple(sorted((first, second), key=lambda row: row.id))
    group, = library.duplicates()
    assert group.reference_ids == tuple(sorted((first.id, second.id)))
    assert archived.id not in group.reference_ids
    with pytest.raises(ValueError, match='metadata exceeds'):
        library._all(include_archived=True)


def test_reference_count_budget_rejects_before_full_decode(db, library, monkeypatch):
    library.create(title='First')
    library.create(title='Second')
    monkeypatch.setattr(academic, 'MAX_REFERENCES', 1)

    def forbidden_decode(row):
        pytest.fail('The reference-count limit must precede full decoding.')

    monkeypatch.setattr(academic, '_row_reference', forbidden_decode)
    with pytest.raises(ValueError, match='at most'):
        library.duplicates()
    assert db.execute('SELECT count(*) FROM academic_reference').fetchone()[0] == 2


@pytest.mark.parametrize('format', ['ris', 'csl-json'])
def test_duplicate_preview_fanout_is_bounded_without_writes(db, library, monkeypatch, format):
    for index in range(12):
        library.create(title=f'Existing {index}', doi='10.1234/shared')
    if format == 'ris':
        data = b'TY  - BOOK\nTI  - Incoming\nDO  - 10.1234/shared\nER  -\n' * 12
    else:
        data = json.dumps([{'title': 'Incoming', 'DOI': '10.1234/shared'}] * 12).encode()
    before = tuple(db.execute('SELECT * FROM academic_reference ORDER BY id'))
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 100, raising=False)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format=format)
    assert tuple(db.execute('SELECT * FROM academic_reference ORDER BY id')) == before
    assert db.in_transaction


def test_duplicate_budget_preserves_sorted_deduplicated_overlapping_matches(library, monkeypatch):
    first = library.create(title='First', doi='10.1234/shared', isbn='0306406152')
    second = library.create(title='Second', doi='10.1234/shared', isbn='9780306406157')
    data = json.dumps([{'title': 'Incoming', 'DOI': '10.1234/shared',
                        'ISBN': '0306406152'}] * 2).encode()
    # Two keys each visit two existing IDs and two input indices, for each of
    # two input records. The conservative limit counts before deduplication.
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 16, raising=False)
    preview = library.import_preview(data, format='csl-json')
    assert [entry.duplicate_ids for entry in preview.entries] == [tuple(sorted((first.id, second.id)))] * 2
    assert [entry.duplicate_indices for entry in preview.entries] == [(1,), (0,)]
    monkeypatch.setattr(academic, 'MAX_DUPLICATE_PREVIEW_SLOTS', 15)
    with pytest.raises(ValueError, match='Too many duplicate matches'):
        library.import_preview(data, format='csl-json')


def test_full_size_distinct_reference_preview_is_not_truncated(library):
    data = json.dumps([{'title': f'Paper {index}', 'DOI': f'10.1234/{index}'}
                       for index in range(2000)]).encode()
    preview = library.import_preview(data, format='csl-json')
    assert len(preview.entries) == 2000
    assert [entry.index for entry in preview.entries] == list(range(2000))
    assert all(not entry.duplicate_ids and not entry.duplicate_indices for entry in preview.entries)
    assert library.list() == ()
