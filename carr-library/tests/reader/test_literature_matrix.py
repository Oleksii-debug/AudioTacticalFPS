"""Real CSV round trips and read-only academic-matrix integration."""
import csv
from dataclasses import replace
import io
import json
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.insights import CatalogAnalysis, SourceInsight, analyze_catalog, NOTICE
from carr_library.research import literature_matrix as matrix


def source(**changes):
    row = SourceInsight(
        edition_id='source-edition', title='Історія науки — 研究',
        authors=('Олексій Петренко', 'Research and Documentation Consortium'),
        language='uk', edition='Second edition', volume='II', collections=('Research',),
        text_units=5, indexed_units=4, physical_pages=3, logical_segments=2,
        recorded_review_pages=1, evidence_count=2,
        topics=('Research / Methods [topic methods]',), tags=('methods',),
        issues=('not_fully_indexed',), asset_sha256='a9' * 32, revision='active-revision')
    return replace(row, **changes)


def analysis(*rows, **changes):
    report = CatalogAnalysis('workspace-id', 'Дослідницька бібліотека',
                             'collection-id', 'Selected sources', tuple(rows))
    return replace(report, **changes)


def csv_rows(report, **options):
    rendered = matrix.render_literature_matrix(report, **options)
    return list(csv.DictReader(io.StringIO(rendered, newline='')))


def test_real_catalog_and_saved_evidence_export_without_private_content(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research Україна')
    collection = app.create_collection(workspace, 'Library review')
    imported = app.add_text(workspace, '=SUM(1,2)',
        'Private original prose; the evidence target is a railway.',
        authors=('Smith and Jones Research Group',), language='en', collections=(collection,))
    # Separate editions sharing identical source bytes remain separate rows.
    second = app.add_text(workspace, 'Another edition',
        'Private original prose; the evidence target is a railway.', collections=(collection,))
    assert imported['asset'] == second['asset'] and imported['edition'] != second['edition']
    app.add_text(workspace, 'Outside selected collection', 'Unselected source text.')
    other = app.create_workspace('Private unrelated library')
    app.add_text(other, 'Unrelated private title', 'Unrelated private text.')
    topic = app.create_topic(workspace, 'Transport')
    hit = next(hit for hit in app.search(workspace, 'railway') if hit.edition_id == imported['edition'])
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision,
                            note='A PRIVATE RESEARCH NOTE that must not leave the library.')
    app.set_evidence_tags(workspace, topic, evidence, ('=HYPERLINK("example")', 'history'))

    report = analyze_catalog(app, workspace, collection=collection)
    csv_data = matrix.render_literature_matrix(report)
    rows = list(csv.DictReader(io.StringIO(csv_data, newline='')))
    assert rows[0]['record_type'] == 'report' and rows[0]['source_count'] == '2'
    assert json.loads(rows[0]['notice_json']) == NOTICE
    records = [row for row in rows if row['record_type'] == 'source']
    assert len(records) == 2
    row = next(row for row in records if json.loads(row['title_json']) == '=SUM(1,2)')
    assert json.loads(row['authors_json']) == ['Smith and Jones Research Group']
    assert json.loads(row['collections_json']) == ['Library review']
    assert row['physical_pages'] == '0' and row['logical_segments'] == '1'
    assert row['recorded_review_pages'] == '0' and row['evidence_count'] == '1'
    assert json.loads(row['tags_json']) == ['=HYPERLINK("example")', 'history']
    assert any('Transport' in label for label in json.loads(row['topics_json']))
    for format in ('csv', 'markdown', 'text'):
        output = matrix.render_literature_matrix(report, format=format)
        assert 'Private original prose' not in output
        assert 'A PRIVATE RESEARCH NOTE' not in output
        assert 'Outside selected collection' not in output
        assert 'Unrelated private' not in output
        assert imported['asset'] not in output and hit.revision not in output
        assert str(app.root) not in output
    # Rendering has no side effects on either evidence or source selection.
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway'
    assert app.evidence(workspace, topic)[0].note.startswith('A PRIVATE RESEARCH NOTE')


@pytest.mark.parametrize('payload', [
    '=HYPERLINK("https://example.invalid", "open")', '+1+2', '-1+2', '@SUM(A1:A2)',
    '\t\r\n=2+3', ' \ufeff＝SUM(A1:A2)', '\u202e＠SUM(A1:A2)',
    '\",;\r\n=2+3\x00\u0085\u2028\u2029',
])
def test_every_untrusted_csv_cell_is_formula_inert_and_exactly_reversible(payload):
    row = source(title=payload, authors=(payload,), language=payload, edition=payload,
        volume=payload, collections=(payload,), topics=(payload,), tags=(payload,), issues=(payload,),
        edition_id='=identity', revision='+revision')
    report = analysis(row, workspace='@workspace', workspace_name=payload,
                      collection_id='-collection', collection_name=payload, warnings=(payload,))
    output = matrix.render_literature_matrix(report, include_trace=True)
    records = list(csv.DictReader(io.StringIO(output, newline='')))
    assert len(records) == 2 and len(output.splitlines()) == 3
    metadata, data = records
    assert json.loads(metadata['warnings_json']) == [payload]
    for field in ('title', 'language', 'edition', 'volume'):
        assert json.loads(data[field + '_json']) == payload
    for field in ('authors', 'collections', 'topics', 'tags', 'checklist'):
        assert json.loads(data[field + '_json']) == [payload]
    assert json.loads(data['workspace_name_json']) == payload
    assert json.loads(data['collection_name_json']) == payload
    assert json.loads(data['edition_id_json']) == '=identity'
    assert json.loads(data['revision_id_json']) == '+revision'
    for record in records:
        for cell in record.values():
            assert not cell or cell[0] not in '=+-@\t\r\n＝＋－＠'
            assert not any(char in cell for char in '\x00\r\n\u0085\u2028\u2029\u202e\ufeff')
    assert row.title == payload and report.workspace_name == payload


def test_absent_fields_and_distinct_editions_remain_explicit():
    first = source(title=None, authors=(), language=None, edition=None, volume=None,
                   collections=(), revision=None)
    second = replace(first, edition_id='another-edition')
    rows = csv_rows(analysis(first, second))
    assert len(rows) == 3
    for row in rows[1:]:
        assert json.loads(row['title_json']) is None
        assert json.loads(row['authors_json']) == []
        assert row['physical_pages'] == '3' and row['logical_segments'] == '2'
        assert row['recorded_review_pages'] == '1' and row['evidence_count'] == '2'
    assert 'edition_id_json' not in rows[0] and 'asset_sha256_json' not in rows[0]
    assert 'revision_id_json' not in rows[0] and 'workspace_id_json' not in rows[0]


@pytest.mark.parametrize('format', ['csv', 'markdown', 'text'])
def test_trace_requires_explicit_boolean_and_no_extra_private_attributes_are_read(format):
    class PrivateSource(SourceInsight):
        @property
        def quote(self):
            pytest.fail('The matrix must not access source quotations.')

        @property
        def note(self):
            pytest.fail('The matrix must not access research notes.')

        @property
        def path(self):
            pytest.fail('The matrix must not access source paths.')

    item = source()
    private = PrivateSource(**vars(item))
    report = analysis(private)
    ordinary = matrix.render_literature_matrix(report, format=format)
    explicit = matrix.render_literature_matrix(report, format=format, include_trace=True)
    assert item.asset_sha256 not in ordinary and item.asset_sha256 in explicit
    with pytest.raises(ValueError, match='options'):
        matrix.render_literature_matrix(report, format=format, include_trace='yes')


@pytest.mark.parametrize('format', ['csv', 'markdown', 'text'])
def test_empty_scope_retains_actual_scope_notices_and_warnings(format):
    report = analysis(workspace_name='Empty library', collection_name='Empty collection',
                      warnings=('Stored research link could not be decoded.',))
    output = matrix.render_literature_matrix(report, format=format)
    assert 'Empty library' in output and 'Empty collection' in output
    assert 'Stored research link could not be decoded' in output
    assert 'not a fresh check of source authenticity' in output
    if format == 'csv':
        records = list(csv.DictReader(io.StringIO(output, newline='')))
        assert len(records) == 1 and records[0]['source_count'] == '0'
        assert records[0]['record_type'] == 'report'
    else:
        assert 'No sources in this scope.' in output


def test_long_visible_topic_paths_are_not_cut_off_or_relabelled():
    path = ' / '.join('Long research topic ' + str(n) for n in range(256)) + ' [topic exact-id]'
    assert len(path) > 4000
    rows = csv_rows(analysis(source(topics=(path,))))
    assert json.loads(rows[1]['topics_json']) == [path]


@pytest.mark.parametrize('format', ['csv', 'markdown', 'text'])
def test_cancellation_during_render_returns_no_report(format, monkeypatch):
    stopped = Event()
    original = matrix._source_value

    def read_value(row, field):
        value = original(row, field)
        if value == 'Cancel during this title':
            stopped.set()
        return value

    monkeypatch.setattr(matrix, '_source_value', read_value)
    with pytest.raises(InterruptedError, match='cancelled'):
        matrix.render_literature_matrix(analysis(source(title='Cancel during this title')),
                                        format=format, cancelled=stopped.is_set)
    assert stopped.is_set()


def test_already_cancelled_export_does_not_inspect_its_input():
    with pytest.raises(InterruptedError):
        matrix.render_literature_matrix(object(), cancelled=lambda: True)


@pytest.mark.parametrize('format', ['csv', 'markdown', 'text'])
def test_output_budget_rejects_whole_report_without_silent_truncation(format, monkeypatch):
    monkeypatch.setattr(matrix, 'MAX_OUTPUT_BYTES', 128)
    with pytest.raises(ValueError, match='output exceeds'):
        matrix.render_literature_matrix(analysis(source()), format=format)


@pytest.mark.parametrize('changes', [
    {'evidence_count': True}, {'text_units': -1}, {'indexed_units': 6},
    {'recorded_review_pages': 4}, {'logical_segments': 0},
    {'asset_sha256': '../source.pdf'}, {'authors': ['not an immutable list']},
    {'title': '\ud800'},
])
def test_invalid_analysis_records_fail_before_export(changes):
    with pytest.raises(ValueError):
        matrix.render_literature_matrix(analysis(source(**changes)))


def test_duplicate_identity_and_unbounded_input_are_rejected(monkeypatch):
    row = source()
    with pytest.raises(ValueError, match='Duplicate'):
        matrix.render_literature_matrix(analysis(row, row))
    monkeypatch.setattr(matrix, 'MAX_SOURCES', 1)
    with pytest.raises(ValueError, match='Too many'):
        matrix.render_literature_matrix(analysis(row, replace(row, edition_id='other')))
    monkeypatch.setattr(matrix, 'MAX_METADATA_BYTES', 10)
    with pytest.raises(ValueError, match='metadata exceeds'):
        matrix.render_literature_matrix(analysis(row))


def test_invalid_scope_or_options_never_yield_partial_data():
    report = analysis(source())
    with pytest.raises(ValueError, match='collection scope'):
        matrix.render_literature_matrix(replace(report, collection_name=None))
    with pytest.raises(ValueError, match='Choose CSV'):
        matrix.render_literature_matrix(report, format='html')
    with pytest.raises(ValueError, match='options'):
        matrix.render_literature_matrix(report, cancelled=False)
    with pytest.raises(ValueError, match='authorized catalog analysis'):
        matrix.render_literature_matrix(object())


def test_rendering_is_deterministic_and_preserves_source_order():
    first = source(title='First in the chosen catalog order', edition_id='z-edition')
    second = source(title='Second in that order', edition_id='a-edition')
    report = analysis(first, second)
    for format in ('csv', 'markdown', 'text'):
        result = matrix.render_literature_matrix(report, format=format)
        assert result == matrix.render_literature_matrix(report, format=format)
        assert result.index(first.title) < result.index(second.title)


@pytest.mark.parametrize('format', ['markdown', 'text'])
def test_readable_reports_use_normal_names_and_explicit_missing_values(format):
    row = source(title='Readable source', authors=('First author', 'Research Consortium'),
                 language=None, edition=None, volume=None, collections=(), tags=())
    output = matrix.render_literature_matrix(analysis(row), format=format)
    assert 'First author; Research Consortium' in output
    assert 'Not recorded' in output and 'None' in output
    assert 'json-v1' not in output and 'Format:' not in output
    assert '"Readable source"' not in output and 'null' not in output
    assert '["First author"' not in output
