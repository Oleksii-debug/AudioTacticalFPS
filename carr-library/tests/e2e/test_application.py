"""Independent application-level checks: actual storage, retained bytes and services."""
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'private-library')


def test_manual_import_search_citation_notes_export_and_reopen(app):
    workspace = app.create_workspace('Research', 'A synthetic acceptance library')
    collection = app.create_collection(workspace, 'Sources')
    topic = app.create_topic(workspace, 'Transport')
    child = app.create_topic(workspace, 'Railways', parent=topic)
    raw = 'Передмова\r\nRailway history — Straße.\fДруга частина Україна.'
    imported = app.add_text(workspace, 'Synthetic history', raw,
                            authors=('Synthetic Author',), language='uk',
                            edition='First', volume='II', collections=(collection,))
    assert imported['state'] == 'complete' and imported['verified'] is True
    assert imported['completed'] == 2
    assert app.assets.resolve(imported['asset']).read_bytes() == raw.encode('utf-8')
    edition, = app.browse(workspace, collection=collection)
    assert edition.title == 'Synthetic history' and edition.authors == ('Synthetic Author',)
    assert {t.id for t in app.topics(workspace)} == {topic, child}

    hit, = app.search(workspace, 'railway history', collection=collection)
    assert hit.quote == 'Railway history' and hit.verified
    assert 'Segment 1' in hit.citation and 'Physical page' not in hit.citation
    context = app.context(workspace, hit.ref, hit.revision)
    assert context.pages[0].raw_text == raw.split('\f')[0]
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    thematic, = app.search(workspace, 'history', mode='thematic', collection=collection)
    assert thematic.quote in raw and thematic.verified

    evidence_id = app.save_hit(workspace, child, hit.ref, hit.revision, 'My own interpretation')
    with app.evidence_transaction(workspace) as evidence:
        evidence.edit_note(child, evidence_id, 'Updated independent research note')
    saved, = app.evidence(workspace, child)
    assert saved.anchor.quote == hit.quote and saved.note.startswith('Updated')
    output = app.export_topic(workspace, child, format='text')
    assert 'Exact quotation: "Railway history"' in output
    assert 'Updated independent research note' in output and 'Synthetic Author' in output
    query = app.save_query(workspace, child, 'Saved search', 'railway')
    assert app.run_query(workspace, child, query)[0].evidence.evidence_id == evidence_id

    reopened = LibraryApp(app.root)
    assert reopened.workspaces()[0].id == workspace
    assert reopened.evidence(workspace, child)[0] == saved
    assert reopened.export_topic(workspace, child, format='text') == output
    assert reopened.search(workspace, 'Україна')[0].quote == 'Україна'


def test_identical_ids_across_workspaces_do_not_cross_authorization(app):
    one = app.create_workspace('One')
    two = app.create_workspace('Two')
    shared = 'Shared source railway history.'
    a = app.add_text(one, 'Same title', shared, work_id='work', edition_id='edition')
    b = app.add_text(two, 'Same title', shared, work_id='work', edition_id='edition')
    assert a['asset'] == b['asset'] and a['id'] != b['id']
    assert len(list(app.assets_dir.glob('*.txt'))) == 1
    first, = app.search(one, 'railway')
    second, = app.search(two, 'railway')
    assert first.workspace != second.workspace
    with pytest.raises(ValueError):
        app.copy(two, first.ref, first.revision)
    collection = app.create_collection(one, 'Only one')
    with app.catalog_transaction(one) as catalog:
        catalog.assign(collection, 'edition')
    with pytest.raises(ValueError):
        app.search(two, 'railway', collection=collection)
    app.add_text(two, 'Other', 'Private unique secondworkspace keyword.')
    assert app.search(one, 'secondworkspace') == ()
    assert len(app.search(two, 'secondworkspace')) == 1


def test_removed_collection_membership_invalidates_live_hit_but_preserves_saved_evidence(app):
    workspace = app.create_workspace('Library')
    collection = app.create_collection(workspace, 'Selected collection')
    topic = app.create_topic(workspace, 'Historical evidence')
    imported = app.add_text(workspace, 'Document', 'Unmodified historical quotation.',
                            collections=(collection,))
    hit, = app.search(workspace, 'historical quotation', collection=collection)
    app.save_hit(workspace, topic, hit.ref, hit.revision)
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, imported['edition'])
    assert app.search(workspace, 'historical quotation', collection=collection) == ()
    for action in (app.context, app.copy):
        with pytest.raises(ValueError, match='left the selected collection'):
            action(workspace, hit.ref, hit.revision)
    with pytest.raises(ValueError, match='left the selected collection'):
        app.save_hit(workspace, topic, hit.ref, hit.revision)
    assert app.evidence(workspace, topic)[0].anchor.quote == 'historical quotation'
    assert 'historical quotation' in app.export_topic(workspace, topic)


def test_workspace_archive_denies_cached_and_reopened_actions(app):
    workspace = app.create_workspace('Archive me')
    app.add_text(workspace, 'Title', 'Source for archival checks.')
    hit, = app.search(workspace, 'archival')
    app.archive_workspace(workspace)
    assert app.workspaces() == ()
    assert app.workspaces(include_archived=True)[0].archived
    for current in (app, LibraryApp(app.root)):
        with pytest.raises(ValueError, match='archived'):
            current.search(workspace, 'archival')
        with pytest.raises(ValueError):
            current.copy(workspace, hit.ref, hit.revision)
    app.restore_workspace(workspace)
    assert app.search(workspace, 'archival')[0].verified


def test_catalog_filters_apply_to_literal_and_thematic_search(app):
    workspace = app.create_workspace('Filter fixture')
    app.add_text(workspace, 'Railway A', 'Bridge history in Ukraine.', authors=('Author A',),
                 language='uk', edition='First')
    app.add_text(workspace, 'Railway B', 'Bridge history in Slovakia.', authors=('Author B',),
                 language='sk', edition='Second')
    for mode in ('phrase', 'thematic'):
        hits = app.search(workspace, 'history', mode=mode, author='Author A', language='uk',
                           title='Railway A', edition='First')
        assert len(hits) == 1 and hits[0].author == 'Author A'
        assert app.search(workspace, 'history', mode=mode, author='Author A', language='sk') == ()


def test_cancelled_import_and_failed_batch_do_not_publish_partial_catalog(app, tmp_path):
    workspace = app.create_workspace('Import errors')
    cancelled = app.add_text(workspace, 'Cancelled', 'Source for a cancelled import.',
                             cancelled=lambda: True)
    assert cancelled['state'] == 'cancelled'
    assert app.browse(workspace) == () and app.search(workspace, 'Source') == ()
    valid = tmp_path / 'valid.txt'
    valid.write_text('Valid batch source.', encoding='utf-8')
    bad = tmp_path / 'invalid.pdf'
    bad.write_bytes(b'Not a PDF')
    result = app.import_files(workspace, (valid, bad))
    assert [r['state'] for r in result] == ['complete', 'failed']
    assert len(app.browse(workspace)) == 1


def test_pdf_import_requires_independent_review_before_citation(app, tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    pdf = tmp_path / 'synthetic.pdf'
    canvas = Canvas(str(pdf))
    canvas.drawString(72, 700, 'Synthetic railway history')
    canvas.save()
    workspace = app.create_workspace('PDF candidates')
    result = app.import_file(workspace, pdf, title='PDF fixture')
    assert result['state'] == 'complete' and result['verified'] is False
    hit, = app.search(workspace, 'railway')
    assert hit.verified is False
    assert 'independent review' in hit.citation
    assert 'Synthetic railway history' in app.preview(workspace, hit.ref, hit.revision)
    with pytest.raises(ValueError, match='unverified extraction'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    topic = app.create_topic(workspace, 'Evidence')
    with pytest.raises(ValueError, match='unverified extraction'):
        app.save_hit(workspace, topic, hit.ref, hit.revision)
    assert app.evidence(workspace, topic) == ()


def test_docx_import_stays_unpaginated_and_unverified(app, tmp_path):
    from docx import Document
    source = tmp_path / 'synthetic.docx'
    document = Document()
    document.add_paragraph('Synthetic railway source')
    document.add_paragraph('Second paragraph')
    document.save(source)
    workspace = app.create_workspace('DOCX candidates')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['verified'] is False
    hit, = app.search(workspace, 'railway')
    assert not hit.verified and 'Segment' in hit.page and 'Physical page' not in hit.page
    assert 'Synthetic railway source' in app.preview(workspace, hit.ref, hit.revision)
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_original_byte_drift_blocks_fresh_copy(app):
    workspace = app.create_workspace('Integrity')
    imported = app.add_text(workspace, 'Original', 'Original exact quotation.')
    hit, = app.search(workspace, 'exact quotation')
    retained = app.assets.resolve(imported['asset'])
    original = retained.read_bytes()
    retained.write_bytes(b'Altered retained source.')
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='retained asset digest mismatch'):
        app.search(workspace, 'exact quotation')
    retained.write_bytes(original)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'exact quotation'



@pytest.mark.parametrize('planned', [False, True], ids=['direct', 'planned'])
def test_actual_source_snapshot_cancellation_returns_without_retention_or_publication(
        app, tmp_path, monkeypatch, planned):
    from threading import Event
    from carr_library.catalog.intake import source_snapshot

    source = tmp_path / 'source.txt'
    raw = b'Exact source bytes remain outside the cancelled library.'
    source.write_bytes(raw)
    expected = source_snapshot(source.parent, source.name).manifest() if planned else None
    workspace = app.create_workspace('Cancellation during intake')
    stopped, opened = Event(), []
    actual_open = Path.open

    def open_and_stop(path, mode='r', *args, **kwargs):
        stream = actual_open(path, mode, *args, **kwargs)
        if path == source and mode == 'rb':
            # Arm only after the actual selected file descriptor exists. The
            # genuine intake reader must see the same callback after its read.
            opened.append(stream)
            stopped.set()
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', open_and_stop)
        result = app.import_file(workspace, source, cancelled=stopped.is_set,
                                 expected_manifest=expected)

    assert result['state'] == 'cancelled'
    assert len(opened) == 1 and opened[0].closed and stopped.is_set()
    assert source.read_bytes() == raw
    assert tuple(app.assets_dir.iterdir()) == ()
    assert not (app.root / 'imports.sqlite').exists()
    assert app.browse(workspace) == () and app.search(workspace, 'Exact') == ()


@pytest.mark.parametrize('planned', [False, True], ids=['direct', 'planned'])
def test_unrequested_source_interruption_is_not_reported_as_cancellation(
        app, tmp_path, monkeypatch, planned):
    from carr_library import application
    from carr_library.catalog.intake import source_snapshot
    from carr_library.ingest import batch_scan

    source = tmp_path / 'source.txt'
    raw = b'Unrequested interruption must remain an error.'
    source.write_bytes(raw)
    expected = source_snapshot(source.parent, source.name).manifest() if planned else None
    workspace = app.create_workspace('Unrequested intake interruption')
    cancelled = lambda: False
    problem = InterruptedError('Unrequested source interruption.')
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(args)
        assert kwargs['cancelled'] is cancelled
        raise problem

    with monkeypatch.context() as patch:
        if planned:
            patch.setattr(batch_scan, 'planned_snapshot', interrupted)
        else:
            patch.setattr(application, 'source_snapshot', interrupted)
        with pytest.raises(InterruptedError) as caught:
            app.import_file(workspace, source, cancelled=cancelled,
                            expected_manifest=expected)

    assert caught.value is problem and len(calls) == 1
    assert source.read_bytes() == raw
    assert tuple(app.assets_dir.iterdir()) == ()
    assert not (app.root / 'imports.sqlite').exists()
    assert app.browse(workspace) == () and app.search(workspace, 'interruption') == ()


@pytest.mark.parametrize('boundary', ['publication', 'cleanup'])
def test_interruption_after_actual_publication_preserves_commit_and_propagates(
        app, tmp_path, monkeypatch, boundary):
    from contextlib import closing
    from hashlib import sha256
    from threading import Event
    from carr_library import application

    source = tmp_path / 'source.txt'
    raw = b'Exact published quotation survives the later interrupted reply.'
    source.write_bytes(raw)
    workspace = app.create_workspace('Committed import interruption')
    stopped, published = Event(), []
    problem = InterruptedError('Interrupted after the real source publication.')
    actual_publish = application._CatalogPublisher.publish_revision
    actual_close = application.Ingest.close

    def publish(self, scope, edition, revision, asset, config, pages):
        actual_publish(self, scope, edition, revision, asset, config, pages)
        published.append((edition, revision, asset))
        if boundary == 'publication':
            stopped.set()
            raise problem

    def close(self):
        actual_close(self)
        stopped.set()
        raise problem

    with monkeypatch.context() as patch:
        patch.setattr(application._CatalogPublisher, 'publish_revision', publish)
        if boundary == 'cleanup':
            patch.setattr(application.Ingest, 'close', close)
        with pytest.raises(InterruptedError) as caught:
            app.import_file(workspace, source, cancelled=stopped.is_set)

    assert caught.value is problem and stopped.is_set() and len(published) == 1
    edition, revision, asset = published[0]
    assert asset == sha256(raw).hexdigest()
    assert source.read_bytes() == raw and app.assets.resolve(asset).read_bytes() == raw
    assert len(app.browse(workspace)) == 1
    hit, = app.search(workspace, 'published quotation')
    assert hit.revision == revision
    assert app.copy(workspace, hit.ref, revision, 'quote') == 'published quotation'
    with app.read(workspace) as db:
        assert tuple(db.execute('SELECT workspace,edition,asset FROM revision WHERE id=?',
                                (revision,)).fetchone()) == (workspace, edition, asset)
        assert db.execute('SELECT 1 FROM sealed WHERE revision=?', (revision,)).fetchone()
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
        state, = db.execute('SELECT state FROM job WHERE id=?', (revision,)).fetchone()
    assert state == ('failed' if boundary == 'publication' else 'complete')
