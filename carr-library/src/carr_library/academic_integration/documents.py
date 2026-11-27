"""Editable native DOCX exports with embedded, hash-checked citation provenance.

The document contains ordinary Word runs, bookmarks and (for note styles)
native endnotes. It is not a Word add-in and does not claim live field updates.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
from typing import Callable, Sequence
from zipfile import BadZipFile, ZipFile

from lxml import etree

from carr_library.durable_state import local_path, regular
from carr_library.research.export_file import _filename, capture_export_destination, write_binary_export
from .formatting import CitationCancelled, CitationError, FormattedCitations, _text

_NAMESPACE = 'urn:carr:citations:1'
_PART = 'customXml/carr-citations.xml'
_MAX_DOCUMENT = 32_000_000


@dataclass(frozen=True)
class CitationParagraph:
    text: str
    citation_ids: tuple[str, ...] = ()


class _Runs(HTMLParser):
    def __init__(self, paragraph):
        super().__init__(convert_charrefs=True)
        self.paragraph = paragraph
        self.active = []

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.paragraph.add_run().add_break()
        else:
            self.active.append(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.active) - 1, -1, -1):
            if self.active[index] == tag:
                self.active.pop(index)
                break

    def handle_data(self, data):
        run = self.paragraph.add_run(data)
        run.bold = any(x in self.active for x in ('b', 'strong'))
        run.italic = any(x in self.active for x in ('i', 'em'))
        run.underline = 'u' in self.active
        run.font.superscript = 'sup' in self.active
        run.font.subscript = 'sub' in self.active
        run.font.small_caps = 'span' in self.active


def _markup(paragraph, html):
    parser = _Runs(paragraph)
    parser.feed(html)
    parser.close()


def _bookmark(paragraph, identifier, number):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    start, end = OxmlElement('w:bookmarkStart'), OxmlElement('w:bookmarkEnd')
    start.set(qn('w:id'), str(number))
    start.set(qn('w:name'), 'carr_' + sha256(identifier.encode('utf-8')).hexdigest()[:30])
    end.set(qn('w:id'), str(number))
    # WordprocessingML requires paragraph properties before all run-level
    # children, including bookmarks (bibliography indents add such properties).
    position = 1 if len(paragraph._p) and paragraph._p[0].tag == qn('w:pPr') else 0
    paragraph._p.insert(position, start)
    paragraph._p.append(end)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def citation_docx_bytes(result: FormattedCitations, *,
                        title: str = 'Citations and bibliography',
                        paragraphs: Sequence[CitationParagraph | str] | None = None,
                        cancelled: Callable[[], bool] | None = None) -> bytes:
    """Render native runs/endnotes and the source snapshot without publishing.

    Paragraph citation IDs refer to ``result.citations[].cluster_id``. Citation
    strings remain editable; originals and research-library records are never
    modified. The caller retains its destination and source authorization.
    """
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.shared import Inches, Pt
    from docx.text.paragraph import Paragraph

    if not isinstance(result, FormattedCitations):
        raise CitationError('Export requires a completed local CSL formatting result.')
    title = _text(title, 'Document title', 255)
    if not title.strip():
        raise CitationError('Choose a nonblank document title.')
    def check():
        if cancelled and cancelled():
            raise CitationCancelled('Citation document export was cancelled.')
    check()
    if paragraphs is None:
        body = [CitationParagraph(f'Citation {index + 1}', (citation.cluster_id,))
                for index, citation in enumerate(result.citations)]
    else:
        if not isinstance(paragraphs, (list, tuple)) or len(paragraphs) > 10_000:
            raise CitationError('The document paragraph budget was exceeded.')
        body = [CitationParagraph(x) if isinstance(x, str) else x for x in paragraphs]
    by_id = {x.cluster_id: x for x in result.citations}
    body_size = 0
    used_clusters = []
    for paragraph in body:
        if not isinstance(paragraph, CitationParagraph):
            raise CitationError('Use document text or CitationParagraph values.')
        body_size += len(_text(paragraph.text, 'Document paragraph', 200_000))
        if body_size > 4_000_000 or len(paragraph.citation_ids) > 100:
            raise CitationError('The document text or citation budget was exceeded.')
        if any(identifier not in by_id for identifier in paragraph.citation_ids):
            raise CitationError('A document paragraph names an unknown citation cluster.')
        used_clusters.extend(paragraph.citation_ids)
    if used_clusters != [x.cluster_id for x in result.citations]:
        raise CitationError('Document citation order must match the formatting result; regenerate after moving or repeating citations.')
    if result.style.kind == 'note' and any(
            value.note_index not in (None, index + 1) for index, value in enumerate(result.citations)):
        raise CitationError('Native endnote export needs consecutive note numbers starting at 1.')
    document = Document()
    section = document.sections[0]
    section.top_margin = section.bottom_margin = Inches(0.8)
    section.left_margin = section.right_margin = Inches(0.9)
    normal = document.styles['Normal']
    normal.font.name = 'Calibri'
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(7)
    language = OxmlElement('w:lang')
    language.set(qn('w:val'), result.locale)
    normal.element.get_or_add_rPr().append(language)
    properties = document.core_properties
    properties.title = title
    properties.subject = result.style.title[:255]
    properties.author = ''
    properties.last_modified_by = ''
    properties.comments = 'Editable CSL citations; embedded CARR provenance. No automatic Word field updates.'
    properties.keywords = 'CSL, bibliography, CARR'
    document.add_heading(title, 0)
    document.add_paragraph(result.style.title, style='Subtitle')

    endnotes = OxmlElement('w:endnotes')
    for identifier, tag, kind in ((-1, 'separator', 'separator'), (0, 'continuationSeparator', 'continuationSeparator')):
        note = OxmlElement('w:endnote')
        note.set(qn('w:id'), str(identifier))
        note.set(qn('w:type'), kind)
        paragraph, run = OxmlElement('w:p'), OxmlElement('w:r')
        run.append(OxmlElement('w:' + tag))
        paragraph.append(run)
        note.append(paragraph)
        endnotes.append(note)
    note_count, bookmark_id = 0, 1
    for record in body:
        check()
        paragraph = document.add_paragraph(record.text)
        for cluster_id in record.citation_ids:
            citation = by_id[cluster_id]
            if result.style.kind == 'note':
                note_count += 1
                reference = OxmlElement('w:endnoteReference')
                reference.set(qn('w:id'), str(note_count))
                run = paragraph.add_run()
                run.font.superscript = True
                run._r.append(reference)
                note = OxmlElement('w:endnote')
                note.set(qn('w:id'), str(note_count))
                note_p = OxmlElement('w:p')
                note.append(note_p)
                endnotes.append(note)
                native_paragraph = Paragraph(note_p, document.part)
                number = OxmlElement('w:endnoteRef')
                number_run = native_paragraph.add_run()
                number_run.font.superscript = True
                number_run._r.append(number)
                native_paragraph.add_run(' ')
                _markup(native_paragraph, citation.html)
                _bookmark(native_paragraph, 'citation:' + cluster_id + ':' + str(note_count), bookmark_id)
            else:
                if paragraph.text:
                    paragraph.add_run(' ')
                _markup(paragraph, citation.html)
                _bookmark(paragraph, 'citation:' + cluster_id + ':' + str(bookmark_id), bookmark_id)
            bookmark_id += 1
    if note_count:
        part = Part(PackURI('/word/endnotes.xml'),
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml',
                    etree.tostring(endnotes, xml_declaration=True, encoding='UTF-8', standalone=True))
        document.part.relate_to(part, RT.ENDNOTES)
    document.add_heading('Bibliography', 1)
    parameters = result.interchange['bibliography_parameters']
    for entry in result.bibliography:
        check()
        paragraph = document.add_paragraph()
        if parameters.get('hangingindent') or parameters.get('second-field-align'):
            paragraph.paragraph_format.left_indent = Inches(0.25)
            paragraph.paragraph_format.first_line_indent = Inches(-0.25)
        paragraph.paragraph_format.line_spacing = min(3, max(1, parameters.get('linespacing') or 1))
        paragraph.paragraph_format.space_after = Pt(7)
        _markup(paragraph, entry.html)
        _bookmark(paragraph, 'reference:' + '|'.join(entry.reference_ids), bookmark_id)
        bookmark_id += 1
    document.add_heading('Formatting provenance', 1)
    document.add_paragraph(result.attribution)
    document.add_paragraph('The embedded reference metadata and stable citation IDs allow regeneration. '
                           'These editable citations do not update automatically in Word. '
                           'A verified locator records the caller’s source check; it is not independent verification by the formatter.')
    for warning in result.warnings:
        document.add_paragraph(warning)
    snapshot = result.interchange
    snapshot['document'] = {'title': title, 'paragraphs': [
        {'text': p.text, 'citation_ids': list(p.citation_ids)} for p in body]}
    raw = _json(snapshot)
    if len(raw) > 16_000_000:
        raise CitationError('The citation document provenance exceeds its size budget.')
    metadata = etree.Element('{%s}citations' % _NAMESPACE, nsmap={None: _NAMESPACE})
    metadata.set('schema', '1')
    metadata.set('sha256', sha256(raw).hexdigest())
    metadata.text = raw.decode('utf-8')
    part = Part(PackURI('/' + _PART), 'application/xml', etree.tostring(metadata,
                xml_declaration=True, encoding='UTF-8', standalone=True))
    document.part.relate_to(part, RT.CUSTOM_XML)
    output = BytesIO()
    document.save(output)
    check()
    content = output.getvalue()
    if len(content) > _MAX_DOCUMENT:
        raise CitationError('The citation document exceeds its size budget.')
    return content


def export_citation_docx(result: FormattedCitations, destination: str | Path, *,
                         title: str = 'Citations and bibliography',
                         paragraphs: Sequence[CitationParagraph | str] | None = None,
                         cancelled: Callable[[], bool] | None = None,
                         private_root: str | Path | None = None) -> Path:
    """Publish a new document with captured destination identity.

    A standalone caller has no library scope: by default its protected root is
    this installed package. Library callers supply their data root and retain
    their own source/scope check through the cancellation callback.
    """
    path = local_path(destination)
    _filename(path.name)
    if path.suffix.lower() != '.docx':
        raise CitationError('Choose a new .docx destination.')
    if path.exists():
        raise FileExistsError('The citation document destination already exists.')
    regular(path.parent, directory=True)
    protected = Path(__file__).resolve().parents[1] if private_root is None else private_root
    def check():
        if cancelled and cancelled():
            raise CitationCancelled('Citation document export was cancelled.')
        return False
    check()
    approval = capture_export_destination(path, private_root=protected)
    content = citation_docx_bytes(result, title=title, paragraphs=paragraphs,
                                  cancelled=cancelled)
    receipt = write_binary_export(path, content, private_root=protected,
                                  cancelled=check, require_absent=True, approval=approval)
    # Shared publication reports a completed file even if staging cleanup needs
    # attention; its receipt warning is also logged for path-only API callers.
    return receipt.path


def read_citation_docx(source: str | Path) -> dict:
    """Read only CARR's embedded metadata; no document scripts or external links.

    The hash detects accidental corruption, not malicious edits to both the
    metadata and its hash. Edited visible Word text is never claimed identical
    to this source snapshot.
    """
    path = local_path(source, exists=True)
    if regular(path).st_size > _MAX_DOCUMENT:
        raise CitationError('The citation document exceeds its size budget.')
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000 or sum(i.file_size for i in infos) > 100_000_000:
                raise CitationError('The citation document exceeds its archive budget.')
            matching = [x for x in infos if x.filename == _PART]
            if len(matching) != 1 or matching[0].file_size > 16_000_000:
                raise CitationError('The document has no unique bounded CARR citation snapshot.')
            raw = archive.read(matching[0])
    except (OSError, BadZipFile) as exc:
        raise CitationError('The citation document is not a readable DOCX archive.') from exc
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise CitationError('Citation provenance must not contain external entities.')
    try:
        node = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        if node.tag != '{%s}citations' % _NAMESPACE or node.get('schema') != '1':
            raise CitationError('The document citation snapshot uses an unsupported schema.')
        payload = (node.text or '').encode('utf-8')
        if sha256(payload).hexdigest() != node.get('sha256'):
            raise CitationError('The document citation snapshot failed its integrity check.')
        result = json.loads(payload)
        if not isinstance(result, dict) or result.get('schema') != 1:
            raise CitationError('The document citation snapshot is invalid.')
        return result
    except (ValueError, etree.XMLSyntaxError) as exc:
        if isinstance(exc, CitationError):
            raise
        raise CitationError('The document citation snapshot is invalid.') from exc
