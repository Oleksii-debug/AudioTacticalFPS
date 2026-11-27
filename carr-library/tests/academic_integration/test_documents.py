from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from docx import Document
from lxml import etree
import pytest

from carr_library.academic_integration import (
    CitationCancelled, CitationError, CitationParagraph, CitationRequest,
    export_citation_docx, read_citation_docx, render_citations,
)


@pytest.fixture(scope='module')
def formatted():
    return render_citations([{
        'id': 'stable-reference-7', 'type': 'book', 'title': 'Наука & суспільство',
        'author': [{'family': 'Шевченко', 'given': 'Тарас'}],
        'issued': {'date-parts': [[2020]]}, 'publisher': 'Наукова думка'}],
        locale='uk-UA', citations=[CitationRequest('stable-reference-7', '12', locator_verified=True)])


def test_docx_contains_editable_unicode_runs_bookmarks_and_intact_provenance(tmp_path, formatted):
    path = export_citation_docx(formatted, tmp_path / 'research.docx', title='Дослідження',
        paragraphs=[CitationParagraph('Перевірена теза.', ('citation-1',))])
    loaded = Document(path)
    text = '\n'.join(p.text for p in loaded.paragraphs)
    assert 'Перевірена теза. (Шевченко, 2020, С. 12)' in text
    assert any(run.italic and run.text == 'Наука & суспільство'
               for paragraph in loaded.paragraphs for run in paragraph.runs)
    snapshot = read_citation_docx(path)
    assert snapshot['references'][0]['id'] == 'stable-reference-7'
    assert snapshot['clusters'][0]['locator_evidence'][0]['locator_verified'] is True
    assert snapshot['processor']['sha256'] == formatted.processor['sha256']
    assert snapshot['style']['sha256'] == formatted.style.sha256
    assert snapshot['document']['paragraphs'][0]['text'] == 'Перевірена теза.'
    with ZipFile(path) as archive:
        xml = archive.read('word/document.xml')
        assert b'bookmarkStart' in xml
        assert b'<w:hyperlink' not in xml
        assert not any(name.endswith('.bin') for name in archive.namelist())
        root = etree.fromstring(xml)
        namespace = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        for paragraph in root.iter(namespace + 'p'):
            if paragraph.find(namespace + 'pPr') is not None:
                assert paragraph[0].tag == namespace + 'pPr'


def test_notes_style_creates_actual_native_endnotes(tmp_path, formatted):
    result = render_citations(list(formatted.references), style='chicago-notes', citations=[
        CitationRequest('stable-reference-7', '12', locator_verified=True),
        CitationRequest('stable-reference-7', '18', locator_verified=True)])
    path = export_citation_docx(result, tmp_path / 'notes.docx', paragraphs=[
        CitationParagraph('First supported statement.', ('citation-1',)),
        CitationParagraph('Second supported statement.', ('citation-2',))])
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    with ZipFile(path) as archive:
        notes = etree.fromstring(archive.read('word/endnotes.xml'))
        body = etree.fromstring(archive.read('word/document.xml'))
        assert len(body.findall('.//w:endnoteReference', ns)) == 2
        actual = [x for x in notes.findall('w:endnote', ns) if int(x.get('{%s}id' % ns['w'])) > 0]
        assert len(actual) == 2
        for note, citation in zip(actual, result.citations):
            assert ''.join(note.itertext()).strip() == citation.text
        assert b'/endnotes' in archive.read('word/_rels/document.xml.rels')


def test_export_never_overwrites_and_cancel_leaves_no_artifact(tmp_path, formatted):
    path = tmp_path / 'preserved.docx'
    path.write_bytes(b'Original source bytes')
    with pytest.raises(FileExistsError):
        export_citation_docx(formatted, path)
    assert path.read_bytes() == b'Original source bytes'
    target = tmp_path / 'cancelled.docx'
    with pytest.raises(CitationCancelled):
        export_citation_docx(formatted, target, cancelled=lambda: True)
    assert not target.exists()


def test_reordering_document_citations_requires_real_reformatting(tmp_path, formatted):
    with pytest.raises(CitationError, match='order'):
        export_citation_docx(formatted, tmp_path / 'wrong.docx', paragraphs=['Citation omitted.'])
    with pytest.raises(CitationError, match='unknown'):
        export_citation_docx(formatted, tmp_path / 'unknown.docx',
            paragraphs=[CitationParagraph('Claim', ('invented-citation',))])
    assert not list(tmp_path.glob('*.docx'))


def test_embedded_provenance_corruption_is_rejected(tmp_path, formatted):
    path = export_citation_docx(formatted, tmp_path / 'good.docx')
    corrupted = tmp_path / 'corrupted.docx'
    with ZipFile(path) as source, ZipFile(corrupted, 'w', ZIP_DEFLATED) as output:
        for item in source.infolist():
            raw = source.read(item)
            if item.filename == 'customXml/carr-citations.xml':
                raw = raw.replace(b'stable-reference-7', b'stable-reference-9')
            output.writestr(item.filename, raw)
    with pytest.raises(CitationError, match='integrity'):
        read_citation_docx(corrupted)


@pytest.mark.parametrize('name', ['CON.docx', 'bad:name.docx', 'trailing.docx ', 'no-extension'])
def test_windows_unsafe_output_names_are_rejected_everywhere(tmp_path, formatted, name):
    with pytest.raises(ValueError):
        export_citation_docx(formatted, tmp_path / name)


def test_source_symlink_destination_is_rejected(tmp_path, formatted):
    original = tmp_path / 'original.docx'
    original.write_bytes(b'Preserved')
    link = tmp_path / 'linked.docx'
    try:
        link.symlink_to(original)
    except OSError:
        pytest.skip('Creating symlinks requires platform permissions.')
    with pytest.raises(ValueError):
        export_citation_docx(formatted, link)
    assert original.read_bytes() == b'Preserved'
