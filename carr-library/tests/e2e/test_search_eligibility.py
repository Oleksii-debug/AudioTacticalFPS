"""Real native PDF candidates must not suppress verified TXT search results."""
import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.application import Library


@pytest.fixture
def mixed(tmp_path):
    app = Library(tmp_path / 'library')
    app.create_workspace('Research', workspace='a')
    app.create_workspace('Other', workspace='b')
    source = tmp_path / 'candidate.pdf'
    canvas = Canvas(str(source))
    canvas.drawString(72, 700, 'Shared railway phrase')
    canvas.save()
    pdf = app.import_file('a', source, title='Unreviewed native PDF',
                           authors=('PDF author',), work_id='pdf', edition_id='00-pdf')
    assert pdf['state'] == 'complete'
    assert pdf['citation_status'] == 'review_required'
    txt = app.import_text('a', 'Shared railway phrase', title='Byte-verified text',
                           authors=('TXT author',), work_id='txt', edition_id='99-txt')
    return app, pdf, txt


@pytest.mark.parametrize('mode', ['literal', 'thematic'])
def test_review_required_pdf_cannot_consume_verified_result_limit(mixed, mode):
    app, _, _ = mixed
    hits = app.search('a', 'railway', retrieval_mode=mode, limit=1)
    assert len(hits) == 1
    assert hits[0].title == 'Byte-verified text'
    assert hits[0].author == 'TXT author'
    assert hits[0].page == 'Segment 1 (unpaginated)'


@pytest.mark.parametrize('mode', ['literal', 'thematic'])
def test_corrupt_txt_is_not_silently_skipped_like_unreviewed_pdf(mixed, mode):
    app, _, txt = mixed
    (app.originals / (txt['asset'] + '.txt')).write_bytes(b'Changed original')
    with pytest.raises(ValueError, match='unverified extraction'):
        app.search('a', 'railway', retrieval_mode=mode)


def test_search_report_exposes_review_counts_without_cross_scope_leak(mixed):
    app, pdf, _ = mixed
    report = app.search_report('a', 'railway', limit=1)
    assert len(report['hits']) == 1
    assert report['status'] == dict(policy='strict_utf8_source_verification', eligible_sources=1,
                                    review_required_sources=1, not_indexed_sources=0, preview_sources=1)
    assert app.search_status('b') == dict(policy='strict_utf8_source_verification', eligible_sources=0,
                                         review_required_sources=0, not_indexed_sources=0, preview_sources=0)
    with app.bind_scope('a').catalog_tx() as catalog:
        catalog.create_collection('pdf-only', 'Awaiting source review')
        catalog.assign('pdf-only', pdf['edition_id'])
    report = app.search_report('a', 'railway', collection='pdf-only')
    assert report['hits'] == ()
    assert report['status']['eligible_sources'] == 0
    assert report['status']['review_required_sources'] == 1
    with app.bind_scope('a').catalog_tx() as catalog:
        catalog.archive('pdf-only')
    with pytest.raises(ValueError, match='archived collection'):
        app.search_report('a', 'railway', collection='pdf-only')


def test_registered_source_without_active_revision_is_explicitly_unindexed(mixed):
    app, _, txt = mixed
    asset_path = app.originals / (txt['asset'] + '.txt')
    with app.bind_scope('a').catalog_tx() as catalog:
        catalog.add_work('later-work', 'Not extracted yet')
        catalog.add_edition('later-edition', 'later-work',
            {'sha256': txt['asset'], 'mime': 'text/plain', 'size': asset_path.stat().st_size})
    report = app.search_report('a', 'railway')
    assert report['status']['not_indexed_sources'] == 1
    assert len(report['hits']) == 1
