"""Real PDF search/reading must stay useful without claiming verified citations.

Fixtures are synthetic standard-font PDFs, parsed and rendered by the actual
pypdf/PDFium ingestion path. No visual text attestation, GUI, or clipboard is
mocked or claimed. The retained original and workspace checks remain mandatory
even when an extracted page is only available as an explicitly marked preview.
"""
from hashlib import sha256
import json

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from carr_library.application import Library


PDF_LINES = (
    'Railway evidence is synthetic original text for PDF extraction testing.',
    'Native extraction remains unverified against the rendered source page.',
)
TEXT_RAW = 'Railway evidence from independently byte-verified original text.\r\n'


def write_native_pdf(path):
    """Build standard-font PDF objects; no private or external source fixture."""
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    for lines in (('Synthetic preface with a different search term.',), PDF_LINES):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font}),
        })
        stream = DecodedStreamObject()
        commands = ['BT /F1 10 Tf 40 740 Td 16 TL']
        for index, line in enumerate(lines):
            if index:
                commands.append('T*')
            commands.append('(' + line + ') Tj')
        commands.append('ET')
        stream.set_data('\n'.join(commands).encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)
    # This is expected extraction text, not independent visual verification.
    return PdfReader(path).pages[1].extract_text()


@pytest.fixture
def mixed_library(tmp_path):
    app = Library(tmp_path / 'private-library')
    for workspace in ('alpha', 'beta'):
        app.create_workspace(workspace.capitalize(), workspace=workspace)
        app.create_topic(workspace, 'Research', topic='topic')
    with app.bind_scope('alpha').catalog_tx() as catalog:
        catalog.create_collection('selected', 'Selected sources')
    text = app.import_text('alpha', TEXT_RAW, title='Verified text',
                           authors=('Synthetic text author',),
                           collections=('selected',))
    assert text['state'] == 'complete'
    assert text['citation_status'] == 'byte_verified'
    # The existing verified source is searchable before introducing a PDF.
    assert len(app.search('alpha', 'Railway')) == 1

    pdf = tmp_path / 'synthetic-source.pdf'
    raw_pdf = write_native_pdf(pdf)
    imported = app.import_file('alpha', pdf, title='PDF extraction preview',
                               authors=('Synthetic PDF author',),
                               collections=('selected',))
    assert imported['state'] == 'complete'
    assert imported['citation_status'] == 'review_required'
    return app, text, imported, raw_pdf


def pdf_hit(app, **options):
    hits = app.search('alpha', 'Railway', include_unverified=True, **options)
    return next(hit for hit in hits if hit.title == 'PDF extraction preview')


@pytest.mark.parametrize('retrieval_mode', ['literal', 'thematic'])
@pytest.mark.parametrize('result_mode', ['short', 'thematic'])
def test_mixed_search_keeps_verified_text_and_explicit_pdf_preview(
        mixed_library, retrieval_mode, result_mode):
    app, _, _, raw_pdf = mixed_library
    hits = app.search('alpha', 'Railway', retrieval_mode=retrieval_mode,
                      result_mode=result_mode, include_unverified=True)
    assert len(hits) == 2
    text, = (hit for hit in hits if hit.title == 'Verified text')
    pdf, = (hit for hit in hits if hit.title == 'PDF extraction preview')
    assert text.citation_verified is True
    assert pdf.citation_verified is False
    assert text.quote in TEXT_RAW
    assert pdf.quote in raw_pdf
    assert 'extraction' in pdf.citation.lower()
    assert 'unverified' in pdf.citation.lower()
    assert 'PDF page 2' in pdf.page
    assert 'printed page unverified' in pdf.page.lower()
    services = app.bind_scope('alpha', 'topic')
    assert services.verified_copy('alpha', text.ref, text.revision, 'quote') == text.quote


def test_pdf_more_returns_exact_extraction_in_a_source_preview(mixed_library):
    from carr_library.reader.context import Context
    from carr_library.reader.source_preview import SourcePreview

    app, _, imported, raw_pdf = mixed_library
    hit = pdf_hit(app)
    preview = app.bind_scope('alpha', 'topic').load_context('alpha', hit.ref, hit.revision)
    assert isinstance(preview, SourcePreview)
    assert not isinstance(preview, Context)
    assert len(preview.pages) == 1
    page = preview.page(0)
    assert page.raw_text == raw_pdf
    assert page.quote == hit.quote
    assert page.citation == hit.citation
    assert page.raw_sha256 == sha256(raw_pdf.encode('utf-8')).hexdigest()
    assert page.identity.workspace == 'alpha'
    assert page.identity.edition_id == imported['edition_id']
    assert page.identity.asset_sha256 == imported['asset']
    assert page.identity.revision == hit.revision
    assert page.locator.display() == hit.page
    assert not hasattr(page, 'anchor')
    with pytest.raises(IndexError):
        preview.page(1)


def test_preview_coverage_is_separate_from_verified_coverage(mixed_library):
    app, _, _, _ = mixed_library
    report = app.search_report('alpha', 'Railway', include_unverified=True)
    assert report['status']['eligible_sources'] == 1
    assert report['status']['review_required_sources'] == 1
    assert report['status']['preview_sources'] == 1
    assert [hit.citation_verified for hit in report['hits']] == [True, False]
    assert app.search_status('beta')['preview_sources'] == 0


@pytest.mark.parametrize('field', ['quote', 'citation'])
def test_pdf_preview_cannot_be_copied_as_verified_evidence(mixed_library, field):
    app, _, _, _ = mixed_library
    hit = pdf_hit(app)
    services = app.bind_scope('alpha', 'topic')
    with pytest.raises(ValueError, match='unverified extraction'):
        services.verified_copy('alpha', hit.ref, hit.revision, field)


def test_pdf_preview_cannot_be_saved_as_verified_evidence(mixed_library):
    app, _, _, _ = mixed_library
    hit = pdf_hit(app)
    services = app.bind_scope('alpha', 'topic')
    with pytest.raises(ValueError, match='unverified extraction|verified source context required'):
        services.save_hit('alpha', hit.ref, hit.revision, hit.quote, hit.citation)
    with services.evidence_tx() as evidence:
        assert evidence.list_saved('topic') == ()


def test_pdf_preview_rejects_foreign_workspace(mixed_library):
    app, _, _, _ = mixed_library
    hit = pdf_hit(app)
    alpha = app.bind_scope('alpha', 'topic')
    beta = app.bind_scope('beta', 'topic')
    with pytest.raises(ValueError, match='scope mismatch'):
        alpha.load_context('beta', hit.ref, hit.revision)
    with pytest.raises(ValueError, match='source unavailable'):
        beta.load_context('beta', hit.ref, hit.revision)
    assert app.search('beta', 'Railway') == ()


def test_pdf_preview_rechecks_removed_collection_membership(mixed_library):
    app, _, imported, _ = mixed_library
    hit = pdf_hit(app, collection='selected')
    services = app.bind_scope('alpha', 'topic')
    with services.catalog_tx() as catalog:
        assert catalog.remove('selected', imported['edition_id'])
    hits = app.search('alpha', 'Railway', collection='selected', include_unverified=True)
    assert len(hits) == 1 and hits[0].title == 'Verified text'
    for action in ('context', 'copy', 'save'):
        with pytest.raises(ValueError, match='no longer in selected collection'):
            if action == 'context':
                services.load_context('alpha', hit.ref, hit.revision)
            elif action == 'copy':
                services.verified_copy('alpha', hit.ref, hit.revision, 'quote')
            else:
                services.save_hit('alpha', hit.ref, hit.revision, hit.quote, hit.citation)
    assert len(app.search('alpha', 'Railway', include_unverified=True)) == 2


def test_changed_retained_pdf_never_falls_back_to_a_preview(mixed_library):
    app, _, imported, _ = mixed_library
    hit = pdf_hit(app)
    retained = app.originals / (imported['asset'] + '.pdf')
    original = retained.read_bytes()
    changed = original.replace(b'Railway', b'Highway', 1)
    assert changed != original and len(changed) == len(original)
    retained.write_bytes(changed)
    services = app.bind_scope('alpha', 'topic')
    with pytest.raises(ValueError, match='changed|digest|hash|integrity'):
        services.load_context('alpha', hit.ref, hit.revision)
    with pytest.raises(ValueError, match='changed|digest|hash|integrity'):
        app.search('alpha', 'Railway', include_unverified=True)


@pytest.mark.parametrize('field,value', [
    (1, True), (2, True), (3, False), (2, -1), (3, 1000000), (3, 0),
])
def test_pdf_preview_rejects_malformed_reference_offsets(mixed_library, field, value):
    app, _, _, _ = mixed_library
    hit = pdf_hit(app)
    values = json.loads(hit.ref)
    values[field] = value
    reference = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with pytest.raises(ValueError):
        app.bind_scope('alpha', 'topic').load_context('alpha', reference, hit.revision)


def test_changed_verified_text_is_not_downgraded_to_pdf_style_preview(mixed_library):
    app, text, _, _ = mixed_library
    hit, = (row for row in app.search('alpha', 'Railway') if row.title == 'Verified text')
    retained = app.originals / (text['asset'] + '.txt')
    retained.write_bytes(b'Highway' + retained.read_bytes()[len(b'Railway'):])
    services = app.bind_scope('alpha', 'topic')
    with pytest.raises(ValueError, match='unverified extraction|changed|integrity'):
        services.load_context('alpha', hit.ref, hit.revision)
    with pytest.raises(ValueError, match='unverified extraction|changed|integrity'):
        services.verified_copy('alpha', hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='unverified extraction|changed|integrity'):
        app.search('alpha', 'Railway')


def test_pdf_preview_requires_explicit_discovery_choice(mixed_library):
    app, _, _, _ = mixed_library
    verified = app.search('alpha', 'Railway')
    assert len(verified) == 1 and verified[0].citation_verified
    both = app.search('alpha', 'Railway', include_unverified=True)
    assert len(both) == 2
    assert both[0].citation_verified and not both[1].citation_verified
    limited = app.search('alpha', 'Railway', include_unverified=True, limit=1)
    assert limited == verified
