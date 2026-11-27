"""Actual upstream parser, Unicode, hostile input, and durable metadata tests."""
from hashlib import sha256
import logging
import sqlite3

import pytest

from carr_library.research import bibtex_import as bib
from carr_library.research.academic_library import AcademicLibrary, _DEFAULTS, _validate
from carr_library.storage.academic_schema import ACADEMIC_SCHEMA
from carr_library.storage.revisions import SCHEMA_V3


def one(text):
    rows = bib.parse_bibtex(text)
    assert len(rows) == 1
    values, warnings = rows[0]
    _validate({**_DEFAULTS, **values})
    return values, warnings


def test_unicode_accents_braces_and_explicit_person_suffix():
    values, warnings = one(r'''@BOOK{Café2026,
        title = {Українська: {NASA}, M{\"u}ller and \v{S}t\'{u}dia},
        author = {Garc\'{i}a, Mar\'{i}a and Smith, Jr., John and {Research and Development, Inc.}},
        year = 2026, publisher = {Local Press}, isbn = {978-0-306-40615-7}
    }''')
    assert values['title'] == 'Українська: NASA, Müller and Štúdia'
    assert values['authors'] == ('García, María', 'Smith, John, Jr.', '{Research and Development, Inc.}')
    assert values['isbn'] == '978-0-306-40615-7'
    assert 'Citation key: Café2026' in values['notes']
    assert 'M{\\"u}ller' in values['notes']
    assert any('corporate' in warning for warning in warnings)


def test_strings_concatenation_forward_reference_and_numeric_components():
    values, warnings = one('''@string{publication = prefix # "Journal"}
        @string{prefix = {A Local }}
        @article{caseKey, title = {Research } # 2026,
            journal = PUBLICATION, year=2026, month=nov, pages={12--23}}
    ''')
    assert values['title'] == 'Research 2026'
    assert values['container_title'] == 'A Local Journal'
    assert values['pages'] == '12–23'
    assert 'Used string definitions (inert text):' in values['notes']
    assert '@string{prefix = {A Local }}' in values['notes']
    assert any('month' in warning for warning in warnings)


def test_literal_macro_name_is_not_interpolated():
    values, _ = one('@string{term={Expanded}}\n@book{x,title={term},year=2026}')
    assert values['title'] == 'term'
    assert 'Used string definitions' not in values['notes']


def test_string_optional_trailing_comma_is_supported():
    values, _ = one('@string{j="Journal",}\n@book{x,title=j}')
    assert values['title'] == 'Journal'


def test_protected_punctuation_inside_person_name_is_not_reinterpreted_as_suffix():
    values, warnings = one('@book{x,title={Names},author={Doe, {John, A.}}}')
    assert values['authors'] == ('{Doe, John, A.}',)
    assert any('Protected punctuation' in warning for warning in warnings)


@pytest.mark.parametrize(('name', 'expected'), [
    (', Jr., John', '{, Jr., John}'),
    (', Jane', '{, Jane}'),
    ('{}, Jr., John', '{, Jr., John}'),
    ('Doe, Jr., {}', '{Doe, Jr.,}'),
    ('Doe, {}', '{Doe,}'),
])
def test_incomplete_comma_names_preserve_identity_instead_of_shifting_fields(name, expected, tmp_path):
    import json
    from carr_library.application import LibraryApp
    from carr_library.ui.academic_library_dialog import AcademicController

    original = '@book{x,title={Test},author={' + name + '}}'
    values, warnings = one(original)
    assert values['authors'] == (expected,)
    assert original in values['notes']
    assert any('Incomplete comma-separated person name' in warning for warning in warnings)
    # Reuse the canonical library's literal-name contract through actual
    # review, import, reopen and CSL export. No extra formatting engine or
    # personal-name inference is introduced by the BibTeX adapter.
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('BibTeX name review')
    controller = AcademicController(app, workspace)
    preview = controller.preview(original.encode('utf-8'), 'bibtex')
    assert controller.list() == ()
    imported = controller.import_selected(preview, (0,))
    reopened = AcademicController(LibraryApp(app.root), workspace)
    row = reopened.get(imported.imported_ids[0])
    assert row.authors == (expected[1:-1],)
    assert original in row.notes
    records = json.loads(reopened.export(identity=row.id, format='csl-json'))
    assert records[0]['author'] == [{'literal': expected[1:-1]}]
    from carr_library.citations.bibliography_workbench import records_for_style
    # The explicit personal-name mode must continue treating the preserved
    # incomplete name as literal, rather than assigning John/Jr. new roles.
    snapshot = json.dumps([{'id': 'x', 'type': 'book', 'title': 'Test',
                           'author': [{'literal': expected}]}])
    records, _ = records_for_style(snapshot, name_mode='family-given')
    assert records[0]['author'] == [{'literal': expected[1:-1]}]


@pytest.mark.parametrize('name', ['Doe,,John', 'Doe,{},John'])
def test_empty_optional_suffix_does_not_leave_a_dangling_name_component(name):
    values, warnings = one('@book{x,title={Test},author={' + name + '}}')
    assert values['authors'] == ('Doe, John',)
    assert not any('Incomplete comma-separated' in warning for warning in warnings)


def test_complete_comma_and_corporate_names_keep_distinct_semantics():
    values, warnings = one('''@book{x,title={Test},
        author={Doe, Jane and Doe, Jr., John and {Research, Inc.} and {Research, Inc.}, Alice}}''')
    assert values['authors'] == ('Doe, Jane', 'Doe, John, Jr.', '{Research, Inc.}',
                                '{Research, Inc., Alice}')
    assert not any('Incomplete comma-separated' in warning for warning in warnings)


@pytest.mark.parametrize(('author', 'expected'), [
    (r'\{Research Group\}', '{Research Group}'),
    (r'{\{Research Group\}}', '{Research Group}'),
    ('{Research Group}', 'Research Group'),
    (r'Doe, \{Jane\}', 'Doe, {Jane}'),
    ('{Research, Inc.}', 'Research, Inc.'),
    (r'{\{Research, Inc.\}}', '{Research, Inc.}'),
    ('Doe, {John, A.}', 'Doe, John, A.'),
    (', Jr., John', ', Jr., John'),
])
def test_canonical_literal_import_preserves_real_braces_without_guessing_markers(tmp_path, author, expected):
    import json
    from carr_library.application import LibraryApp
    from carr_library.ui.academic_library_dialog import AcademicController

    source = '@book{x,title={Literal name},author={' + author + '}}'
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Literal names')
    controller = AcademicController(app, workspace)
    preview = controller.preview(source.encode('utf-8'), 'bibtex')
    assert preview.entries[0].authors == (expected,)
    imported = controller.import_selected(preview, (0,))
    reopened = AcademicController(LibraryApp(app.root), workspace)
    row = reopened.get(imported.imported_ids[0])
    assert row.authors == (expected,)
    assert source in row.notes
    assert json.loads(reopened.export(identity=row.id, format='csl-json'))[0]['author'] == [
        {'literal': expected}]


@pytest.mark.parametrize('name', ['Doe,', 'Doe, Jr.,', ',,'])
def test_structurally_invalid_trailing_comma_names_fail_without_reassignment(name):
    with pytest.raises(ValueError):
        bib.parse_bibtex('@book{x,title={Test},author={' + name + '}}')


def test_parenthesized_blocks_quoted_braces_and_literal_hash():
    values, _ = one('''@book(key, title="A {NASA} # paper", year=2026,
        author={Іваненко, Олена and Šimko, Ján})''')
    assert values['title'] == 'A NASA # paper'
    assert values['authors'] == ('Іваненко, Олена', 'Šimko, Ján')


def test_latex_formatting_and_plain_percent_are_not_lost():
    values, _ = one(r'''@book{tex, title={\LaTeX{} and \textit{100\%} complete},
        abstract={100% useful}, url={\url{https://example.org/a\_b}}}''')
    assert values['title'] == 'LaTeX and 100% complete'
    assert values['abstract'] == '100% useful'
    assert values['url'] == 'https://example.org/a_b'


def test_unknown_and_active_tex_are_inert_and_visible(monkeypatch):
    bib._upstream()
    calls = []

    def denied(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Unexpected file/network/process access')

    import builtins
    import subprocess
    import urllib.request
    monkeypatch.setattr(builtins, 'open', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setattr(urllib.request, 'urlopen', denied)
    values, warnings = one(r'''@preamble{"\input{/private/not-opened}"}
        @book{hostile,title={A \unknown{word} and \input{/private/data}},
        file={private.pdf}, note={\write18{echo prohibited}}}''')
    assert values['title'] == r'A \unknown{word} and \input{/private/data}'
    assert r'\write18{echo prohibited}' in values['notes']
    assert any('preambles' in warning for warning in warnings)
    assert any('Attachment paths' in warning for warning in warnings)
    assert any('Unexpanded TeX' in warning for warning in warnings)
    assert calls == []


def test_original_key_cannot_replace_application_identity_or_become_tag():
    values, warnings = one('@book{source_existing_reference,title={Unlinked metadata}, xprivate={Keep this}}')
    assert 'id' not in values and 'edition_id' not in values
    assert values['tags'] == ()
    assert 'Citation key: source_existing_reference' in values['notes']
    assert 'xprivate={Keep this}' in values['notes']
    assert any('xprivate' in warning for warning in warnings)
    assert any('Exports omitting notes' in warning for warning in warnings)


def test_crossref_inherits_explicit_container_but_not_child_identity_or_pages():
    rows = bib.parse_bibtex('''@inproceedings{child, title={A chapter}, author={Child, Alice}, crossref={parent}, pages={4--9}}
        @proceedings{parent,title={Collected work},author={Parent, Peter},year=2026,
        publisher={Example},pages={1--300},doi={10.1234/parent},isbn={0306406152}}''')
    child, warnings = rows[0]
    assert child['title'] == 'A chapter'
    assert child['container_title'] == 'Collected work'
    assert child['authors'] == ('Child, Alice',)
    assert child['year'] == '2026'
    assert child['publisher'] == 'Example'
    assert child['pages'] == '4–9'
    assert child['doi'] == child['isbn'] == ''
    assert 'Referenced entry (inert text):' in child['notes']
    assert '@proceedings{parent' in child['notes']
    assert any('Local cross-reference parent' in warning for warning in warnings)
    _validate({**_DEFAULTS, **child})


def test_child_selection_preserves_parent_macro_provenance():
    child, _ = bib.parse_bibtex('''@string{publication={Collected research}}
        @incollection{child,title={Child},crossref={parent}}
        @book{parent,title=publication,year=2025}''')[0]
    assert child['container_title'] == 'Collected research'
    assert '@string{publication={Collected research}}' in child['notes']


def test_missing_author_and_pages_are_not_inferred_from_parent():
    child, _ = bib.parse_bibtex('''@inbook{child,title={A section},crossref={parent}}
        @book{parent,title={Collection},author={Parent, Author},pages={1--500}}''')[0]
    assert child['authors'] == ()
    assert child['pages'] == ''


@pytest.mark.parametrize('text', [
    '@book{a,title={First}}\n@book{a,title={Second}}',
    '@book{A,title={First}}\n@book{a,title={Second}}',
    '@book{a,title={First},title={Second}}',
    '@book{a,title={First},TiTle={Second}}',
    '@string{x={First}}\n@string{X={Second}}\n@book{a,title=x}',
    '@string{x=y}\n@string{y=x}\n@book{a,title={A}}',
    '@book{a,title=not_defined}',
    '@book{a,title={Unfinished}',
    '@book{a,title={Complete}}\n@book{broken,title=',
    '@book{a,title={Complete}}\n@book missing braces',
    '@book{a,title={A} #}',
    '@book{a,title=# {A}}',
    '@book{a,title={A} {B}}',
    '@book{a,title=unquoted text}',
    '@book{a,title={A},year=2026,year}',
    '@book{a,title={A}, crossref={missing}}',
    '@book{a,title={A}, crossref={b}}\n@book{b,title={B},crossref={a}}',
    '@book{a,title={A},crossref={https://example.org/remote}}',
    '@book{a,title={A},year=2025,date={2026-01-01}}',
    '@book{a,title={A},number=3,issue=4}',
    '@book{a,title={A}, author={Doe, Jr, John, Extra}}',
    '@book{a,title={A}, author={Doe, John and }}',
    '@book{a,title={A}, author={and Doe, John}}',
    '@book{a,title={A}, author={Doe, John and and Smith, Jane}}',
    '@book{a,title={A}, author={}} @book{empty}',
    '@book{a,title={A}}\n@commentary{lost,title={Lost entry}}',
    '@book{a,title={A}}\n@preambleExtra{"not a preamble"}',
])
def test_invalid_batch_is_rejected_as_a_whole(text):
    with pytest.raises(ValueError):
        bib.parse_bibtex(text)


@pytest.mark.parametrize('text', ['', ' ', None, b'@book{x,title={A}}',
                                   '@book{x,title={Unsafe\x00}}', '@book{x,title={Bad\u202e}}'])
def test_invalid_input_has_no_partial_result(text):
    with pytest.raises(ValueError):
        bib.parse_bibtex(text)


def test_duplicate_errors_do_not_log_private_input(caplog):
    sentinel = 'private-title-do-not-log'
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError):
            bib.parse_bibtex('@book{a,title={A},' + sentinel + '}')
    assert sentinel not in caplog.text


def test_recursive_macro_expansion_is_bounded_before_allocating_result():
    strings = ['@string{s0={0123456789}}']
    strings.extend(f'@string{{s{i}=s{i-1} # s{i-1}}}' for i in range(1, 19))
    with pytest.raises(ValueError, match='budget'):
        bib.parse_bibtex('\n'.join(strings) + '\n@book{x,title=s18}')


def test_macro_chain_depth_is_bounded():
    strings = [f'@string{{s{i}=s{i+1}}}' for i in range(bib.MAX_MACRO_DEPTH + 1)]
    strings.append(f'@string{{s{bib.MAX_MACRO_DEPTH + 1}={{end}}}}')
    with pytest.raises(ValueError, match='nested'):
        bib.parse_bibtex('\n'.join(strings) + '\n@book{x,title=s0}')


def _dependency_order(rows, order):
    if order == 'child-first':
        return list(reversed(rows))
    if order == 'interleaved':
        return rows[::2] + rows[1::2]
    return rows


@pytest.mark.parametrize('order', ['parent-first', 'child-first', 'interleaved'])
@pytest.mark.parametrize('over_limit', [False, True])
@pytest.mark.parametrize(('leaf', 'expected'), [('{Synthetic leaf}', 'Synthetic leaf'), ('jan', 'January')])
def test_macro_depth_boundary_is_independent_of_definition_order(order, over_limit, leaf, expected):
    depth = bib.MAX_MACRO_DEPTH + int(over_limit)
    strings = ['@string{s0=' + leaf + '}']
    strings.extend(f'@string{{s{i}=s{i-1}}}' for i in range(1, depth))
    source = '\n'.join(_dependency_order(strings, order))
    source += f'\n@book{{x,title=s{depth - 1}}}'
    if over_limit:
        with pytest.raises(ValueError, match='nested BibTeX string'):
            bib.parse_bibtex(source)
    else:
        values, _ = one(source)
        assert values['title'] == expected


@pytest.mark.parametrize('order', ['parent-first', 'child-first', 'interleaved'])
@pytest.mark.parametrize('over_limit', [False, True])
def test_crossref_depth_boundary_is_independent_of_entry_order(order, over_limit):
    depth = bib.MAX_CROSSREF_DEPTH + int(over_limit)
    entries = ['@book{p0,title={Synthetic parent 0},year=2026}']
    entries.extend('@inbook{p%d,title={Synthetic parent %d},crossref={p%d}}' % (i, i, i - 1)
                   for i in range(1, depth))
    source = '\n'.join(_dependency_order(entries, order))
    if over_limit:
        with pytest.raises(ValueError, match='nested BibTeX cross-reference'):
            bib.parse_bibtex(source)
    else:
        rows = bib.parse_bibtex(source)
        assert len(rows) == depth
        assert {values['title'] for values, _ in rows} == {
            f'Synthetic parent {index}' for index in range(depth)}
        assert all(values['year'] == '2026' for values, _ in rows)


def test_macro_depth_counts_the_longest_chain_not_sibling_dependencies(monkeypatch):
    monkeypatch.setattr(bib, 'MAX_MACRO_DEPTH', 2)
    strings = [f'@string{{s{i}={{Word{i}}}}}' for i in range(20)]
    strings.append('@string{joined=' + ' # { } # '.join(f's{i}' for i in range(20)) + '}')
    source = '\n'.join(strings) + '\n@book{x,title=joined}'
    values, _ = one(source)
    assert values['title'] == ' '.join(f'Word{i}' for i in range(20))


def test_deeply_nested_input_is_rejected_before_upstream_parser(monkeypatch):
    monkeypatch.setattr(bib, '_upstream', lambda: pytest.fail('Upstream must not receive overdeep input'))
    with pytest.raises(ValueError, match='nesting'):
        bib.parse_bibtex('@book{x,title={' + '{' * bib.MAX_DEPTH + 'x' + '}' * (bib.MAX_DEPTH + 2))


def test_byte_budget_counts_utf8_not_python_characters(monkeypatch):
    monkeypatch.setattr(bib, 'MAX_BYTES', 60)
    with pytest.raises(ValueError, match='8 MiB'):
        bib.parse_bibtex('@book{x,title={' + 'Ї' * 25 + '}}')


def test_record_and_field_budgets(monkeypatch):
    monkeypatch.setattr(bib, 'MAX_RECORDS', 1)
    with pytest.raises(ValueError, match='references'):
        bib.parse_bibtex('@book{a,title={A}}\n@book{b,title={B}}')
    monkeypatch.setattr(bib, 'MAX_FIELDS', 1)
    with pytest.raises(ValueError, match='too many fields'):
        bib.parse_bibtex('@book{a,title={A},year=2026}')


def test_oversized_provenance_fails_without_truncation(monkeypatch):
    monkeypatch.setattr(bib, 'MAX_NOTES', 150)
    with pytest.raises(ValueError, match='no text was truncated'):
        bib.parse_bibtex('@book{x,title={A}, unknown={' + 'b' * 150 + '}}')


def test_biblatex_date_keywords_and_unsupported_fields_are_disclosed():
    values, warnings = one('''@online{site,title={Local research},date={2026-11-26},
        keywords={History, Ukraine; History},language={uk},urldate={2026-11-27},
        url={https://example.org},editor={Do Not Guess as Author},note={A note}}''')
    assert values['year'] == '2026'
    assert values['item_type'] == 'webpage'
    assert values['authors'] == ()
    assert values['tags'] == ('History', 'Ukraine')
    assert values['notes'].startswith('A note\n\nBibTeX import provenance')
    assert any('editor' in w and 'urldate' in w for w in warnings)
    assert any('full date' in w for w in warnings)


def test_grouped_keyword_punctuation_stays_with_one_keyword():
    values, _ = one('''@book{x,title={Keywords},keywords={{Research, development},Science;{A; B}}}''')
    assert values['tags'] == ('Research, development', 'Science', 'A; B')


def test_crossref_provenance_fanout_has_a_total_memory_budget(monkeypatch):
    text = '\n'.join(f'@inbook{{child{i},title={{Child {i}}},crossref={{parent}}}}' for i in range(20))
    text += '\n@book{parent,title={Parent},abstract={' + 'a' * 5000 + '}}'
    assert len(text.encode()) < 20000
    monkeypatch.setattr(bib, 'MAX_BYTES', 20000)
    with pytest.raises(ValueError, match='provenance exceed'):
        bib.parse_bibtex(text)


def test_source_text_is_immutable_and_result_reproducible():
    text = '@book{x,title={A},author={Doe, John},year=2026}'
    before = sha256(text.encode()).hexdigest()
    first = bib.parse_bibtex(text)
    second = bib.parse_bibtex(text)
    assert first == second
    assert sha256(text.encode()).hexdigest() == before


def test_real_sqlite_import_metadata_preserves_notes_and_scope_after_reopen(tmp_path):
    path = tmp_path / 'references.sqlite'
    db = sqlite3.connect(path, isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA recursive_triggers=ON')
    db.executescript(SCHEMA_V3 + ACADEMIC_SCHEMA + '\nPRAGMA user_version=4;')
    for workspace in ('first', 'second'):
        db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
        db.execute('INSERT INTO workspace_meta(workspace,name) VALUES(?,?)', (workspace, workspace))
    db.execute('BEGIN')
    library = AcademicLibrary(db, 'first')
    values, _ = one('@book{original-key,title={Two workspaces},author={{Research, Inc.}},year=2026}')
    saved = library.create(**values)
    assert saved.id != 'original-key'
    assert AcademicLibrary(db, 'second').list() == ()
    assert db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    assert 'BibTeX import provenance' not in library.export(format='csl-json')
    assert 'BibTeX import provenance' in library.export(format='csl-json', include_notes=True)
    db.commit()
    db.close()
    reopened = sqlite3.connect(path, isolation_level=None)
    try:
        reopened.execute('PRAGMA foreign_keys=ON')
        reopened.execute('BEGIN')
        row = AcademicLibrary(reopened, 'first').get(saved.id)
        assert 'Citation key: original-key' in row.notes
        assert row.authors == ('{Research, Inc.}',)
        assert AcademicLibrary(reopened, 'second').list() == ()
    finally:
        reopened.close()
