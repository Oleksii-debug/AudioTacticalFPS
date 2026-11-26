"""Independent metadata, tag and explicit PDF-review workflows on real storage."""
from hashlib import sha256

import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.application import LibraryApp


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Primary research')
    topic = app.create_topic(workspace, 'Research evidence')
    return app, workspace, topic


def test_metadata_revision_preserves_original_and_saved_citation(library):
    app, workspace, topic = library
    raw = 'Railway history — незмінний оригінал.\r\nSecond line.'
    imported = app.add_text(workspace, 'Original title', raw, authors=('Original Author',),
                            language='uk', edition='First edition', volume='I')
    previous, = app.search(workspace, 'Railway history')
    evidence = app.save_hit(workspace, topic, previous.ref, previous.revision, 'Independent note')
    saved_before, = app.evidence(workspace, topic)
    export_before = app.export_topic(workspace, topic, format='text')
    originals_before = {path.name: path.read_bytes() for path in app.assets_dir.iterdir() if path.is_file()}

    revision = app.update_metadata(workspace, imported['edition'], title='Revised title',
                                   authors=('Revised Author',), language='en',
                                   edition='Revised description', volume='II')
    assert revision != previous.revision
    current, = app.search(workspace, 'Railway history', title='Revised title',
                           author='Revised Author', language='en', edition='Revised description')
    assert (current.revision, current.title, current.author) == (revision, 'Revised title', 'Revised Author')
    assert current.verified and current.quote == previous.quote
    assert app.search(workspace, 'Railway history', title='Original title') == ()
    assert app.assets.resolve(imported['asset']).read_bytes() == raw.encode('utf-8')
    assert {path.name: path.read_bytes() for path in app.assets_dir.iterdir() if path.is_file()} == originals_before
    assert app.copy(workspace, previous.ref, previous.revision, 'quote') == previous.quote
    assert 'Original title' in app.copy(workspace, previous.ref, previous.revision, 'both')
    assert 'Revised title' in app.copy(workspace, current.ref, revision, 'both')
    assert app.evidence(workspace, topic) == (saved_before,)
    assert app.export_topic(workspace, topic, format='text') == export_before
    assert LibraryApp(app.root).evidence(workspace, topic)[0].evidence_id == evidence


def test_metadata_change_cannot_target_foreign_workspace(library):
    app, workspace, _topic = library
    foreign = app.create_workspace('Other research')
    imported = app.add_text(workspace, 'Protected title', 'Protected railway source.')
    old, = app.search(workspace, 'railway')
    with pytest.raises(ValueError):
        app.update_metadata(foreign, imported['edition'], title='Cross-library replacement')
    new, = app.search(workspace, 'railway')
    assert (new.revision, new.title) == (old.revision, 'Protected title')
    assert app.browse(foreign) == ()


def test_tags_are_persistent_and_workspace_scoped(library):
    app, first, topic = library
    second = app.create_workspace('Other research')
    second_topic = app.create_topic(second, 'Research evidence')
    app.add_text(first, 'First source', 'Identical railway text.')
    app.add_text(second, 'Second source', 'Identical railway text.')
    hit_a, = app.search(first, 'railway')
    hit_b, = app.search(second, 'railway')
    evidence_a = app.save_hit(first, topic, hit_a.ref, hit_a.revision, 'First note')
    evidence_b = app.save_hit(second, second_topic, hit_b.ref, hit_b.revision, 'Second note')
    app.set_evidence_tags(first, topic, evidence_a, ('Archive', 'Україна', 'Archive'))
    app.set_evidence_tags(second, second_topic, evidence_b, ('Archive', 'Private second tag'))
    assert app.evidence_tags(first, topic, evidence_a) == ('Archive', 'Україна')
    assert [row.evidence_id for row in app.evidence_with_tag(first, topic, 'Archive')] == [evidence_a]
    assert app.evidence_with_tag(first, topic, 'Private second tag') == ()
    with pytest.raises(ValueError):
        app.evidence_tags(second, topic, evidence_a)
    with pytest.raises(ValueError):
        app.set_evidence_tags(second, second_topic, evidence_a, ('Injected',))
    reopened = LibraryApp(app.root)
    assert reopened.evidence_tags(second, second_topic, evidence_b) == ('Archive', 'Private second tag')
    app.set_evidence_tags(first, topic, evidence_a, ())
    assert app.evidence_tags(first, topic, evidence_a) == ()
    assert app.evidence_tags(second, second_topic, evidence_b) == ('Archive', 'Private second tag')
    saved_a, = app.evidence(first, topic)
    assert (saved_a.note, saved_a.anchor.quote) == ('First note', 'railway')


def pdf_source(tmp_path):
    path = tmp_path / 'review fixture.pdf'
    canvas = Canvas(str(path))
    canvas.drawString(72, 700, 'Reviewed railway history')
    canvas.showPage()
    canvas.drawString(72, 700, 'Separate unreviewed evidence')
    canvas.save()
    return path


def test_explicit_pdf_review_and_revocation_fail_closed(library, tmp_path):
    app, workspace, topic = library
    source = pdf_source(tmp_path)
    original_hash = sha256(source.read_bytes()).hexdigest()
    result = app.import_file(workspace, source, title='PDF research source')
    assert result['state'] == 'complete'
    candidate, = app.search(workspace, 'Reviewed railway history')
    assert not candidate.verified
    with pytest.raises(ValueError, match='confirmed'):
        app.review_pdf_page(workspace, candidate.ref, candidate.revision, 'Fixture reviewer')
    with pytest.raises(ValueError):
        app.copy(workspace, candidate.ref, candidate.revision, 'quote')

    # Explicit operator confirmation in this synthetic test is not a claim that
    # a real human reviewed an arbitrary document or that NVDA was exercised.
    app.review_pdf_page(workspace, candidate.ref, candidate.revision,
                        'Fixture reviewer', confirmed=True)
    reviewed, = app.search(workspace, 'Reviewed railway history')
    assert reviewed.verified
    assert app.copy(workspace, reviewed.ref, reviewed.revision, 'quote') == 'Reviewed railway history'
    untouched, = app.search(workspace, 'Separate unreviewed evidence')
    assert not untouched.verified
    evidence = app.save_hit(workspace, topic, reviewed.ref, reviewed.revision, 'Separate note')
    saved, = app.evidence(workspace, topic)
    assert saved.anchor.locator.printed is None and not saved.anchor.locator.verified
    assert saved.anchor.locator.index == 1
    with app.read(workspace) as db:
        payload_before = db.execute('SELECT payload FROM research_evidence WHERE workspace=? AND topic=? AND id=?',
                                   (workspace, topic, evidence)).fetchone()[0]

    app.revoke_pdf_review(workspace, reviewed.edition_id, reviewed.revision, 1)
    assert not app.search(workspace, 'Reviewed railway history')[0].verified
    for action in (lambda: app.copy(workspace, reviewed.ref, reviewed.revision, 'quote'),
                   lambda: app.evidence(workspace, topic),
                   lambda: app.export_topic(workspace, topic)):
        with pytest.raises(ValueError):
            action()
    with pytest.raises(ValueError, match='revoked'):
        app.review_pdf_page(workspace, reviewed.ref, reviewed.revision,
                            'Fixture reviewer', confirmed=True)
    with app.read(workspace) as db:
        assert db.execute('SELECT payload FROM research_evidence WHERE workspace=? AND topic=? AND id=?',
                          (workspace, topic, evidence)).fetchone()[0] == payload_before
    assert sha256(app.assets.resolve(result['asset']).read_bytes()).hexdigest() == original_hash


def test_pdf_metadata_revision_does_not_transfer_review(library, tmp_path):
    app, workspace, topic = library
    result = app.import_file(workspace, pdf_source(tmp_path), title='Original PDF title')
    old, = app.search(workspace, 'Reviewed railway history')
    app.review_pdf_page(workspace, old.ref, old.revision, 'Fixture reviewer', confirmed=True)
    app.save_hit(workspace, topic, old.ref, old.revision)
    old_saved, = app.evidence(workspace, topic)
    revision = app.update_metadata(workspace, result['edition'], title='New PDF title')
    new, = app.search(workspace, 'Reviewed railway history')
    assert new.revision == revision and new.title == 'New PDF title' and not new.verified
    with pytest.raises(ValueError):
        app.copy(workspace, new.ref, new.revision, 'quote')
    assert app.evidence(workspace, topic) == (old_saved,)
    assert 'Original PDF title' in app.export_topic(workspace, topic)

