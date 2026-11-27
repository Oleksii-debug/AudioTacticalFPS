import pytest
from carr_library.academic_integration import CitationRequest, read_citation_docx


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
