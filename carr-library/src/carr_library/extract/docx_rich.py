"""Canonical, conservative DOCX notes and embedded inline-picture admission.

Used only when rich constructs are present. No Word process, network, image
rendering, page inference or quotation approval is involved. Ordinary DOCX
continues through its byte-compatible original extractor branch.
"""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from zipfile import ZipFile

SOURCE_PROFILE = 'carr-docx-rich-source/1'
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'
PIC = '{http://schemas.openxmlformats.org/drawingml/2006/picture}'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
NOTES = ('footnote', 'endnote')
RICH_TAGS = frozenset((W + 'drawing', W + 'footnoteReference', W + 'endnoteReference'))
TEXT_TAGS = frozenset(W + name for name in ('t', 'tab', 'br', 'cr', 'noBreakHyphen', 'ptab'))
MAX_NODES = 100_000
MAX_RECORDS = 10_000
MAX_IMAGES = 2048
MAX_NOTES = 2048


def _sha(value):
    return sha256(value).hexdigest()


def _fail(reason):
    raise ValueError('unsupported body structure requires review: ' + reason)


def is_rich(document):
    return any(node.tag in RICH_TAGS for node in document.element.iter())


def _tree(root, budget):
    from .docx_ordered import _unsupported_tags
    from lxml import etree
    forbidden = _unsupported_tags() - RICH_TAGS
    if root.getroottree().docinfo.doctype:
        _fail('document type declarations')
    # Property containers are deliberately preserved but not source text.
    # Do not let malformed nested runs/markers disappear from both our walk
    # and python-docx Paragraph.text, which would make equality vacuous.
    properties = frozenset(W + name for name in
        ('pPr', 'rPr', 'tblPr', 'tblPrEx', 'tblGrid', 'trPr', 'tcPr', 'sectPr'))
    content = (TEXT_TAGS - {W + 'tab'}) | RICH_TAGS | frozenset(W + name for name in
        ('p', 'r', 'tbl', 'tr', 'tc', 'hyperlink', 'bookmarkStart', 'bookmarkEnd',
         'proofErr', 'footnoteRef', 'endnoteRef', 'lastRenderedPageBreak'))
    todo = [(root, 0, False)]
    while todo:
        node, depth, in_properties = todo.pop()
        budget[0] += 1
        if budget[0] > MAX_NODES or depth > 128:
            _fail('rich XML node or depth budget')
        if not isinstance(node.tag, str) or node.tag in forbidden:
            _fail('hidden, active or unrepresented rich XML')
        raw_text = etree._Element.text.__get__(node)
        raw_tail = etree._Element.tail.__get__(node)
        if ((raw_text or '').strip() and node.tag != W + 't') or (raw_tail or '').strip():
            _fail('text outside an ordinary text element')
        if node.tag == W + 't' and node.getparent().tag != W + 'r':
            _fail('text outside an ordinary run')
        if in_properties and (node.tag in content or
                (node.tag == W + 'tab' and node.getparent().tag != W + 'tabs')):
            _fail('text or fixed markers inside preserved properties')
        inside = in_properties or node.tag in properties
        todo.extend((child, depth + 1, inside) for child in node)


def _empty(node, attributes=()):
    if len(node) or (node.text or '').strip() or set(node.attrib) - set(attributes):
        _fail('nonempty or extended inline marker')


def _id(value, *, separator=False):
    pattern = r'-?(?:0|[1-9][0-9]{0,9})' if separator else r'(?:0|[1-9][0-9]{0,9})'
    if type(value) is not str or re.fullmatch(pattern, value) is None:
        _fail('invalid note identifier')
    number = int(value)
    if not -2147483648 <= number <= 2147483647:
        _fail('note identifier budget')
    return number


def _blocks(container, owner, *, body=False):
    """Return the same body/table locator tuples as the ordinary extractor."""
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    count = 0
    for child in container:
        if body and child.tag == W + 'sectPr':
            continue
        count += 1
        if child.tag == W + 'p':
            yield [count], Paragraph(child, owner)
        elif child.tag == W + 'tbl':
            table = Table(child, owner)
            if table._tbl.xpath('.//w:gridSpan | .//w:vMerge | .//w:tbl'):
                raise ValueError('complex table requires review')
            if any(node.tag not in (W + 'tblPr', W + 'tblGrid', W + 'tr') for node in child):
                _fail('extended table blocks')
            for j, row in enumerate(table.rows, 1):
                if row.grid_cols_before or row.grid_cols_after:
                    raise ValueError('nonrectangular table requires review')
                if any(node.tag not in (W + 'trPr', W + 'tc') for node in row._tr):
                    _fail('extended table row')
                for k, cell in enumerate(row.cells, 1):
                    if any(node.tag not in (W + 'tcPr', W + 'p') for node in cell._tc):
                        _fail('extended or nested cell')
                    for n, paragraph in enumerate(cell.paragraphs, 1):
                        yield [count, j, k, n], paragraph
        else:
            _fail('unknown body or note block')


def _notes(document, archive, budget):
    from docx.oxml import parse_xml
    parts, normal = {}, {}
    for kind in NOTES:
        path = 'word/' + kind + 's.xml'
        candidates = [rel for rel in document.part.rels.values() if rel.reltype == REL + kind + 's']
        if len(candidates) > 1 or bool(candidates) != (path in archive.namelist()):
            _fail('missing, duplicated or unattached note part')
        if not candidates:
            continue
        relation = candidates[0]
        if relation.is_external:
            _fail('external note part')
        part = relation.target_part
        expected_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.' + kind + 's+xml'
        if str(part.partname) != '/' + path or part.content_type != expected_type:
            _fail('noncanonical note part authority')
        raw = archive.read(path)
        if part.blob != raw:
            _fail('note part snapshot changed')
        root = parse_xml(raw)
        _tree(root, budget)
        if (root.tag != W + kind + 's' or set(root.attrib) - {
                '{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable'}):
            _fail('note part root')
        rows, ids, specials = {}, set(), set()
        for note in root:
            if note.tag != W + kind or set(note.attrib) - {W + 'id', W + 'type'}:
                _fail('note entry type')
            category = note.get(W + 'type', 'normal')
            number = _id(note.get(W + 'id'), separator=category != 'normal')
            if number in ids:
                _fail('duplicate note identifier')
            ids.add(number)
            if category == 'normal':
                if len(normal) >= MAX_NOTES:
                    _fail('note count budget')
                rows[number] = note
                normal[(kind, number)] = note
            elif category in ('separator', 'continuationSeparator'):
                if category in specials:
                    _fail('duplicate note separator')
                specials.add(category)
                # Separators are preserved package data, never silently omitted
                # ordinary note prose or executable/custom marker structures.
                if len(note) != 1 or note[0].tag != W + 'p':
                    _fail('nontrivial note separator')
                count = 0
                for child in note[0]:
                    if child.tag == W + 'pPr':
                        # The bounded tree pass already proved these preserved
                        # properties contain no hidden text/run/object markers.
                        continue
                    if child.tag != W + 'r':
                        _fail('nontrivial note separator')
                    for content in child:
                        if content.tag == W + 'rPr':
                            continue
                        if content.tag != W + category:
                            _fail('nontrivial note separator')
                        _empty(content)
                        count += 1
                if count != 1:
                    _fail('nontrivial note separator')
            else:
                _fail('unsupported note category')
        parts[kind] = {'path': path, 'part': part, 'root': root, 'normal': rows}
    return parts, normal


def _node_sha(node):
    from lxml import etree
    def shape(current):
        return [current.tag, sorted(current.attrib.items()),
                etree._Element.text.__get__(current), [shape(child) for child in current]]
    return _sha(json.dumps(shape(node), ensure_ascii=False, separators=(',', ':'),
                           allow_nan=False).encode('utf-8'))


def _picture(node, part, archive, media):
    from docx.image.image import Image
    allowed = ({W + 'drawing'}
        | {WP + name for name in ('inline', 'extent', 'effectExtent', 'docPr', 'cNvGraphicFramePr')}
        | {A + name for name in ('graphic', 'graphicData', 'graphicFrameLocks', 'picLocks',
            'blip', 'stretch', 'fillRect', 'srcRect', 'xfrm', 'off', 'ext', 'prstGeom', 'avLst')}
        | {PIC + name for name in ('pic', 'nvPicPr', 'cNvPr', 'cNvPicPr', 'blipFill', 'spPr')})
    if (len(node) != 1 or node[0].tag != WP + 'inline'
            or any(item.tag not in allowed or (item.text or '').strip() for item in node.iter())):
        _fail('only ordinary embedded inline pictures are supported')
    inline = node[0]
    graphics = inline.findall(A + 'graphic')
    extents = inline.findall(WP + 'extent')
    properties = inline.findall(WP + 'docPr')
    if len(graphics) != 1 or len(extents) != 1 or len(properties) != 1:
        _fail('ambiguous inline picture geometry')
    try:
        if any(not 0 < int(extents[0].get(key, '')) <= 2147483647 for key in ('cx', 'cy')):
            _fail('inline picture extent')
    except (TypeError, ValueError):
        _fail('inline picture extent')
    graphic = graphics[0]
    if (len(graphic) != 1 or graphic[0].tag != A + 'graphicData'
            or graphic[0].get('uri') != PIC[1:-1]
            or len(graphic[0]) != 1 or graphic[0][0].tag != PIC + 'pic'):
        _fail('picture is not a plain DrawingML picture')
    blips = node.findall('.//' + A + 'blip')
    if len(blips) != 1 or len(blips[0]) or set(blips[0].attrib) - {R + 'embed', 'cstate'}:
        _fail('linked, extended or ambiguous picture data')
    relation_id = blips[0].get(R + 'embed')
    relation = part.rels.get(relation_id)
    if relation is None or relation.reltype != REL + 'image' or relation.is_external:
        _fail('picture relationship is not an embedded image')
    target = relation.target_part
    path = str(target.partname).lstrip('/')
    if (not path.startswith('word/media/') or path.count('/') != 2
            or target.content_type not in ('image/png', 'image/jpeg') or target.rels):
        _fail('picture media kind or namespace')
    raw = archive.read(path)
    if raw != target.blob or not 0 < len(raw) <= 32 * 1024 * 1024:
        _fail('embedded image snapshot')
    try:
        image = Image.from_blob(raw)
        if (image.content_type != target.content_type or image.content_type not in ('image/png', 'image/jpeg')
                or not 0 < image.px_width <= 100000 or not 0 < image.px_height <= 100000
                or image.px_width * image.px_height > 100_000_000):
            _fail('embedded image header or geometry')
    except Exception as exc:
        raise ValueError('unsupported body structure requires review: embedded image header') from exc
    row = {'part': path, 'sha256': _sha(raw), 'size': len(raw), 'media_type': target.content_type,
           'width': image.px_width, 'height': image.px_height}
    if path in media and media[path] != row:
        _fail('embedded image identity changed')
    if path not in media and len(media) >= MAX_IMAGES:
        _fail('embedded image count')
    media[path] = row
    return {'kind': 'image', 'relationship_id': relation_id, 'xml_sha256': _node_sha(node), **row}


def _hyperlink(node, part):
    if set(node.attrib) - {R + 'id', W + 'anchor', W + 'history', W + 'tooltip', W + 'docLocation', W + 'tgtFrame'}:
        _fail('extended hyperlink')
    identity = node.get(R + 'id')
    anchor = node.get(W + 'anchor')
    if identity is not None:
        relation = part.rels.get(identity)
        if relation is None or relation.reltype != REL + 'hyperlink' or not relation.is_external:
            _fail('hyperlink relationship')
        value = relation.target_ref
        if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            _fail('hyperlink address')
        try:
            if urlsplit(value).scheme.casefold() not in ('https', 'http', 'mailto', 'tel'):
                _fail('active or unreviewed hyperlink scheme')
        except ValueError:
            _fail('hyperlink address')
    elif type(anchor) is not str or not anchor or len(anchor) > 1024:
        _fail('hyperlink has no exact target')


@dataclass
class ParagraphEntry:
    locator: object
    paragraph: object
    part_name: str
    markers: list
    segments: list


def _paragraph(paragraph, part, archive, media, *, note=None):
    markers, segments, current, text_parts = [], [], [], []
    offset, start = 0, 0
    mode = 'first-textual-run-style'

    def flush():
        nonlocal current, start
        if current:
            segments.append({'start': start, 'end': offset, 'nodes': tuple(current), 'inline_mode': mode})
        current, start = [], offset

    def run(node):
        nonlocal offset, start
        for child in node:
            if (child.tail or '').strip():
                _fail('non-element run text')
            if child.tag == W + 'rPr':
                continue
            if child.tag in TEXT_TAGS:
                value = str(child)
                if not current:
                    start = offset
                current.append(child)
                text_parts.append(value)
                offset += len(value)
            elif child.tag == W + 'lastRenderedPageBreak':
                _empty(child)
            elif child.tag in RICH_TAGS:
                flush()
                if child.tag == W + 'drawing':
                    marker = _picture(child, part, archive, media)
                else:
                    if note is not None:
                        _fail('a note cannot introduce another note reference')
                    _empty(child, (W + 'id',))
                    kind = 'footnote' if child.tag == W + 'footnoteReference' else 'endnote'
                    marker = {'kind': kind, 'note_id': _id(child.get(W + 'id'))}
                markers.append({'offset': offset, **marker})
            elif child.tag in (W + 'footnoteRef', W + 'endnoteRef'):
                _empty(child)
                if note is None or child.tag != W + note[0] + 'Ref':
                    _fail('unbound note label')
                flush()
                markers.append({'offset': offset, 'kind': 'note-label', 'note_kind': note[0], 'note_id': note[1]})
            else:
                _fail('unrepresented run content')

    for child in paragraph._p:
        if (child.tail or '').strip():
            _fail('non-element paragraph text')
        if child.tag == W + 'pPr':
            continue
        if child.tag == W + 'r':
            run(child)
        elif child.tag == W + 'hyperlink':
            flush()
            _hyperlink(child, part)
            mode = 'one-whole-fragment-hyperlink'
            if any(node.tag != W + 'r' for node in child):
                _fail('hyperlink text structure')
            for node in child:
                run(node)
            flush()
            mode = 'first-textual-run-style'
        elif child.tag in (W + 'bookmarkStart', W + 'bookmarkEnd', W + 'proofErr'):
            if len(child) or (child.text or '').strip():
                _fail('extended nontext paragraph marker')
            # Preserve every fixed bookmark/proofing boundary as a separate
            # text fragment; replacing an earlier run cannot cross this node.
            flush()
            markers.append({'offset': offset, 'kind': 'boundary',
                            'tag': child.tag[len(W):], 'xml_sha256': _node_sha(child)})
        else:
            _fail('unrepresented paragraph content')
    flush()
    value = ''.join(text_parts)
    if value != paragraph.text:
        _fail('rich paragraph text does not match the canonical text API')
    return value, markers, segments


def inspect_document(document, data):
    """Return validated parts and mutable paragraph nodes from this exact snapshot."""
    budget = [0]
    _tree(document.element, budget)
    if str(document.part.partname) != '/word/document.xml':
        _fail('main document part identity')
    records, entries, references, media = [], [], [], {}
    total = 0

    def add(locator, paragraph, part, part_name, *, note=None):
        nonlocal total
        value, markers, segments = _paragraph(paragraph, part, archive, media, note=note)
        total += len(value)
        if len(records) >= MAX_RECORDS or total > 10_000_000:
            raise ValueError('document text budget exceeded')
        ordinal = len(records) + 1
        records.append({'logical_locator': locator, 'raw_text': value, 'text_sha256': _sha(value.encode('utf-8')),
                        'physical_index': None, 'printed_label': None, 'printed_label_verified': False})
        entries.append(ParagraphEntry(locator, paragraph, part_name, markers, segments))
        for marker in markers:
            if marker['kind'] in NOTES:
                references.append((marker['kind'], marker['note_id'], ordinal, marker['offset']))

    with ZipFile(BytesIO(data)) as archive:
        if any(name.casefold().startswith(('_xmlsignatures/', 'word/activex/'))
               or 'vbaproject' in name.casefold() for name in archive.namelist()):
            _fail('signed or active rich package')
        if b'macroenabled' in archive.read('[Content_Types].xml').lower():
            _fail('macro-enabled rich package')
        parts, normal = _notes(document, archive, budget)
        owner = SimpleNamespace(part=document.part)
        for locator, paragraph in _blocks(document.element.body, owner, body=True):
            add(locator, paragraph, document.part, 'word/document.xml')
        identities = [(kind, identity) for kind, identity, _, _ in references]
        if len(set(identities)) != len(identities) or set(identities) != set(normal):
            _fail('missing, duplicate or unreferenced ordinary note')
        for kind, identity, source_record, source_offset in references:
            descriptor = parts[kind]
            note_node = normal[(kind, identity)]
            owner = SimpleNamespace(part=descriptor['part'])
            first = len(entries)
            for blocks, paragraph in _blocks(note_node, owner):
                locator = {'part': descriptor['path'], 'kind': kind, 'note_id': identity, 'block': blocks}
                add(locator, paragraph, descriptor['part'], descriptor['path'], note=(kind, identity))
            note_entries = entries[first:]
            labels = [(index, marker) for index, entry in enumerate(note_entries)
                      for marker in entry.markers if marker['kind'] == 'note-label']
            if (not note_entries or len(labels) != 1 or labels[0][0] != 0
                    or note_entries[0].paragraph.text[:labels[0][1]['offset']].strip()
                    or not any(entry.paragraph.text.strip() for entry in note_entries)):
                _fail('note label placement or empty ordinary note')
    if not any(row['raw_text'].strip() for row in records):
        raise ValueError('empty body preview')
    structure = {
        'notes': [{'kind': kind, 'note_id': identity, 'source_record': ordinal, 'source_offset': offset}
                  for kind, identity, ordinal, offset in references],
        'markers': [{'record': ordinal, **marker} for ordinal, entry in enumerate(entries, 1)
                    for marker in entry.markers],
        'media': [media[path] for path in sorted(media)]}
    return {'records': records, 'entries': entries, 'parts': parts, 'structure': structure}


def extract_rich_document(document, data):
    result = inspect_document(document, data)
    structure = result['structure']
    return {'status': 'review_required', 'records': result['records'],
        'warnings': ['body_and_independent_notes', 'inline_images_preserved_not_transcribed',
                     'logical_locator_not_page', 'layout_not_verified', 'note_labels_not_quote_text'],
        'provenance': {'asset_sha256': _sha(data), 'engine': 'python-docx', 'version': '1.2.0',
            'source_profile': SOURCE_PROFILE,
            'structure_sha256': _sha(json.dumps(structure, ensure_ascii=False,
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8'))},
        'rich_structure': structure}
