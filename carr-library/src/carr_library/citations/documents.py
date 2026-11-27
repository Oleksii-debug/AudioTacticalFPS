"""Editable Word citations using the existing bounded CSL processor.

Native endnotes and hash-checked custom XML preserve a source snapshot for
regeneration. Visible Word edits are never certified by that metadata.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from .csl import CustomStyle, RenderBudget, _records, _clusters, render
from .bibliography_workbench import _parts
from ..reader.narration_text import read_document

SCHEMA = 'carr-citation-document/1'
_NAMESPACE = 'urn:carr:citations:1'
_PART = 'customXml/carr-citations.xml'
_MAX_DOCUMENT = 32 * 1024 * 1024
_MAX_METADATA = 16 * 1024 * 1024


@dataclass(frozen=True)
class CitationParagraph:
    text: str
    citation_ids: tuple[str, ...] = ()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def _text(value, label, limit):
    if not isinstance(value, str) or len(value) > limit or any(
            ord(char) < 32 and char not in '\t\r\n' for char in value):
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8')
    return value


def _markup(paragraph, value):
    for text, styles in _parts(value):
        run = paragraph.add_run(text)
        run.bold = bool(styles & {'b', 'strong'})
        run.italic = bool(styles & {'i', 'em'})
        run.font.superscript = 'sup' in styles
        run.font.subscript = 'sub' in styles
        run.font.small_caps = 'small-caps' in styles


def _bookmark(paragraph, identifier, number):
    # Reuses the canonical DOCX integration's WordprocessingML ordering.
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    start, end = OxmlElement('w:bookmarkStart'), OxmlElement('w:bookmarkEnd')
    start.set(qn('w:id'), str(number))
    start.set(qn('w:name'), 'carr_' + sha256(identifier.encode('utf-8')).hexdigest()[:30])
    end.set(qn('w:id'), str(number))
    position = 1 if len(paragraph._p) and paragraph._p[0].tag == qn('w:pPr') else 0
    paragraph._p.insert(position, start)
    paragraph._p.append(end)


def citation_ids(clusters):
    """Stable occurrence identities; moving citations requires regeneration."""
    return tuple(cluster['id'] if type(cluster) is dict else 'citation-' + str(index + 1)
                 for index, cluster in enumerate(clusters))


def citation_document(records, *, style='apa', locale='en-US', citations=None,
                      paragraphs=None, title='Citations and bibliography',
                      cancelled=lambda: False):
    """Return DOCX bytes; the existing scoped export service publishes them."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.shared import Inches, Pt
    from docx.text.paragraph import Paragraph
    from lxml import etree

    def check():
        if cancelled():
            raise TimeoutError('Citation document export cancelled.')
    check()
    title = _text(title, 'document title', 255)
    if not title.strip():
        raise ValueError('Choose a nonblank document title.')
    budget = RenderBudget()
    rows, identities, record_warnings = _records(records, budget)
    rendered = render(rows, style=style, locale=locale, citations=citations, output='html', budget=budget)
    clusters, _, contexts, _ = _clusters(citations, rows, identities, budget,
                                        note_style=rendered.style.kind == 'note')
    document_clusters = [dict(id=context['id'], items=cluster, note_index=context['note_index'] or None)
                         for cluster, context in zip(clusters, contexts)]
    if rendered.style.kind == 'note' and rendered.note_indices != tuple(range(1, len(clusters) + 1)):
        raise ValueError('Native Word endnotes require consecutive note numbers starting at 1.')
    check()
    ids = rendered.citation_ids
    if paragraphs is None:
        body = tuple(CitationParagraph('Citation example ' + str(index + 1), (identity,))
                     for index, identity in enumerate(ids))
    else:
        if not isinstance(paragraphs, (tuple, list)) or len(paragraphs) > 10_000:
            raise ValueError('The citation document exceeds its paragraph budget.')
        body = tuple(CitationParagraph(value) if isinstance(value, str) else value for value in paragraphs)
    used, total = [], 0
    for paragraph in body:
        if (not isinstance(paragraph, CitationParagraph) or not isinstance(paragraph.citation_ids, tuple)
                or len(paragraph.citation_ids) > 100):
            raise ValueError('Use typed paragraphs and bounded citation identities.')
        total += len(_text(paragraph.text, 'document paragraph', 200_000))
        if total > 4_000_000 or any(identity not in ids for identity in paragraph.citation_ids):
            raise ValueError('The document text or citation identity is invalid.')
        used.extend(paragraph.citation_ids)
    if tuple(used) != ids:
        raise ValueError('Document citation order must match the formatting sequence; regenerate after moving citations.')
    by_id = dict(zip(ids, rendered.citations))
    document = Document()
    normal = document.styles['Normal']
    normal.font.name, normal.font.size = 'Calibri', Pt(11)
    language = OxmlElement('w:lang')
    language.set(qn('w:val'), locale)
    normal.element.get_or_add_rPr().append(language)
    properties = document.core_properties
    properties.title, properties.subject = title, rendered.style.title[:255]
    properties.author = properties.last_modified_by = ''
    properties.comments = 'Editable CSL citations with source snapshot; no automatic Word field updates.'
    document.add_heading(title, 0)
    document.add_paragraph(rendered.style.title, style='Subtitle')
    endnotes = OxmlElement('w:endnotes')
    for identity, kind in ((-1, 'separator'), (0, 'continuationSeparator')):
        note = OxmlElement('w:endnote')
        note.set(qn('w:id'), str(identity))
        note.set(qn('w:type'), kind)
        paragraph, run = OxmlElement('w:p'), OxmlElement('w:r')
        run.append(OxmlElement('w:' + kind))
        paragraph.append(run)
        note.append(paragraph)
        endnotes.append(note)
    note_count, bookmark = 0, 1
    for record in body:
        check()
        paragraph = document.add_paragraph(record.text)
        for identity in record.citation_ids:
            if rendered.style.kind == 'note':
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
                native = Paragraph(note_p, document.part)
                number = OxmlElement('w:endnoteRef')
                number_run = native.add_run()
                number_run.font.superscript = True
                number_run._r.append(number)
                native.add_run(' ')
                _markup(native, by_id[identity])
                _bookmark(native, identity, bookmark)
            else:
                if paragraph.text:
                    paragraph.add_run(' ')
                _markup(paragraph, by_id[identity])
                _bookmark(paragraph, identity, bookmark)
            bookmark += 1
    if note_count:
        part = Part(PackURI('/word/endnotes.xml'),
            'application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml',
            etree.tostring(endnotes, xml_declaration=True, encoding='UTF-8', standalone=True))
        document.part.relate_to(part, RT.ENDNOTES)
    document.add_heading('Bibliography', 1)
    for entry, reference_ids in zip(rendered.entries, rendered.entry_ids):
        check()
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(.5)
        paragraph.paragraph_format.first_line_indent = Inches(-.5)
        _markup(paragraph, entry)
        _bookmark(paragraph, 'reference:' + '|'.join(reference_ids), bookmark)
        bookmark += 1
    document.add_heading('Formatting provenance', 1)
    document.add_paragraph(rendered.attribution)
    document.add_paragraph('These citations and endnotes are editable text. They do not update automatically in Word. '
        'The embedded snapshot supports regeneration; it does not verify visible edits, quotations or page locators.')
    warnings = tuple(dict.fromkeys((*record_warnings, *rendered.warnings)))
    for warning in warnings:
        document.add_paragraph(warning)
    snapshot = {'schema': SCHEMA, 'records': rows, 'clusters': document_clusters,
        'citation_ids': list(ids), 'style': {**asdict(rendered.style), 'sha256': rendered.style_sha256,
            'custom_source': style.source.decode('utf-8') if isinstance(style, CustomStyle) else None},
        'locale': locale, 'processor': {'package': rendered.processor_package_version,
            'reported': rendered.processor_reported_version},
        'rendered': {'citations': list(rendered.citations), 'entries': list(rendered.entries),
                     'entry_ids': [list(group) for group in rendered.entry_ids]},
        'document': {'title': title, 'paragraphs': [asdict(value) for value in body]},
        'warnings': list(warnings), 'quotation_verified': False, 'visible_edits_verified': False}
    raw = _json(snapshot)
    if len(raw) > _MAX_METADATA:
        raise ValueError('Citation document provenance exceeds its byte budget.')
    metadata = etree.Element('{%s}citations' % _NAMESPACE, nsmap={None: _NAMESPACE})
    metadata.set('schema', SCHEMA)
    metadata.set('sha256', sha256(raw).hexdigest())
    metadata.text = raw.decode('utf-8')
    part = Part(PackURI('/' + _PART), 'application/xml',
        etree.tostring(metadata, xml_declaration=True, encoding='UTF-8', standalone=True))
    document.part.relate_to(part, RT.CUSTOM_XML)
    output = BytesIO()
    document.save(output)
    check()
    value = output.getvalue()
    if len(value) > _MAX_DOCUMENT:
        raise ValueError('Citation document exceeds its byte budget.')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate citation provenance field.')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-finite citation provenance value.')


def read_citation_docx(source):
    """Read inert embedded metadata only; never execute fields or follow links.

    A checksum detects corruption. It is not an authenticity signature, and
    matching it does not verify the visible Word text or a quotation.
    """
    from defusedxml import ElementTree
    data = read_document(source, byte_budget=_MAX_DOCUMENT)
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000 or sum(info.file_size for info in infos) > 100_000_000:
                raise ValueError('Citation document exceeds its archive budget.')
            names = set()
            for info in infos:
                path = PurePosixPath(info.filename)
                if (info.orig_filename != info.filename or '\\' in info.filename
                        or path.is_absolute() or '..' in path.parts or info.filename in names):
                    raise ValueError('Citation document contains an ambiguous archive entry.')
                names.add(info.filename)
            matches = [info for info in infos if info.filename == _PART]
            if len(matches) != 1 or matches[0].file_size > _MAX_METADATA:
                raise ValueError('No unique bounded citation snapshot is present.')
            raw = archive.read(matches[0])
        node = ElementTree.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if (node.tag != '{%s}citations' % _NAMESPACE or node.get('schema') != SCHEMA
                or len(node) != 0 or set(node.attrib) != {'schema', 'sha256'}):
            raise ValueError('Unsupported citation snapshot schema.')
        payload = (node.text or '').encode('utf-8')
        if sha256(payload).hexdigest() != node.get('sha256'):
            raise ValueError('Citation snapshot failed its integrity check.')
        result = json.loads(payload, object_pairs_hook=_unique, parse_constant=_constant)
        expected = {'schema', 'records', 'clusters', 'citation_ids', 'style', 'locale',
                    'processor', 'rendered', 'document', 'warnings', 'quotation_verified',
                    'visible_edits_verified'}
        if (type(result) is not dict or set(result) != expected or result['schema'] != SCHEMA
                or result['quotation_verified'] is not False or result['visible_edits_verified'] is not False):
            raise ValueError('Invalid citation snapshot contract.')
        budget = RenderBudget()
        rows, identities, _ = _records(result['records'], budget)
        if type(result['style']) is not dict or result['style'].get('kind') not in ('note', 'in-text'):
            raise ValueError('Invalid citation style metadata.')
        clusters, _, contexts, _ = _clusters(result['clusters'], rows, identities, budget,
            note_style=result['style']['kind'] == 'note')
        if result['citation_ids'] != [context['id'] for context in contexts]:
            raise ValueError('Citation snapshot occurrence identities changed.')
        return result
    except (BadZipFile, OSError, UnicodeError, RecursionError) as exc:
        raise ValueError('Citation document is unreadable or exceeds its input limits.') from exc
