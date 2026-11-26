"""Source bibliography integration against actual V3 catalog/storage tables."""
import csv
import io
import json
import re
import sqlite3

import pytest

from carr_library.catalog import bibliography
from carr_library.catalog.bibliography import export_bibliography, export_inventory
from carr_library.catalog.membership import Catalog
from carr_library.catalog.ris_preview import preview_ris
from carr_library.catalog._rispy_parser import loads as parse_ris
from carr_library.storage.revisions import Page, Storage


TITLE = 'Дослідження — 東京, 100% "текст"'
AUTHORS = ('Олена Коваль', 'Consortium and Partners; 東京', 'Müller, J.')
MANIFEST = dict(sha256='a' * 64, mime='text/plain', size=8)
FORMATS = ('csl-json', 'ris', 'bibtex', 'csv')


def export(db, workspace='a', *, format='csl-json', **kwargs):
    if format == 'csv':
        return export_inventory(db, workspace, **kwargs)
    return export_bibliography(db, workspace, format=format, **kwargs)


def refresh(db):
    db.rollback()
    db.execute('BEGIN')


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'library.db'
    writer = Storage(path, schema_version=3)
    writer.db.execute('PRAGMA journal_mode=WAL')
    try:
        with writer.transaction():
            writer.db.executemany('INSERT INTO workspace VALUES(?)',
                                 [('a',), ('b',), ('empty',)])
            a, b = Catalog(writer.db, 'a'), Catalog(writer.db, 'b')
            a.add_work('w', TITLE, AUTHORS)
            a.add_edition('e1', 'w', MANIFEST, language='uk', edition='1', volume='iv')
            a.add_edition('e2', 'w', MANIFEST, language='en', edition='2', volume='v')
            a.add_work('same-title', TITLE, ('Other author',))
            a.add_edition('e3', 'same-title', MANIFEST)
            a.add_work('unknown')
            a.add_edition('e4', 'unknown', MANIFEST)
            a.create_collection('c', 'Selected')
            a.create_collection('second', 'Second')
            a.create_collection('old', 'Old')
            a.assign('c', 'e1')
            a.assign('c', 'e3')
            a.assign('second', 'e1')
            a.assign('old', 'e2')
            a.archive('old')
            b.add_work('w', 'PRIVATE OTHER WORKSPACE', ('Private author',))
            b.add_edition('e1', 'w', MANIFEST, language='zz', edition='Secret')
            b.create_collection('c', 'Private same collection ID')
            b.assign('c', 'e1')
            b.create_collection('b-only', 'Private collection')
            writer.stage_revision('a', 'e1', 'raw-revision', 'a' * 64, 'synthetic',
                                  [Page('PRIVATE ORIGINAL SOURCE TEXT', 1)])
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield writer, db
        finally:
            db.close()
    finally:
        writer.close()


def test_csl_metadata_types_literal_authors_and_duplicate_titles(library):
    _, db = library
    rows = json.loads(export_bibliography(db, 'a'))
    assert len(rows) == 4
    assert rows[0] == {
        'id': rows[0]['id'], 'type': 'document', 'title': TITLE,
        'language': 'uk', 'edition': '1', 'volume': 'iv',
        'author': [{'literal': name} for name in AUTHORS],
    }
    assert rows[1]['edition'] == '2' and rows[1]['language'] == 'en'
    assert rows[2]['title'] == rows[0]['title']
    assert rows[2]['author'] == [{'literal': 'Other author'}]
    assert rows[3] == {'id': rows[3]['id'], 'type': 'document'}
    assert len({row['id'] for row in rows}) == 4
    assert all(re.fullmatch(r'source_[0-9a-f]{64}', row['id']) for row in rows)
    assert not any({'issued', 'publisher', 'DOI', 'ISBN', 'page'} & row.keys() for row in rows)


def test_ids_are_workspace_scoped_and_independent_of_collection_or_metadata(library):
    writer, db = library
    original = json.loads(export_bibliography(db, 'a'))
    selected = json.loads(export_bibliography(db, 'a', collection='c'))
    other = json.loads(export_bibliography(db, 'b', collection='c'))
    assert [row['id'] for row in selected] == [original[0]['id'], original[2]['id']]
    assert other[0]['id'] not in {row['id'] for row in original}
    assert other[0]['title'] == 'PRIVATE OTHER WORKSPACE'
    with writer.transaction():
        writer.db.execute("UPDATE catalog_work SET title=? WHERE workspace='a' AND id='w'",
                          ('Corrected current title',))
    # One caller snapshot cannot mix old and newly corrected catalog metadata.
    assert json.loads(export_bibliography(db, 'a')) == original
    refresh(db)
    updated = json.loads(export_bibliography(db, 'a'))
    assert updated[0]['title'] == updated[1]['title'] == 'Corrected current title'
    assert [row['id'] for row in updated] == [row['id'] for row in original]


@pytest.mark.parametrize('format', FORMATS)
def test_deterministic_read_only_scope_and_metadata_only(library, format):
    writer, db = library
    changes = writer.db.total_changes
    read_tables = set()
    allowed = {'workspace', 'catalog_work', 'catalog_author', 'catalog_edition',
               'catalog_collection', 'catalog_membership'}

    def authorizer(action, first, second, database, trigger):
        if action == sqlite3.SQLITE_READ:
            read_tables.add(first)
            if first not in allowed:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorizer)
    result = export(db, format=format)
    assert export(db, format=format) == result
    assert 'PRIVATE OTHER WORKSPACE' not in result
    assert 'Private author' not in result
    assert 'PRIVATE ORIGINAL SOURCE TEXT' not in result
    assert db.in_transaction and db.total_changes == 0
    assert writer.db.total_changes == changes
    assert read_tables <= allowed


def test_ris_roundtrip_uses_existing_strict_preview_and_ordered_literal_authors(library):
    _, db = library
    result = export_bibliography(db, 'a', format='ris')
    hints = preview_ris(result.encode('utf-8'))
    assert len(hints) == 4
    assert hints[0].reference_type == 'GEN'
    assert hints[0].titles == (TITLE,)
    assert hints[0].authors == AUTHORS
    assert hints[0].languages == ('uk',)
    assert hints[0].editions == ('1',) and hints[0].volumes == ('iv',)
    parsed = parse_ris(result)
    csl = json.loads(export_bibliography(db, 'a'))
    assert [row['id'] for row in parsed] == [row['id'] for row in csl]
    assert all(row['type_of_reference'] == 'GEN' for row in parsed)
    assert all(not ({'year', 'publication_year', 'doi', 'publisher', 'start_page'} & row.keys())
               for row in parsed)


def test_bibtex_literal_names_escaping_and_no_metadata_as_syntax(library):
    writer, db = library
    attack = '}\n@book{injected,\ntitle={Danger}} \\input{secret}% & # $ _ ^ ~'
    with writer.transaction():
        writer.db.execute("UPDATE catalog_work SET title=? WHERE workspace='a' AND id='w'",
                          (attack,))
    result = export_bibliography(db, 'a', format='bibtex')
    assert len(re.findall(r'^@misc\{source_[0-9a-f]{64},$', result, re.MULTILINE)) == 4
    assert not re.search(r'^@book', result, re.MULTILINE)
    assert result.count('{') == result.count('}')
    assert r'{\textbackslash}input{\textbraceleft}secret{\textbraceright}' in result
    assert r'\% \& \# \$ \_ {\textasciicircum} {\textasciitilde}' in result
    assert 'author = {{Олена Коваль} and {Consortium and Partners; 東京} and {Müller, J.}},' in result
    assert 'Metadata line breaks and tabs are represented as spaces.' in result
    assert not any(f'  {name} =' in result for name in ('year', 'publisher', 'doi', 'pages'))


def test_inventory_formula_protection_and_lossless_unicode_nulls(library):
    writer, db = library
    values = ('=SUM(1,2)', '+cmd', '-1+1', '@IMPORTXML("x")', '\t=1',
              '\r\n=2', '  =3', '名字; and Другий\u202e', '"quoted",\ntext')
    with writer.transaction():
        catalog = Catalog(writer.db, 'a')
        for i, value in enumerate(values):
            catalog.add_work(f'formula-w{i}', value, (value, 'Literal; and Co'))
            catalog.add_edition(f'formula-e{i}', f'formula-w{i}', MANIFEST,
                                language=value, edition=value, volume=value)
    output = export_inventory(db, 'a')
    rows = list(csv.DictReader(io.StringIO(output)))
    assert len(rows) == 4 + len(values)
    for row in rows:
        assert row['text_encoding'] == 'json-string-v1'
        for header, cell in row.items():
            if header.endswith('_json'):
                assert cell[0] in ('"', '[', 'n')
                json.loads(cell)
    by_id = {json.loads(row['edition_id_json']): row for row in rows}
    for i, value in enumerate(values):
        row = by_id[f'formula-e{i}']
        assert json.loads(row['title_json']) == value
        assert json.loads(row['authors_json']) == [value, 'Literal; and Co']
        assert json.loads(row['language_json']) == value
        assert json.loads(row['edition_json']) == value
        assert json.loads(row['volume_json']) == value
    assert json.loads(by_id['e4']['title_json']) is None
    assert json.loads(by_id['e4']['authors_json']) == []
    assert json.loads(by_id['e1']['asset_sha256_json']) == 'a' * 64
    assert json.loads(by_id['e1']['work_id_json']) == 'w'
    assert len(output.splitlines()) == 1 + len(rows)


@pytest.mark.parametrize('format', FORMATS)
def test_full_pagination_beyond_500_editions_without_deduplication(library, format):
    writer, db = library
    with writer.transaction():
        catalog = Catalog(writer.db, 'a')
        for i in range(503):
            catalog.add_edition(f'bulk-{i:04}', 'w', MANIFEST, edition=str(i))
    result = export(db, format=format)
    if format == 'csl-json':
        rows = json.loads(result)
        assert len(rows) == len({row['id'] for row in rows}) == 507
        assert rows[502]['edition'] == '502'
    elif format == 'ris':
        assert len(preview_ris(result.encode('utf-8'))) == 507
    elif format == 'bibtex':
        assert len(re.findall(r'^@misc\{', result, re.MULTILINE)) == 507
    else:
        rows = list(csv.DictReader(io.StringIO(result)))
        assert len(rows) == 507
        assert json.loads(rows[502]['edition_id_json']) == 'bulk-0502'


@pytest.mark.parametrize('format', FORMATS)
def test_collection_scope_archives_and_empty_library(library, format):
    _, db = library
    selected = export(db, collection='c', format=format)
    assert 'Private' not in selected
    for collection in ('old', 'b-only', 'missing', "' OR 1=1 --"):
        with pytest.raises(ValueError, match='unknown or archived collection'):
            export(db, collection=collection, format=format)
    empty = export(db, 'empty', format=format)
    if format == 'csl-json':
        assert json.loads(selected) and json.loads(empty) == []
    elif format == 'ris':
        assert len(preview_ris(selected.encode('utf-8'))) == 2
        assert empty == ''
    elif format == 'bibtex':
        assert selected.count('@misc{') == 2 and '@misc{' not in empty
    else:
        assert len(list(csv.DictReader(io.StringIO(selected)))) == 2
        assert list(csv.DictReader(io.StringIO(empty))) == []


@pytest.mark.parametrize('format', FORMATS)
def test_record_and_utf8_byte_limits_fail_instead_of_truncating(library, monkeypatch, format):
    _, db = library
    expected = export(db, format=format)
    monkeypatch.setattr(bibliography, 'MAX_RECORDS', 4)
    assert export(db, format=format) == expected
    monkeypatch.setattr(bibliography, 'MAX_RECORDS', 3)
    with pytest.raises(ValueError, match='Too many catalog sources'):
        export(db, format=format)
    monkeypatch.setattr(bibliography, 'MAX_RECORDS', 4)
    size = len(expected.encode('utf-8'))
    assert size > len(expected)
    monkeypatch.setattr(bibliography, 'MAX_EXPORT_BYTES', size)
    assert export(db, format=format) == expected
    monkeypatch.setattr(bibliography, 'MAX_EXPORT_BYTES', size - 1)
    with pytest.raises(ValueError, match='export is too large'):
        export(db, format=format)
    assert db.in_transaction and db.total_changes == 0


@pytest.mark.parametrize('format', FORMATS)
def test_cancellation_before_query_and_after_processing_has_no_partial_return(library, format):
    _, db = library
    queries = []
    db.set_trace_callback(queries.append)
    with pytest.raises(TimeoutError, match='cancelled'):
        export(db, format=format, cancelled=lambda: True)
    assert queries == []
    polls = 0

    def cancel_during_export():
        nonlocal polls
        polls += 1
        return polls >= 6

    with pytest.raises(TimeoutError, match='cancelled'):
        export(db, format=format, cancelled=cancel_during_export)
    assert polls == 6 and queries
    assert db.in_transaction and db.total_changes == 0
    assert export(db, format=format)


@pytest.mark.parametrize('value', (True, 1, b'a', None, '', '\ud800', '\x00'))
def test_invalid_workspace_types_or_encoding(library, value):
    _, db = library
    for function in (export_bibliography, export_inventory):
        with pytest.raises(ValueError):
            function(db, value)


def test_invalid_format_connection_collection_and_callback(library):
    _, db = library
    for value in (True, 1, None, [], b'ris', 'RIS', 'csv', 'unknown'):
        with pytest.raises(ValueError, match='Unsupported'):
            export_bibliography(db, 'a', format=value)
    for function in (export_bibliography, export_inventory):
        with pytest.raises(ValueError, match='SQLite connection'):
            function(None, 'a')
        for value in (True, 1, [], '', b'c', '\ud800'):
            with pytest.raises(ValueError):
                function(db, 'a', collection=value)
        with pytest.raises(ValueError, match='callback'):
            function(db, 'a', cancelled=False)
        with pytest.raises(ValueError, match='unknown workspace'):
            function(db, 'missing')


def test_transaction_and_foreign_keys_are_required_without_altering_caller(library):
    _, db = library
    db.rollback()
    for function in (export_bibliography, export_inventory):
        with pytest.raises(ValueError, match='caller snapshot'):
            function(db, 'a')
        assert not db.in_transaction
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN')
    for function in (export_bibliography, export_inventory):
        with pytest.raises(ValueError, match='foreign keys'):
            function(db, 'a')
        assert db.in_transaction


@pytest.mark.parametrize('unsafe', ('\nER  -\nTY  - BOOK', '\r', '\t', '\u2028', '\u202e'))
def test_ris_rejects_multiline_or_control_metadata_but_csl_preserves_it(library, unsafe):
    writer, db = library
    title = 'Literal' + unsafe + 'title'
    with writer.transaction():
        writer.db.execute("UPDATE catalog_work SET title=? WHERE workspace='a' AND id='w'",
                          (title,))
    with pytest.raises(ValueError, match='Unsafe multiline/control RIS'):
        export_bibliography(db, 'a', format='ris')
    assert json.loads(export_bibliography(db, 'a'))[0]['title'] == title
    rows = list(csv.DictReader(io.StringIO(export_inventory(db, 'a'))))
    assert json.loads(rows[0]['title_json']) == title
    assert db.in_transaction and db.total_changes == 0
