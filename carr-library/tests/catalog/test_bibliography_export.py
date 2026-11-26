"""Bibliography exchange against real canonical SQLite catalogs and snapshots."""
from contextlib import closing, contextmanager
import json
import sqlite3

import pytest

from carr_library.catalog import bibliography_export as export
from carr_library.catalog.membership import Catalog
from carr_library.catalog.ris_preview import preview_ris
from carr_library.storage.revisions import Storage
from carr_library.storage.schema_identity import schema_fingerprint


FORMATS = ('csl-json', 'ris', 'bibtex')
TITLE = 'Історія e\u0301 / é / العربية / 中文'
AUTHORS = ('Research and Evidence Group', 'Коваль, Олена', '研究会', '研究会')
MANIFEST = dict(sha256='a' * 64, mime='text/plain', size=123)


@contextmanager
def snapshot(path, *, foreign_keys=True):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=' + ('ON' if foreign_keys else 'OFF'))
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            yield db
        finally:
            db.rollback()


def create_workspace(db, identity, *, archived=0):
    db.execute('INSERT INTO workspace VALUES(?)', (identity,))
    db.execute('INSERT INTO workspace_meta VALUES(?,?,?,?)',
               (identity, 'Library ' + identity, 'PRIVATE WORKSPACE NOTE', archived))
    return Catalog(db, identity)


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'research.sqlite'
    with closing(Storage(path, schema_version=3)) as storage:
        assert storage.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        with storage.transaction():
            a = create_workspace(storage.db, 'a')
            a.add_work('shared-work', TITLE, AUTHORS)
            a.add_edition('e01', 'shared-work', MANIFEST, language='uk', edition='1', volume='II')
            a.add_edition('e02', 'shared-work', MANIFEST, language='en', edition='2', volume='III')
            a.add_work('another-work', TITLE, ('Different author',))
            a.add_edition('e03', 'another-work', MANIFEST)
            a.add_work('unknown-work')
            a.add_edition('e04', 'unknown-work', MANIFEST)
            for identity in ('selected', 'other', 'all', 'archived', 'empty'):
                a.create_collection(identity, identity)
            for edition in ('e01', 'e02', 'e03', 'e04'):
                a.assign('all', edition)
            a.assign('selected', 'e01')
            a.assign('selected', 'e02')
            a.assign('other', 'e01')
            a.assign('other', 'e03')
            a.assign('archived', 'e02')
            a.archive('archived')
            b = create_workspace(storage.db, 'b')
            b.add_work('shared-work', 'PRIVATE OTHER LIBRARY', ('Other person',))
            b.add_edition('e01', 'shared-work', MANIFEST)
            b.create_collection('selected', 'selected')
            b.create_collection('b-only', 'b-only')
            b.assign('selected', 'e01')
            create_workspace(storage.db, 'empty')
            create_workspace(storage.db, 'archived-workspace', archived=1)
        yield storage, path


def test_csl_preserves_editions_authors_unicode_and_only_known_fields(library):
    storage, path = library
    before = schema_fingerprint(storage.db)
    with snapshot(path) as db:
        text = export.export_bibliography(db, 'a')
        assert db.in_transaction and db.total_changes == 0
    items = json.loads(text)
    assert len(items) == len({row['id'] for row in items}) == 4
    assert all(row['type'] == 'document' for row in items)
    assert [row.get('title') for row in items] == [TITLE, TITLE, TITLE, None]
    assert items[0]['author'] == [{'literal': name} for name in AUTHORS]
    assert items[1]['author'] == items[0]['author']
    assert items[2]['author'] == [{'literal': 'Different author'}]
    assert items[0]['language'] == 'uk' and items[1]['language'] == 'en'
    assert items[0]['edition'] == '1' and items[1]['edition'] == '2'
    assert items[0]['volume'] == 'II' and items[1]['volume'] == 'III'
    assert set(items[3]) == {'id', 'type'}
    allowed = {'id', 'type', 'title', 'author', 'language', 'edition', 'volume'}
    assert all(set(row) <= allowed for row in items)
    assert 'PRIVATE' not in text and 'shared-work' not in text and MANIFEST['sha256'] not in text
    assert schema_fingerprint(storage.db) == before
    storage.check()


@pytest.mark.parametrize('format', FORMATS)
def test_library_and_collection_are_scoped_and_never_duplicate_memberships(library, format):
    _, path = library
    with snapshot(path) as db:
        whole = export.export_bibliography(db, 'a', format=format)
        chosen = export.export_bibliography(db, 'a', collection='selected', format=format)
        other = export.export_bibliography(db, 'b', collection='selected', format=format)
        assert 'PRIVATE OTHER LIBRARY' not in whole + chosen
        assert 'PRIVATE OTHER LIBRARY' in other
        with pytest.raises(ValueError, match='unknown or archived collection'):
            export.export_bibliography(db, 'a', collection='b-only', format=format)
        with pytest.raises(ValueError, match='unknown or archived collection'):
            export.export_bibliography(db, 'a', collection="selected' OR 1=1 --", format=format)
        if format == 'csl-json':
            assert len(json.loads(whole)) == 4
            assert len(json.loads(chosen)) == 2
            assert [item['id'] for item in json.loads(chosen)] == [item['id'] for item in json.loads(whole)[:2]]
            assert json.loads(other)[0]['id'] != json.loads(whole)[0]['id']
        elif format == 'ris':
            assert len(preview_ris(whole.encode('utf-8'))) == 4
            assert len(preview_ris(chosen.encode('utf-8'))) == 2
        else:
            assert whole.count('@misc{') == 4 and chosen.count('@misc{') == 2


@pytest.mark.parametrize('format', FORMATS)
def test_archived_collection_or_workspace_is_rejected_even_when_empty(library, format):
    _, path = library
    with snapshot(path) as db:
        for collection in ('archived', 'missing'):
            with pytest.raises(ValueError, match='unknown or archived collection'):
                export.export_bibliography(db, 'a', collection=collection, format=format)
        with pytest.raises(ValueError, match='missing or archived'):
            export.export_bibliography(db, 'archived-workspace', format=format)
        with pytest.raises(ValueError, match='unknown workspace'):
            export.export_bibliography(db, 'missing', format=format)


@pytest.mark.parametrize('format', FORMATS)
def test_empty_library_and_collection_are_valid_exports(library, format):
    _, path = library
    with snapshot(path) as db:
        for workspace, collection in (('a', 'empty'), ('empty', None)):
            result = export.export_bibliography(db, workspace, collection=collection, format=format)
            if format == 'csl-json':
                assert json.loads(result) == []
            elif format == 'ris':
                assert result == ''
            else:
                assert all(line.startswith('%') or not line for line in result.splitlines())


def test_ris_roundtrip_uses_actual_parser_and_keeps_separate_authors(library):
    _, path = library
    with snapshot(path) as db:
        text = export.export_bibliography(db, 'a', collection='selected', format='ris')
    assert text.startswith('TY  - GEN\n') and '\n1.\n' not in text
    first, second = preview_ris(text.encode('utf-8'))
    assert first.reference_type == second.reference_type == 'GEN'
    assert first.titles == second.titles == (TITLE,)
    assert first.authors == second.authors == AUTHORS
    assert first.languages == ('uk',) and second.languages == ('en',)
    assert first.editions == ('1',) and second.editions == ('2',)
    assert first.volumes == ('II',) and second.volumes == ('III',)
    assert not first.metadata_verified and not second.metadata_verified
    identifiers = [line.removeprefix('ID  - ') for line in text.splitlines() if line.startswith('ID  - ')]
    with snapshot(path) as db:
        csl = json.loads(export.export_bibliography(db, 'a', collection='selected'))
    assert identifiers == [row['id'] for row in csl]


def test_bibtex_escapes_syntax_and_preserves_literal_author_boundaries(library):
    storage, path = library
    title = 'A {title} \\input{secret} $ & # % _ ^ ~\n@evil{attack}'
    with storage.transaction():
        storage.db.execute('UPDATE catalog_work SET title=? WHERE workspace=? AND id=?',
                           (title, 'a', 'shared-work'))
        storage.db.execute('DELETE FROM catalog_author WHERE workspace=? AND work=?', ('a', 'shared-work'))
        storage.db.executemany('INSERT INTO catalog_author VALUES(?,?,?,?)',
            [('a', 'shared-work', 0, 'Alpha and Omega'),
             ('a', 'shared-work', 1, 'Doe, Jane'),
             ('a', 'shared-work', 2, 'Кафедра & наука')])
    with snapshot(path) as db:
        text = export.export_bibliography(db, 'a', collection='selected', format='bibtex')
    assert sum(line.startswith('@misc{') for line in text.splitlines()) == 2
    assert not any(line.startswith('@evil') for line in text.splitlines())
    assert '\\input{' not in text
    assert r'{\textbraceleft}title{\textbraceright}' in text
    assert r'{\textbackslash}input{\textbraceleft}secret{\textbraceright}' in text
    assert r'\$ \& \# \% \_ {\textasciicircum} {\textasciitilde}' in text
    assert '  author = {{Alpha and Omega} and {Doe, Jane} and {Кафедра \\& наука}},' in text
    assert '  language = {uk},' in text
    assert '  language = {en},' in text


@pytest.mark.parametrize('bad', ['Injected\nER  -\nTY  - BOOK', 'A\tB', 'A\x01B', 'A\u2028B'])
def test_ris_rejects_tag_injection_and_csl_preserves_the_original(library, bad):
    storage, path = library
    with storage.transaction():
        storage.db.execute('UPDATE catalog_work SET title=? WHERE workspace=? AND id=?',
                           (bad, 'a', 'shared-work'))
    with snapshot(path) as db:
        with pytest.raises(ValueError, match='single-line metadata'):
            export.export_bibliography(db, 'a', collection='selected', format='ris')
        assert db.in_transaction
        assert json.loads(export.export_bibliography(db, 'a', collection='selected'))[0]['title'] == bad


def test_bibtex_rejects_unrepresentable_control_character(library):
    storage, path = library
    with storage.transaction():
        storage.db.execute('UPDATE catalog_author SET name=? WHERE workspace=? AND work=? AND position=0',
                           ('Name\x01hidden', 'a', 'shared-work'))
    with snapshot(path) as db:
        with pytest.raises(ValueError, match='unsafe control character'):
            export.export_bibliography(db, 'a', format='bibtex')
        assert db.in_transaction


def test_export_never_reads_source_evidence_or_asset_metadata_tables(library):
    _, path = library
    forbidden = {'page', 'revision', 'source_metadata', 'research_evidence', 'catalog_asset'}
    with snapshot(path) as db:
        reads = []
        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ:
                reads.append(table)
                if table in forbidden:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        for format in FORMATS:
            assert export.export_bibliography(db, 'a', format=format)
        assert forbidden.isdisjoint(reads)


def test_cursor_pagination_stays_in_one_snapshot_while_catalog_changes(library, monkeypatch):
    storage, path = library
    monkeypatch.setattr(export, 'PAGE_SIZE', 2)
    reads = []
    changed = False
    def change_between_pages():
        nonlocal changed
        if reads and not changed:
            changed = True
            with storage.transaction():
                catalog = Catalog(storage.db, 'a')
                storage.db.execute('UPDATE catalog_work SET title=? WHERE workspace=? AND id=?',
                                   ('CHANGED AFTER SNAPSHOT', 'a', 'another-work'))
                catalog.add_edition('e05', 'shared-work', MANIFEST)
                catalog.assign('all', 'e05')
                catalog.archive('all')
        return False
    with snapshot(path) as db:
        db.set_trace_callback(lambda sql: reads.append(sql) if sql.startswith('WITH selected AS') else None)
        records = json.loads(export.export_bibliography(db, 'a', collection='all',
                                                       cancelled=change_between_pages))
        assert db.in_transaction
    assert changed and len(reads) >= 2
    assert len(records) == 4 and records[2]['title'] == TITLE
    with snapshot(path) as db:
        with pytest.raises(ValueError, match='unknown or archived collection'):
            export.export_bibliography(db, 'a', collection='all')
        current = json.loads(export.export_bibliography(db, 'a'))
        assert len(current) == 5 and current[2]['title'] == 'CHANGED AFTER SNAPSHOT'


@pytest.mark.parametrize('format', FORMATS)
def test_cancellation_before_or_during_export_leaves_snapshot_owned_by_caller(library, format):
    _, path = library
    with snapshot(path) as db:
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(InterruptedError, match='cancelled'):
            export.export_bibliography(db, 'a', format=format, cancelled=lambda: True)
        assert not statements and db.in_transaction
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 6
        with pytest.raises(InterruptedError, match='cancelled'):
            export.export_bibliography(db, 'a', format=format, cancelled=cancelled)
        assert db.in_transaction and db.total_changes == 0
        assert export.export_bibliography(db, 'a', format=format)


@pytest.mark.parametrize('format', FORMATS)
def test_output_limit_counts_utf8_bytes_and_never_returns_truncated_content(library, monkeypatch, format):
    _, path = library
    with snapshot(path) as db:
        complete = export.export_bibliography(db, 'a', format=format)
        size = len(complete.encode('utf-8'))
        assert size > len(complete)
        monkeypatch.setattr(export, 'MAX_OUTPUT_BYTES', size)
        assert export.export_bibliography(db, 'a', format=format) == complete
        monkeypatch.setattr(export, 'MAX_OUTPUT_BYTES', size - 1)
        with pytest.raises(ValueError, match='output limit'):
            export.export_bibliography(db, 'a', format=format)
        assert db.in_transaction and db.total_changes == 0


def test_actual_ten_thousand_reference_boundary(library):
    storage, path = library
    with storage.transaction():
        catalog = create_workspace(storage.db, 'many')
        catalog.add_work('work', 'Same title', ('One author',))
        catalog.add_edition('00000', 'work', MANIFEST)
        storage.db.executemany('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)',
            [('many', f'{index:05}', 'work', MANIFEST['sha256'], None, None, None)
             for index in range(1, 10000)])
    with snapshot(path) as db:
        records = json.loads(export.export_bibliography(db, 'many'))
        assert len(records) == len({row['id'] for row in records}) == 10000
    with storage.transaction():
        Catalog(storage.db, 'many').add_edition('10000', 'work', MANIFEST)
    with snapshot(path) as db:
        with pytest.raises(ValueError, match='10,000 references'):
            export.export_bibliography(db, 'many')
        assert db.in_transaction


def test_snapshot_and_input_prerequisites(library):
    _, path = library
    with snapshot(path) as db:
        for bad in ('csv', '', None, ['ris']):
            with pytest.raises(ValueError, match='Unsupported bibliography format'):
                export.export_bibliography(db, 'a', format=bad)
        with pytest.raises(ValueError, match='cancellation callback'):
            export.export_bibliography(db, 'a', cancelled=None)
        db.execute('PRAGMA read_uncommitted=ON')
        with pytest.raises(ValueError, match='consistent caller-owned'):
            export.export_bibliography(db, 'a')
        db.execute('PRAGMA read_uncommitted=OFF')
        db.rollback()
        with pytest.raises(ValueError, match='consistent caller-owned'):
            export.export_bibliography(db, 'a')
    with snapshot(path, foreign_keys=False) as db:
        with pytest.raises(ValueError, match='foreign keys required'):
            export.export_bibliography(db, 'a')
