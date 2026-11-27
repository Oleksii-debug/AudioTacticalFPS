"""Interoperability: a reviewed metadata preview can become a local citation."""
import json

import pytest

from carr_library.academic_integration import (
    CitationError, CitationParagraph, CitationRequest,
    export_citation_docx, read_citation_docx, render_citations,
)
from carr_library.acquisition.service import AcquisitionService
from carr_library.application import LibraryApp
from carr_library.ui.citation_formatting import CitationFormattingController


def test_metadata_preview_formats_locally_without_inventing_a_verified_pinpoint(tmp_path):
    requests = []
    source = {'status': 'ok', 'message-type': 'work', 'message': {
        'DOI': '10.5555/local-fixture', 'type': 'journal-article',
        'title': ['Documented findings'], 'author': [{'family': 'Doe', 'given': 'Jane'}],
        'issued': {'date-parts': [[2023]]}, 'container-title': ['Example Journal'],
        'volume': '12', 'issue': '3', 'page': '100-125',
    }}
    raw = json.dumps(source).encode()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed metadata')
    def factory(permission):
        assert permission.allowed is True and permission.purpose == 'metadata'
        def transport(request, *, cancelled):
            assert not cancelled()
            requests.append(request)
            return raw
        return transport
    service = AcquisitionService(app, transport_factory=factory)
    with pytest.raises(PermissionError):
        service.lookup(workspace, '10.5555/local-fixture')
    assert not requests
    preview = service.lookup(workspace, '10.5555/local-fixture', online_allowed=True)
    assert len(requests) == 1 and not preview.sources
    assert preview.metadata.pages == '100-125'
    assert service._provenance(preview)['source_text_verified'] is False
    reference = service.save_reference(workspace, preview)
    controller = CitationFormattingController(app, workspace)
    snapshot = controller.load()
    with pytest.raises(CitationError, match='Verify'):
        controller.render(snapshot, citations=[CitationRequest(reference.id, '112')])
    result = controller.render(snapshot, citations=[CitationRequest(reference.id)])
    # The canonical metadata service retains supplied full names literally.
    assert result.citations[0].text == '(Jane Doe, 2023)'
    assert '12(3), 100–125' in result.bibliography[0].text
    path = export_citation_docx(result, tmp_path / 'metadata-citations.docx',
        paragraphs=[CitationParagraph('The publication reports these findings.', ('citation-1',))])
    stored = read_citation_docx(path)
    assert stored['references'][0]['id'] == reference.id
    assert stored['references'][0]['DOI'] == '10.5555/local-fixture'
    assert stored['clusters'][0]['locator_evidence'][0]['locator_verified'] is None
    assert len(requests) == 1  # Formatting/export never contacts a service.


@pytest.mark.parametrize('style', ['apa', 'chicago-notes'])
@pytest.mark.parametrize(('author', 'literal'), [
    ('{Research and Development, Inc.}', 'Research and Development, Inc.'),
    (', Jr., John', ', Jr., John'),
    (r"Garc\'{i}a, Mar\'{i}a", 'García, María'),
    (r'\{Research Group\}', '{Research Group}'),
    (r'{\{Research Group\}}', '{Research Group}'),
])
def test_reviewed_bibtex_uses_existing_csl_document_workflow(tmp_path, style, author, literal):
    from hashlib import sha256

    from carr_library.application import LibraryApp
    from carr_library.ui.academic_library_dialog import AcademicController
    from carr_library.ui.citation_formatting import CitationFormattingController

    source = tmp_path / 'selected-reference.bib'
    source.write_text('@article{external-key,title={Synthetic findings},author={' + author
                      + '},year=2026,journal={Local Journal},pages={12--23}}', encoding='utf-8')
    original_hash = sha256(source.read_bytes()).hexdigest()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed BibTeX')
    academic = AcademicController(app, workspace)
    preview = academic.preview(source.read_bytes(), 'bibtex')
    assert academic.list() == ()
    imported = academic.import_selected(preview, (0,))
    identity, = imported.imported_ids
    saved = academic.get(identity)
    assert saved.authors == (literal,)
    assert 'Citation key: external-key' in saved.notes

    controller = CitationFormattingController(LibraryApp(app.root), workspace)
    snapshot = controller.load()
    assert snapshot.references[0]['author'] == [{'literal': literal}]
    result = controller.render(snapshot, style=style, citations=[CitationRequest(identity)])
    assert literal in result.bibliography[0].text
    assert 'BibTeX import provenance' not in result.bibliography[0].text
    output = controller.export_docx(snapshot, result, tmp_path / (style + '.docx'))
    embedded = read_citation_docx(output)
    assert embedded['references'][0]['author'] == [{'literal': literal}]
    assert embedded['references'][0]['id'] == identity
    assert embedded['clusters'][0]['locator_evidence'][0]['locator_verified'] is None
    assert sha256(source.read_bytes()).hexdigest() == original_hash
    assert academic.get(identity) == saved
