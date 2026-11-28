"""Real application/store regressions for the source-by-theme research view."""
from contextlib import contextmanager
from dataclasses import replace
import csv
import io
import json
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.research import topic_literature_matrix as matrix_module
from carr_library.research.topic_literature_matrix import build_literature_matrix, export_matrix
from carr_library.research.outline_export import export_topic_outline as export_outline


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Academic research')
    with app.topic_transaction(workspace) as tree:
        tree.create('root', 'Review')
        tree.create('later', 'Repeated topic', parent='root', position=20)
        tree.create('first', 'Repeated topic', parent='root', position=10)
        tree.create('deep', 'Nested section', parent='first')
        tree.create('hidden', 'Archived section', parent='root')
        tree.create('hidden-child', 'Hidden child', parent='hidden')
        tree.archive('hidden')
    return app, workspace


def save_source(app, workspace, topic, *, raw='Railway history and archives.',
                title='Source', author='Researcher', note='Independent note',
                tags=(), evidence='evidence'):
    imported = app.add_text(workspace, title, raw, authors=(author,), edition='First', volume='I')
    hit = next(hit for hit in app.search(workspace, 'Railway') if hit.edition_id == imported['edition'])
    with app.read(workspace) as db:
        anchor, _, _ = app._anchor(db, workspace, hit.ref, hit.revision)
    full = replace(anchor, start=0, end=len(raw), quote=raw)
    with app.evidence_transaction(workspace) as store:
        store.save(topic, evidence, full, note)
    if tags:
        app.set_evidence_tags(workspace, topic, evidence, tags)
    return imported, full


def test_topic_order_and_scoped_evidence_ids_reopen(research):
    app, workspace = research
    imported, anchor = save_source(app, workspace, 'root', tags=('Method', 'History'))
    with app.evidence_transaction(workspace) as store:
        for topic in ('first', 'deep', 'later'):
            # IDs are only unique within a topic; drill-down must retain both.
            store.save(topic, 'evidence', anchor, 'Note in ' + topic)
    app.set_evidence_tags(workspace, 'deep', 'evidence', ('Method',))
    own = build_literature_matrix(app, workspace, 'root')
    assert len(own.sections) == len(own.sources) == 1
    assert own.sources[0].evidence_count == 1
    result = build_literature_matrix(LibraryApp(app.root), workspace, 'root', include_descendants=True)
    assert [(s.topic_id, s.depth) for s in result.sections] == [('root', 0), ('first', 1), ('deep', 2), ('later', 1)]
    assert result.sections[2].path == ('Review', 'Repeated topic', 'Nested section')
    source, = result.sources
    assert source.edition_id == imported['edition']
    assert source.evidence_count == 4
    assert [(e.topic_id, e.evidence_id) for e in source.evidence] == [
        ('root', 'evidence'), ('first', 'evidence'), ('deep', 'evidence'), ('later', 'evidence')]
    assert result.themes == ('History', 'Method')
    assert source.theme_counts == (('History', 1), ('Method', 2))
    assert source.locations == ('Segment 1 (unpaginated)',)
    assert source.evidence[0].quotes == (anchor.quote,)
    assert source.notes[-1] == 'Note in later'


def test_repeated_titles_and_historical_metadata_are_distinct_sources(research):
    app, workspace = research
    imported, original = save_source(app, workspace, 'root', title='Same title', author='Original author')
    save_source(app, workspace, 'first', title='Same title', author='Different author',
                raw='Railway research in another work.', evidence='other')
    revision = app.update_metadata(workspace, imported['edition'], title='Same title',
        authors=('Revised author',), edition='Second', volume='II')
    current = next(hit for hit in app.search(workspace, 'Railway') if hit.edition_id == imported['edition'])
    current_id = app.save_hit(workspace, 'later', current.ref, revision)
    result = build_literature_matrix(app, workspace, 'root', include_descendants=True)
    assert len(result.sources) == 3
    sources = {(row.edition_id, row.revision_id): row for row in result.sources}
    old = sources[(imported['edition'], original.identity.revision)]
    new = sources[(imported['edition'], revision)]
    assert (old.author, old.edition, old.volume) == ('Original author', 'First', 'I')
    assert (new.author, new.edition, new.volume) == ('Revised author', 'Second', 'II')
    assert old.evidence[0].quotes == (original.quote,)
    assert new.evidence[0].evidence_id == current_id
    assert all(source.title == 'Same title' for source in result.sources)


def test_exact_tag_filter_and_empty_sections(research):
    app, workspace = research
    save_source(app, workspace, 'first', tags=('Method', 'Qualitative'))
    result = build_literature_matrix(app, workspace, 'root', include_descendants=True, tag_filter='Method')
    assert result.tag_filter == 'Method' and result.themes == ('Method', 'Qualitative')
    assert result.sources[0].evidence[0].topic_id == 'first'
    assert len(result.sections) == 4 and not result.sections[0].evidence
    assert build_literature_matrix(app, workspace, 'root', include_descendants=True, tag_filter='method').sources == ()
    assert build_literature_matrix(app, workspace, 'root', tag_filter='Method').sources == ()


def test_workspace_archive_and_cross_workspace_scope(research):
    app, workspace = research
    save_source(app, workspace, 'root')
    other = app.create_workspace('Another library')
    with app.topic_transaction(other) as tree:
        tree.create('root', 'Same root ID in another library')
        tree.create('foreign', 'Only in the other library')
    assert build_literature_matrix(app, other, 'root').sources == ()
    with pytest.raises(ValueError, match='archived'):
        build_literature_matrix(app, workspace, 'foreign')
    with pytest.raises(ValueError, match='archived'):
        build_literature_matrix(app, workspace, 'hidden-child')
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        build_literature_matrix(app, workspace, 'root')


def test_one_snapshot_for_topics_notes_and_tags(research, monkeypatch):
    app, workspace = research
    save_source(app, workspace, 'root', note='Old note', tags=('Old theme',))
    with sqlite3.connect(app.database) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    original_read, original_loaders = app.read, app._loaders
    reads = []

    @contextmanager
    def read(scope=None):
        reads.append(scope)
        with original_read(scope) as db:
            yield db

    def loaders(db, scope, **options):
        # A different connection commits after this read snapshot was opened.
        with sqlite3.connect(app.database) as writer:
            writer.execute('UPDATE research_evidence SET note=? WHERE workspace=?', ('New note', workspace))
            writer.execute('UPDATE research_tag SET name=? WHERE workspace=?', ('New theme', workspace))
        return original_loaders(db, scope, **options)

    monkeypatch.setattr(app, 'read', read)
    monkeypatch.setattr(app, '_loaders', loaders)
    monkeypatch.setattr(app, 'evidence_tags', lambda *_: pytest.fail('No per-item snapshot allowed'))
    result = build_literature_matrix(app, workspace, 'root')
    assert reads == [workspace]
    assert result.sources[0].notes == ('Old note',)
    assert result.themes == ('Old theme',)
    monkeypatch.setattr(app, '_loaders', original_loaders)
    refreshed = build_literature_matrix(app, workspace, 'root')
    assert refreshed.sources[0].notes == ('New note',) and refreshed.themes == ('New theme',)


@pytest.mark.parametrize('action', [build_literature_matrix, export_matrix, export_outline])
def test_damaged_retained_source_is_never_hidden_by_filter(research, action):
    app, workspace = research
    imported, _ = save_source(app, workspace, 'root', tags=('Included',))
    app.assets.resolve(imported['asset']).write_bytes(b'Damaged original source')
    with pytest.raises(ValueError):
        action(app, workspace, 'root', tag_filter='Unrelated')


def test_revoked_pdf_review_blocks_fresh_matrix_and_exports(research, tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    app, workspace = research
    pdf = tmp_path / 'review.pdf'
    canvas = Canvas(str(pdf))
    canvas.drawString(72, 700, 'Reviewed railway research')
    canvas.save()
    app.import_file(workspace, pdf, title='Reviewed PDF')
    hit, = app.search(workspace, 'railway')
    # Explicit confirmation for this synthetic fixture is not human acceptance.
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'Fixture reviewer', confirmed=True)
    app.save_hit(workspace, 'root', hit.ref, hit.revision)
    shown = build_literature_matrix(app, workspace, 'root')
    assert shown.sources[0].evidence[0].locations == ('Physical page 1 (printed page unverified)',)
    app.revoke_pdf_review(workspace, hit.edition_id, hit.revision, 1)
    for action in (build_literature_matrix, export_matrix, export_outline):
        with pytest.raises(ValueError):
            action(app, workspace, 'root')


def test_real_two_page_span_keeps_individual_quotes_locations_and_citations(research, tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    app, workspace = research
    pdf = tmp_path / 'two-pages.pdf'
    canvas = Canvas(str(pdf))
    for text in ('First railway slice.', 'Second railway slice.'):
        canvas.drawString(72, 700, text)
        canvas.showPage()
    canvas.save()
    app.import_file(workspace, pdf, title='Two-page study')
    hits = app.search(workspace, 'railway')
    assert len(hits) == 2
    for hit in hits:
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'Fixture reviewer', confirmed=True)
    anchors = []
    with app.read(workspace) as db:
        for hit in hits:
            anchor, load, _ = app._anchor(db, workspace, hit.ref, hit.revision)
            _, _, raw, verified = load(anchor.identity, anchor.locator)
            assert verified
            anchors.append(replace(anchor, start=0, end=len(raw), quote=raw))
    anchors = tuple(sorted(anchors, key=lambda anchor: anchor.locator.index))
    with app.evidence_transaction(workspace) as store:
        store.save('root', 'span', anchors, 'One note for both slices.')
    result = build_literature_matrix(app, workspace, 'root')
    source, = result.sources
    evidence, = source.evidence
    assert source.evidence_count == 1 and evidence.quotes == tuple(a.quote for a in anchors)
    assert len(evidence.citations) == len(evidence.locations) == 2
    assert 'Physical page 1' in evidence.citations[0] and 'Physical page 2' in evidence.citations[1]
    csv_row, = csv.DictReader(io.StringIO(export_matrix(app, workspace, 'root')))
    assert json.loads(csv_row['quotation_slices_json']) == [[a.quote for a in anchors]]
    text = export_outline(app, workspace, 'root', format='text')
    assert 'Source slice 1 of 2' in text and 'Source slice 2 of 2' in text
    quotes = [json.loads(line.removeprefix('Exact quotation: ')) for line in text.splitlines()
              if line.startswith('Exact quotation: ')]
    assert quotes == [a.quote for a in anchors]


def test_cancel_before_read_and_during_historical_verification(research, monkeypatch):
    app, workspace = research
    save_source(app, workspace, 'root')
    read = app.read
    monkeypatch.setattr(app, 'read', lambda *_: pytest.fail('Cancelled operation read the database'))
    with pytest.raises(TimeoutError):
        build_literature_matrix(app, workspace, 'root', cancelled=lambda: True)
    monkeypatch.setattr(app, 'read', read)
    original = app._loaders
    stopped = Event()

    def loaders(*args, **kwargs):
        load, metadata, attest = original(*args, **kwargs)
        def stop_after_read(*args):
            result = load(*args)
            stopped.set()
            return result
        return stop_after_read, metadata, attest

    monkeypatch.setattr(app, '_loaders', loaders)
    with pytest.raises(TimeoutError):
        build_literature_matrix(app, workspace, 'root', cancelled=stopped.is_set)


@pytest.mark.parametrize('limit,value', [('MAX_TOPICS', 1), ('MAX_DEPTH', 0),
    ('MAX_WORKSPACE_TOPICS', 1), ('MAX_TEXT_BYTES', 1), ('MAX_EVIDENCE', 0),
    ('MAX_SOURCES', 0), ('MAX_THEMES', 0), ('MAX_TAGS_PER_EVIDENCE', 0)])
def test_limits_fail_instead_of_dropping_results(research, monkeypatch, limit, value):
    app, workspace = research
    save_source(app, workspace, 'root', tags=('Theme',))
    monkeypatch.setattr(matrix_module, limit, value)
    with pytest.raises(ValueError, match='limit|Too many|exceeds'):
        build_literature_matrix(app, workspace, 'root', include_descendants=True)


def test_malformed_topic_cycle_cannot_hang(research):
    app, workspace = research
    with app.write(workspace) as db:
        db.execute('UPDATE research_topic SET parent=? WHERE workspace=? AND id=?', ('deep', workspace, 'root'))
    with pytest.raises(ValueError, match='archived'):
        build_literature_matrix(app, workspace, 'root', include_descendants=True)


@pytest.mark.parametrize('options', [{'include_descendants': 1}, {'tag_filter': ''},
    {'tag_filter': 'x\x00y'}, {'tag_filter': 42}, {'cancelled': None}])
def test_invalid_options_fail(research, options):
    app, workspace = research
    with pytest.raises(ValueError):
        build_literature_matrix(app, workspace, 'root', **options)


def test_csv_preserves_exact_text_and_neutralizes_formulas(research):
    app, workspace = research
    raw = 'Railway\r\n=SUM(A1:A2) | ``` <script> Café\u0301\u202e'
    note = '+SUM(1,2)\nSeparate note'
    title = '=HYPERLINK("https://invalid.example") | title'
    _, anchor = save_source(app, workspace, 'root', raw=raw, title=title,
        author='@author', note=note, tags=('-Theme', '=Theme'))
    content = export_matrix(app, workspace, 'root')
    row, = csv.DictReader(io.StringIO(content))
    assert row['text_encoding'] == 'json-v1' and row['evidence_count'] == '1'
    assert row['include_descendants'] == 'true' and json.loads(row['tag_filter_json']) is None
    assert json.loads(row['title_json']) == title
    assert json.loads(row['quotation_slices_json']) == [[raw]]
    assert json.loads(row['research_notes_json']) == [note]
    assert json.loads(row['evidence_ids_json']) == [['root', 'evidence']]
    assert json.loads(row['themes_json']) == ['-Theme', '=Theme']
    assert all(value[:1] not in '=+-@\t\r\n' for value in row.values())
    assert anchor.quote == raw and app.evidence(workspace, 'root')[0].anchor.quote == raw
    markdown = export_matrix(app, workspace, 'root', format='markdown')
    assert '# Literature review matrix' in markdown
    assert matrix_module._inline(title, table_cell=True) in markdown
    assert '\\| title' in matrix_module._inline(title, table_cell=True)
    assert matrix_module._block(raw) in markdown and matrix_module._block(note) in markdown


def test_export_is_fresh_and_output_limit_is_explicit(research, monkeypatch):
    app, workspace = research
    save_source(app, workspace, 'root', note='Old note')
    displayed = build_literature_matrix(app, workspace, 'root')
    with app.evidence_transaction(workspace) as store:
        store.edit_note('root', 'evidence', 'Changed note')
    assert displayed.sources[0].notes == ('Old note',)
    assert 'Changed note' in export_matrix(app, workspace, 'root')
    monkeypatch.setattr(matrix_module, 'MAX_EXPORT_BYTES', 1)
    with pytest.raises(ValueError, match='output size'):
        export_matrix(app, workspace, 'root')
    with pytest.raises(ValueError, match='output size'):
        export_outline(app, workspace, 'root')
