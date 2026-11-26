"""Catalog reports over real imports, SQLite scopes and immutable evidence.

These tests inspect stored inventory. They do not attest real user documents or
replace Windows/NVDA acceptance of the desktop report dialogs.
"""
from contextlib import contextmanager
import csv
from hashlib import sha256
import io
import json
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog import insights
from carr_library.research.literature_matrix import render_literature_matrix


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'catalog-report-library')


def _snapshot(app):
    """Logical data and schema, including original evidence payloads and notes."""
    with app.read() as db:
        return tuple(db.iterdump())


def _anchor(app, workspace, phrase):
    hit, = app.search(workspace, phrase)
    return app.context(workspace, hit.ref, hit.revision).pages[0].anchor


@pytest.fixture
def sample(app):
    workspace = app.create_workspace('Research inventory')
    primary = app.create_collection(workspace, 'Primary sources')
    secondary = app.create_collection(workspace, 'Comparison sources')
    topic = app.create_topic(workspace, 'Transport research')
    raw_parts = ('PRIVATE ORIGINAL ALPHA', 'PRIVATE ORIGINAL BETA')
    first = app.add_text(workspace, 'First source', '\f'.join(raw_parts),
                         work_id='work-one', edition_id='source-one',
                         authors=('First Author',), language='uk',
                         edition='First edition', volume='II',
                         collections=(primary, secondary))
    second = app.add_text(workspace, 'Outside selected collection', 'EXCLUDED ORIGINAL GAMMA',
                          work_id='work-two', edition_id='source-two',
                          collections=(secondary,))
    assert first['state'] == second['state'] == 'complete'
    anchors = tuple(_anchor(app, workspace, phrase) for phrase in raw_parts)
    with app.evidence_transaction(workspace) as store:
        store.save(topic, 'two-segment-evidence', anchors, 'PRIVATE RESEARCH NOTE')
    app.set_evidence_tags(workspace, topic, 'two-segment-evidence', ('railways', 'history'))
    return dict(app=app, workspace=workspace, primary=primary, secondary=secondary,
                topic=topic, first=first, second=second, raw_parts=raw_parts)


def _all_reports(analysis):
    return (insights.format_overview(analysis), insights.format_checklist(analysis),
            *(render_literature_matrix(analysis, format=kind)
              for kind in ('csv', 'markdown', 'text')))


def test_collection_scoped_inventory_counts_span_once_and_never_exports_notes(sample):
    app, workspace = sample['app'], sample['workspace']
    foreign = app.create_workspace('FOREIGN LIBRARY NAME')
    with app.topic_transaction(foreign) as tree:
        tree.create(sample['topic'], 'FOREIGN TOPIC NAME')
    app.add_text(foreign, 'FOREIGN TITLE', 'FOREIGN PRIVATE ORIGINAL',
                 work_id='work-one', edition_id='source-one', authors=('FOREIGN AUTHOR',))
    hit, = app.search(foreign, 'FOREIGN PRIVATE ORIGINAL')
    evidence = app.save_hit(foreign, sample['topic'], hit.ref, hit.revision, 'FOREIGN NOTE')
    app.set_evidence_tags(foreign, sample['topic'], evidence, ('FOREIGN TAG',))
    before = _snapshot(app)

    analysis = insights.analyze_catalog(app, workspace, collection=sample['primary'],
                                        authorized_topic=sample['topic'])
    source, = analysis.sources
    assert analysis.collection_id == sample['primary']
    assert analysis.collection_name == 'Primary sources'
    assert source.edition_id == 'source-one'
    assert source.collections == ('Comparison sources', 'Primary sources')
    assert (source.text_units, source.indexed_units, source.physical_pages,
            source.logical_segments, source.recorded_review_pages) == (2, 2, 0, 2, 0)
    assert source.evidence_count == 1  # Two anchors and two tags, one saved record.
    assert source.topics == (f"Transport research [topic {sample['topic']}]",)
    assert source.tags == ('history', 'railways')
    assert source.issues == () and analysis.warnings == ()
    for report in _all_reports(analysis):
        for private in (*sample['raw_parts'], 'PRIVATE RESEARCH NOTE', 'EXCLUDED ORIGINAL',
                        'Outside selected collection', 'FOREIGN'):
            assert private not in report
    assert 'Source editions: 1.' in insights.format_overview(analysis)
    assert 'unpaginated logical segments: 2.' in insights.format_overview(analysis)
    assert _snapshot(app) == before


def test_archived_parent_hides_active_child_evidence_and_tags_without_destroying_rows(sample):
    app, workspace = sample['app'], sample['workspace']
    parent = app.create_topic(workspace, 'HIDDEN PARENT')
    child = app.create_topic(workspace, 'HIDDEN CHILD', parent=parent)
    hit, = app.search(workspace, sample['raw_parts'][0])
    evidence = app.save_hit(workspace, child, hit.ref, hit.revision, 'HIDDEN NOTE')
    app.set_evidence_tags(workspace, child, evidence, ('HIDDEN TAG',))
    with app.write(workspace) as db:
        db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                   (workspace, child, 'hidden-invalid', '{}', '0' * 64, 'HIDDEN BAD NOTE'))
    with app.topic_transaction(workspace) as tree:
        tree.archive(parent)
    before = _snapshot(app)

    analysis = insights.analyze_catalog(app, workspace, collection=sample['primary'])
    source, = analysis.sources
    assert source.evidence_count == 1 and analysis.warnings == ()
    assert source.tags == ('history', 'railways')
    assert all('HIDDEN' not in report for report in _all_reports(analysis))
    with app.read(workspace) as db:
        assert db.execute('SELECT archived FROM research_topic WHERE workspace=? AND id=?',
                          (workspace, child)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM research_evidence WHERE workspace=? AND topic=?',
                          (workspace, child)).fetchone()[0] == 2
    with pytest.raises(ValueError, match='archived'):
        insights.analyze_catalog(app, workspace, authorized_topic=child)
    assert _snapshot(app) == before


def test_historical_evidence_is_a_stored_link_even_when_retained_bytes_are_damaged(sample, monkeypatch):
    app, workspace = sample['app'], sample['workspace']
    retained = app.assets.resolve(sample['first']['asset'])
    old = insights.analyze_catalog(app, workspace, collection=sample['primary']).sources[0]
    app.update_metadata(workspace, 'source-one', title='Revised catalog title',
                        authors=('Revised Author',), language='en', edition='Second edition')
    retained.write_bytes(b'Deliberately damaged synthetic original')
    with pytest.raises(ValueError, match='retained asset digest mismatch'):
        app.search(workspace, sample['raw_parts'][0])
    before = _snapshot(app)

    def forbidden(*args, **kwargs):
        pytest.fail('Catalog inventory attempted to authenticate or read an original')

    monkeypatch.setattr(app, '_retained_source', forbidden)
    monkeypatch.setattr(app.assets, 'resolve', forbidden)
    analysis = insights.analyze_catalog(app, workspace, collection=sample['primary'])
    source, = analysis.sources
    assert source.revision != old.revision and source.title == 'Revised catalog title'
    assert source.evidence_count == 1 and source.tags == old.tags
    assert analysis.warnings == ()
    assert 'not a fresh check of source authenticity' in insights.format_overview(analysis)
    assert all('PRIVATE RESEARCH NOTE' not in report for report in _all_reports(analysis))
    assert _snapshot(app) == before


def test_pdf_counts_describe_recorded_review_not_current_source_authenticity(app, tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas

    pdf = tmp_path / 'synthetic-catalog-review.pdf'
    canvas = Canvas(str(pdf))
    canvas.drawString(72, 700, 'Synthetic catalog railway source')
    canvas.save()
    workspace = app.create_workspace('PDF inventory')
    imported = app.import_file(workspace, pdf, title='Synthetic PDF')
    assert imported['state'] == 'complete'
    hit, = app.search(workspace, 'railway')
    unreviewed, = insights.analyze_catalog(app, workspace).sources
    assert unreviewed.recorded_review_pages == 0
    assert 'missing_pdf_review' in unreviewed.issues
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic fixture reviewer', confirmed=True)
    assert insights.analyze_catalog(app, workspace).sources[0].recorded_review_pages == 1
    app.update_metadata(workspace, imported['edition'], title='Revised PDF metadata')
    revised, = insights.analyze_catalog(app, workspace).sources
    assert revised.recorded_review_pages == 0 and 'missing_pdf_review' in revised.issues
    revised_hit, = app.search(workspace, 'railway')
    assert revised_hit.revision != hit.revision and not revised_hit.verified
    app.review_pdf_page(workspace, revised_hit.ref, revised_hit.revision,
                        'Synthetic fixture reviewer', confirmed=True)
    retained = app.assets.resolve(imported['asset'])
    retained.write_bytes(b'Deliberately damaged synthetic PDF')
    before = _snapshot(app)

    def forbidden(*args, **kwargs):
        pytest.fail('Stored review inventory invoked a source reader or native renderer')

    monkeypatch.setattr(app.assets, 'resolve', forbidden)
    monkeypatch.setattr('carr_library.ingest.worker.isolated_render_fingerprint', forbidden)
    analysis = insights.analyze_catalog(app, workspace)
    source, = analysis.sources
    assert (source.physical_pages, source.logical_segments, source.recorded_review_pages) == (1, 0, 1)
    assert 'missing_pdf_review' not in source.issues
    for report in _all_reports(analysis):
        assert 'not a fresh check of source authenticity' in report
        assert 'Synthetic catalog railway source' not in report
    assert _snapshot(app) == before


def test_invalid_foreign_and_unknown_revision_links_are_warned_and_omitted(sample):
    app, workspace = sample['app'], sample['workspace']
    with app.read(workspace) as db:
        payload, checksum = db.execute('SELECT payload,checksum FROM research_evidence '
            'WHERE workspace=? AND topic=? AND id=?',
            (workspace, sample['topic'], 'two-segment-evidence')).fetchone()
    bad_rows = [('corrupt', payload, '0' * 64)]
    for label, field, value in (('foreign', 'workspace', 'another-library'),
                                ('unknown', 'revision', 'nonexistent-revision')):
        data = json.loads(payload)
        for anchor in data['anchors']:
            anchor['identity'][field] = value
        modified = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        bad_rows.append((label, modified, sha256(modified.encode('utf-8')).hexdigest()))
    with app.write(workspace) as db:
        for label, bad_payload, bad_checksum in bad_rows:
            db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                       (workspace, sample['topic'], label, bad_payload, bad_checksum, 'INVALID SECRET NOTE'))
            db.execute('INSERT INTO research_tag VALUES(?,?,?)',
                       (workspace, label, 'INVALID SECRET TAG ' + label))
            db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',
                       (workspace, sample['topic'], label, label))
    before = _snapshot(app)

    analysis = insights.analyze_catalog(app, workspace, collection=sample['primary'])
    source, = analysis.sources
    assert source.evidence_count == 1 and source.tags == ('history', 'railways')
    warning, = analysis.warnings
    assert warning.startswith('3 stored evidence record(s) could not be linked')
    for report in _all_reports(analysis):
        # Markdown escapes parentheses; the warning still carries the count.
        assert '3 stored evidence record' in report
        assert 'INVALID SECRET' not in report and 'PRIVATE ORIGINAL' not in report
    assert _snapshot(app) == before


def test_collection_archive_and_removal_change_scope_without_deleting_source(sample):
    app, workspace = sample['app'], sample['workspace']
    with app.catalog_transaction(workspace) as catalog:
        catalog.archive(sample['secondary'])
    selected = insights.analyze_catalog(app, workspace, collection=sample['primary'])
    assert selected.sources[0].collections == ('Primary sources',)
    with pytest.raises(ValueError, match='archived'):
        insights.analyze_catalog(app, workspace, collection=sample['secondary'])
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(sample['primary'], 'source-one')
    assert insights.analyze_catalog(app, workspace, collection=sample['primary']).sources == ()
    full = insights.analyze_catalog(app, workspace)
    assert len(full.sources) == 2
    first, = (row for row in full.sources if row.edition_id == 'source-one')
    assert first.evidence_count == 1 and 'unfiled' in first.issues


def test_empty_reports_still_authorize_workspace_collection_and_topic(app):
    workspace = app.create_workspace('Empty library')
    empty_collection = app.create_collection(workspace, 'Empty collection')
    foreign = app.create_workspace('Other library')
    foreign_collection = app.create_collection(foreign, 'Foreign collection')
    foreign_topic = app.create_topic(foreign, 'Foreign topic')
    parent = app.create_topic(workspace, 'Archived parent')
    child = app.create_topic(workspace, 'Unarchived child', parent=parent)
    with app.topic_transaction(workspace) as tree:
        tree.archive(parent)

    analysis = insights.analyze_catalog(app, workspace, collection=empty_collection)
    assert analysis.sources == () and analysis.warnings == ()
    for report in _all_reports(analysis):
        assert 'Empty library' in report and 'Empty collection' in report
    for options in ({'collection': foreign_collection}, {'authorized_topic': foreign_topic},
                    {'authorized_topic': child}, {'collection': 'missing-collection'}):
        with pytest.raises(ValueError):
            insights.analyze_catalog(app, workspace, **options)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        insights.analyze_catalog(app, workspace)


def test_report_cancellation_during_actual_sql_returns_no_partial_analysis(sample, monkeypatch):
    app, workspace = sample['app'], sample['workspace']
    before = _snapshot(app)
    event = Event()
    original_read = app.read

    @contextmanager
    def monitored_read(scope=None, **options):
        with original_read(scope, **options) as db:
            def seen(statement):
                if 'FROM RESEARCH_EVIDENCE E' in statement.upper():
                    event.set()
            db.set_trace_callback(seen)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(app, 'read', monitored_read)
    with pytest.raises(TimeoutError, match='cancelled|timed out'):
        insights.analyze_catalog(app, workspace, cancelled=event.is_set)
    assert event.is_set()
    assert _snapshot(app) == before
    monkeypatch.setattr(app, 'read', original_read)
    assert len(insights.analyze_catalog(app, workspace).sources) == 2


@pytest.mark.parametrize('constant,limit,diagnostic', [
    ('MAX_SOURCES', 1, 'Too many sources'),
    ('MAX_EVIDENCE', 0, 'saved evidence records'),
    ('MAX_PAYLOAD_BYTES', 1, 'reading limit'),
    ('MAX_TOPICS', 0, 'research topics'),
    ('MAX_LINKS', 0, 'collection memberships'),
    ('MAX_METADATA_BYTES', 1, 'Active collection names exceed the report metadata limit'),
])
def test_report_bounds_fail_explicitly_without_truncation_or_mutation(sample, monkeypatch,
                                                                    constant, limit, diagnostic):
    app = sample['app']
    before = _snapshot(app)
    monkeypatch.setattr(insights, constant, limit)
    with pytest.raises(ValueError, match=diagnostic):
        insights.analyze_catalog(app, sample['workspace'])
    assert _snapshot(app) == before


def test_missing_search_rows_are_reported_without_automatic_repair(sample):
    app, workspace = sample['app'], sample['workspace']
    with app.write(workspace) as db:
        db.execute('DELETE FROM indexed WHERE id=(SELECT p.id FROM page p JOIN active a '
                   'ON a.revision=p.revision WHERE a.workspace=? AND a.edition=? ORDER BY p.ordinal LIMIT 1)',
                   (workspace, 'source-one'))
    before = _snapshot(app)
    source, = insights.analyze_catalog(app, workspace, collection=sample['primary']).sources
    assert (source.text_units, source.indexed_units) == (2, 1)
    assert 'not_fully_indexed' in source.issues and source.evidence_count == 1
    assert _snapshot(app) == before


def test_matrix_csv_keeps_untrusted_metadata_in_reversible_inert_text_cells(app):
    workspace = app.create_workspace('=Library title')
    collection = app.create_collection(workspace, '+Collection title')
    title = '=HYPERLINK("https://invalid.example","title")\nSecond line\u202e'
    authors = ('+Author', '-Second author', '@Third author')
    imported = app.add_text(workspace, title, 'PRIVATE MATRIX ORIGINAL',
                            authors=authors, language='\t=language', edition='-edition', volume='@volume',
                            collections=(collection,))
    topic = app.create_topic(workspace, '=Topic name')
    hit, = app.search(workspace, 'PRIVATE MATRIX ORIGINAL')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'PRIVATE MATRIX NOTE')
    tags = ('=formula', '+tag', '-tag', '@tag')
    app.set_evidence_tags(workspace, topic, evidence, tags)
    before = _snapshot(app)

    analysis = insights.analyze_catalog(app, workspace, collection=collection)
    output = render_literature_matrix(analysis)
    reader = csv.DictReader(io.StringIO(output, newline=''))
    report, source = list(reader)
    assert report['record_type'] == 'report' and source['record_type'] == 'source'
    assert report['source_count'] == '1' and source['evidence_count'] == '1'
    assert json.loads(source['title_json']) == title
    assert json.loads(source['authors_json']) == list(authors)
    assert json.loads(source['tags_json']) == sorted(tags)
    assert json.loads(source['language_json']) == '\t=language'
    assert json.loads(report['workspace_name_json']) == '=Library title'
    assert json.loads(report['collection_name_json']) == '+Collection title'
    assert not {'edition_id_json', 'revision_id_json', 'asset_sha256_json',
                'workspace_id_json', 'collection_id_json'} & set(reader.fieldnames)
    for record in (report, source):
        for key, value in record.items():
            if key.endswith('_json') and value:
                assert value[0] not in '=+-@\t\r\n'
                json.loads(value)
    assert imported['asset'] not in output and imported['edition'] not in output
    for text in _all_reports(analysis):
        assert 'PRIVATE MATRIX ORIGINAL' not in text and 'PRIVATE MATRIX NOTE' not in text
    traced = list(csv.DictReader(io.StringIO(render_literature_matrix(analysis, include_trace=True))))
    assert json.loads(traced[1]['edition_id_json']) == imported['edition']
    assert json.loads(traced[1]['asset_sha256_json']) == imported['asset']
    assert _snapshot(app) == before


def test_checklist_source_identity_cannot_inject_a_second_report_line(app):
    workspace = app.create_workspace('Unusual identifiers')
    identity = 'source\nFalse report status:\u202e'
    result = app.add_text(workspace, 'Ordinary title', 'Private identifier fixture',
                          edition_id=identity)
    assert result['state'] == 'complete'
    report = insights.format_checklist(insights.analyze_catalog(app, workspace))
    assert '\nFalse report status:' not in report and '\u202e' not in report
    assert 'Source ID: source False report status:\\u202e' in report
