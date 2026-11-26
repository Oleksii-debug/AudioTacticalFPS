"""PDF review authority through the real desktop service, retained bytes and SQLite."""
import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.application import LibraryApp


@pytest.fixture
def reviewed_source(tmp_path):
    source = tmp_path / 'synthetic-review-source.pdf'
    canvas = Canvas(str(source), invariant=1)
    canvas.drawString(72, 700, 'A synthetic railway quotation for independent review.')
    canvas.drawString(290, 40, 'iv')
    canvas.save()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed synthetic sources')
    imported = app.import_file(workspace, source, title='Known drawing fixture',
                               authors=('Synthetic author',))
    assert imported['state'] == 'complete' and not imported['verified']
    hit, = app.search(workspace, 'railway quotation')
    return app, workspace, imported, hit


def test_pdf_review_requires_confirmation_and_does_not_invent_printed_page(reviewed_source):
    app, workspace, imported, hit = reviewed_source
    assert not hit.verified
    with pytest.raises(ValueError, match='must be confirmed'):
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'fixture reviewer')
    with pytest.raises(ValueError, match='unverified extraction'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    # This fixture is known from its drawing instructions. This explicit test
    # approval is not automatic approval of actual user sources.
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'fixture reviewer', confirmed=True)
    fresh, = app.search(workspace, 'railway quotation')
    assert fresh.verified
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway quotation'
    assert 'printed page unverified' in app.copy(workspace, hit.ref, hit.revision, 'citation')
    assert 'p. iv' not in fresh.citation


def test_pdf_review_revocation_blocks_cached_copy_and_saved_evidence(reviewed_source):
    app, workspace, imported, hit = reviewed_source
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'fixture reviewer', confirmed=True)
    topic = app.create_topic(workspace, 'Evidence')
    app.save_hit(workspace, topic, hit.ref, hit.revision)
    assert 'railway quotation' in app.export_topic(workspace, topic)
    app.revoke_pdf_review(workspace, imported['edition'], hit.revision, 1)
    assert not app.search(workspace, 'railway quotation')[0].verified
    for action in (app.copy, app.context):
        with pytest.raises(ValueError, match='unverified extraction'):
            action(workspace, hit.ref, hit.revision)
    for action in (app.evidence, app.export_topic):
        with pytest.raises(ValueError, match='unverified extraction'):
            action(workspace, topic)
    reopened = LibraryApp(app.root)
    with pytest.raises(ValueError, match='unverified extraction'):
        reopened.export_topic(workspace, topic)
    with pytest.raises(ValueError, match='revoked review'):
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'fixture reviewer', confirmed=True)


def test_changed_pdf_bytes_fail_even_when_visible_page_is_unchanged(reviewed_source):
    app, workspace, imported, hit = reviewed_source
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'fixture reviewer', confirmed=True)
    retained = app.assets.resolve(imported['asset'])
    original = retained.read_bytes()
    # Trailing PDF comments leave all visible pixels unchanged but invalidate
    # the immutable source identity. Review cannot transfer to these bytes.
    retained.write_bytes(original + b'\n% changed source identity\n')
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    assert not app.search(workspace, 'railway quotation')[0].verified
    retained.write_bytes(original)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway quotation'


def test_pdf_review_never_crosses_workspace_authority(reviewed_source):
    app, workspace, imported, hit = reviewed_source
    other = app.create_workspace('Other workspace')
    with pytest.raises(ValueError, match='active library'):
        app.review_pdf_page(other, hit.ref, hit.revision, 'fixture reviewer', confirmed=True)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM source_page_review').fetchone()[0] == 0
