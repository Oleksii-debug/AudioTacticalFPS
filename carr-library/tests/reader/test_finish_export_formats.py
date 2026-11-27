"""Regression for the two export choices offered by the desktop application."""
import csv
from dataclasses import replace
from hashlib import sha256
import io
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.research.evidence_export import Evidence, export_topic


def source(**changes):
    raw = '=HYPERLINK("file:///unused","Цитата")\r\nExact\u202e text.'
    row = Evidence('workspace', 'topic', '=evidence', 'edition-id', 'old-revision',
                   'a' * 64, sha256(raw.encode('utf-8')).hexdigest(),
                   'physical_page', 2, None, 'ix', True, raw, 0, len(raw), raw,
                   '=Title', '@Author', '+First', True, '-Private note\nnext', 'II')
    return replace(row, **changes)


def csv_rows(rows):
    return list(csv.DictReader(io.StringIO(export_topic('workspace', 'topic', rows,
                                                       format='csv'))))


def test_csv_roundtrip_preserves_formula_like_source_notes_ids_and_controls():
    row = source()
    result, = csv_rows([row])
    assert result['text_encoding'] == 'json-string-v1'
    for column, original in (('quotation_json', row.quote), ('research_note_json', row.note),
                             ('title_json', row.title), ('author_json', row.author),
                             ('evidence_id_json', row.evidence_id),
                             ('edition_json', row.edition), ('asset_sha256_json', row.asset_sha256)):
        assert result[column].startswith('"')
        assert json.loads(result[column]) == original
    assert '\r' not in result['quotation_json'] and '\n' not in result['quotation_json']
    assert '\u202e' not in result['quotation_json']
    assert result['physical_page'] == '2' and result['logical_segment'] == ''
    assert json.loads(result['printed_page_json']) == 'ix'
    assert result['start'] == '0' and result['end'] == str(len(row.quote))


def test_csv_never_promotes_an_unverified_label_to_a_page_citation():
    result, = csv_rows([source(printed_label='candidate', printed_label_verified=False)])
    assert json.loads(result['printed_page_json']) is None
    assert result['printed_page_verified'] == 'false'
    assert 'candidate' not in result['citation_json']
    logical = source(kind='logical_segment', physical_index=None, segment_index=3,
                     printed_label=None, printed_label_verified=False)
    result, = csv_rows([logical])
    assert result['logical_segment'] == '3' and result['physical_page'] == ''
    assert 'unpaginated' in json.loads(result['citation_json'])
    assert 'Physical page' not in json.loads(result['citation_json'])


def test_csv_keeps_multpage_quotes_as_separate_exact_slices():
    first = source(span_count=2)
    second_raw = 'Second page\nDifferent source boundary.'
    second = replace(first, raw_text=second_raw, quote=second_raw, start=0,
                     end=len(second_raw), raw_text_sha256=sha256(second_raw.encode()).hexdigest(),
                     physical_index=3, printed_label='2', span_index=2)
    rows = csv_rows([first, second])
    assert [json.loads(row['quotation_json']) for row in rows] == [first.quote, second.quote]
    assert [row['source_slice'] for row in rows] == ['1', '2']
    assert [json.loads(row['evidence_id_json']) for row in rows] == [first.evidence_id] * 2
    assert [row['source_slices'] for row in rows] == ['2', '2']


def test_bibtex_escapes_unbalanced_braces_commands_and_metadata_line_breaks():
    hostile = '}\n@preamble{"unsafe"}\n\\input{not-a-file} % # $ & _ ^ ~ {'
    row = source(title=hostile, author='Name and Partners', edition='first\tline')
    result = export_topic('workspace', 'topic', [row], format='bibtex')
    assert result.count('@misc{evidence_') == 1
    assert '\\input{' not in result and '\n@preamble' not in result
    assert r'{\textbackslash}input{\textbraceleft}' in result
    assert r'{\textbraceright}' in result and r'{\textbraceleft}' in result
    assert 'author = {{Name and Partners}}' in result
    assert 'edition = {first line}' in result
    assert 'represented as spaces' in result
    assert row.quote not in result and row.note not in result
    assert 'pages = ' not in result and 'Physical page 2; printed page ix' in result


def test_bibtex_uses_scoped_stable_keys_and_never_fabricates_logical_pages():
    row = source(kind='logical_segment', physical_index=None, segment_index=4,
                 printed_label=None, printed_label_verified=False)
    one = export_topic('workspace', 'topic', [row], format='bibtex')
    assert one == export_topic('workspace', 'topic', [row], format='bibtex')
    two = export_topic('workspace', 'other', [replace(row, topic_id='other')], format='bibtex')
    first_key = next(line for line in one.splitlines() if line.startswith('@misc{'))
    second_key = next(line for line in two.splitlines() if line.startswith('@misc{'))
    assert first_key != second_key
    assert '=evidence' not in first_key and 'pages = ' not in one
    assert 'Segment 4 (unpaginated)' in one


@pytest.mark.parametrize('format', ['csv', 'bibtex'])
@pytest.mark.parametrize('changes', [{'quote': 'fabricated'}, {'workspace_id': 'other'},
                                    {'verified': False}, {'raw_text': 'changed'}])
def test_new_formats_cannot_bypass_source_scope_or_integrity(format, changes):
    with pytest.raises(ValueError):
        export_topic('workspace', 'topic', [source(**changes)], format=format)


@pytest.mark.parametrize('metadata', ['unsafe\x00title', 'unsafe\x1btitle', 'unsafe\u202etitle'])
def test_bibtex_rejects_nonprinting_metadata_controls(metadata):
    with pytest.raises(ValueError, match='control character'):
        export_topic('workspace', 'topic', [source(title=metadata)], format='bibtex')


def test_application_export_choices_resolve_stored_historical_evidence(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic research')
    topic = app.create_topic(workspace, 'Topic')
    app.add_text(workspace, 'Historical title', 'Exact quotation from source.',
                 authors=('Independent author',))
    hit, = app.search(workspace, 'Exact quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, '=Private research note')
    result, = csv.DictReader(io.StringIO(app.export_topic(workspace, topic, format='csv')))
    assert json.loads(result['quotation_json']) == 'Exact quotation'
    assert json.loads(result['research_note_json']) == '=Private research note'
    bibliography = app.export_topic(workspace, topic, format='bibtex')
    assert 'Historical title' in bibliography and 'Independent author' in bibliography
    assert 'Exact quotation' not in bibliography and 'Private research note' not in bibliography
