"""Interoperability: a reviewed metadata preview can become a local citation."""
import json

import pytest

from carr_library.academic_integration import (
    CitationError, CitationParagraph, CitationRequest, DoiMetadataClient,
    export_citation_docx, read_citation_docx, render_citations,
)
from carr_library.academic_integration.doi import MetadataResponse


def test_metadata_preview_formats_locally_without_inventing_a_verified_pinpoint(tmp_path):
    requests = []
    source = {'status': 'ok', 'message-type': 'work', 'message': {
        'DOI': '10.5555/local-fixture', 'type': 'journal-article',
        'title': ['Documented findings'], 'author': [{'family': 'Doe', 'given': 'Jane'}],
        'issued': {'date-parts': [[2023]]}, 'container-title': ['Example Journal'],
        'volume': '12', 'issue': '3', 'page': '100-125',
    }}
    raw = json.dumps(source).encode()
    def transport(request, *, cancelled):
        requests.append(request.public_url)
        return MetadataResponse(200, {'Content-Type': 'application/json'}, raw, request.url)
    preview = DoiMetadataClient(allow_network=True, transport=transport).lookup('10.5555/local-fixture')
    assert len(requests) == 1
    assert preview.review_required
    assert not preview.licenses
    assert preview.csl_item['page'] == '100-125'
    with pytest.raises(CitationError, match='Verify'):
        render_citations([preview.csl_item], citations=[CitationRequest(preview.reference_id, '112')])
    result = render_citations([preview.csl_item], citations=[CitationRequest(preview.reference_id)])
    assert result.citations[0].text == '(Doe, 2023)'
    assert '12(3), 100–125' in result.bibliography[0].text
    path = export_citation_docx(result, tmp_path / 'metadata-citations.docx',
        paragraphs=[CitationParagraph('The publication reports these findings.', ('citation-1',))])
    stored = read_citation_docx(path)
    assert stored['references'][0]['id'] == preview.reference_id
    assert stored['references'][0]['DOI'] == '10.5555/local-fixture'
    assert stored['clusters'][0]['locator_evidence'][0]['locator_verified'] is None
    assert len(requests) == 1  # Formatting/export never contacts a service.
