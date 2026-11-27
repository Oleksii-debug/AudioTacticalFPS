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
