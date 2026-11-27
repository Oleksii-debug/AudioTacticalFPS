"""Bounded, static XML/ZIP text previews for additional document formats.

The existing ZIP validator owns container safety.  defusedxml supplies the XML
parser; this module only selects the documented text structures.  No archive is
extracted, URL fetched, script/formula run or printed page inferred.  The result
is an explicitly incomplete, unverified logical text preview, like DOCX/EPUB.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import math
import posixpath
import re
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from defusedxml.ElementTree import DefusedXMLParser
from defusedxml.common import DefusedXmlException

from carr_library.catalog.office_archive import validate_office


OFFICE = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
TEXT = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
TABLE = '{urn:oasis:names:tc:opendocument:xmlns:table:1.0}'
DRAW = '{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}'
PRESENTATION = '{urn:oasis:names:tc:opendocument:xmlns:presentation:1.0}'
MANIFEST = '{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}'
STYLE = '{urn:oasis:names:tc:opendocument:xmlns:style:1.0}'
FO = '{urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0}'
REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
CT = '{http://schemas.openxmlformats.org/package/2006/content-types}'
P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
X = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
FB2 = '{http://www.gribuser.ru/xml/fictionbook/2.0}'
FB3 = '{http://www.fictionbook.org/FictionBook3/body}'
XML = '{http://www.w3.org/XML/1998/namespace}'

SUPPORTED_SUFFIXES = frozenset(('.odt', '.ods', '.odp', '.pptx', '.xlsx', '.fb2', '.fb3'))
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_XML_TOTAL = 48 * 1024 * 1024
MAX_NODES = 500_000
MAX_DEPTH = 96
MAX_RECORDS = 10_000
MAX_REPEAT = 10_000
MAX_SHEETS = 1000
_CELL = re.compile(r'([A-Z]{1,3})([1-9][0-9]{0,6})\Z')
_BAD_PERCENT = re.compile(r'%(?![0-9a-fA-F]{2})')


@dataclass
class _Budget:
    max_chars: int
    cancelled: object = None
    deadline: float | None = None
    nodes: int = 0
    xml_bytes: int = 0
    chars: int = 0
    records: list = field(default_factory=list)
    warnings: set = field(default_factory=lambda: {
        'logical_locator_not_page', 'layout_not_verified', 'static_text_preview',
    })

    def check(self):
        if self.cancelled is not None and self.cancelled():
            raise InterruptedError('Document extraction cancelled.')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError('Document extraction deadline exceeded.')

    def add(self, text, locator):
        self.check()
        if not isinstance(text, str):
            raise ValueError('Document text is invalid.')
        if not text.strip():
            return
        self.chars += len(text)
        if self.chars > self.max_chars or len(self.records) >= MAX_RECORDS:
            raise ValueError('Document text budget exceeded.')
        self.records.append({
            'logical_locator': locator, 'raw_text': text,
            'text_sha256': sha256(text.encode('utf-8')).hexdigest(),
            'physical_index': None, 'printed_label': None,
            'printed_label_verified': False,
        })


class _Builder(ET.TreeBuilder):
    def __init__(self, budget):
        super().__init__()
        self.budget, self.depth = budget, 0

    def start(self, tag, attrs):
        self.budget.check()
        self.depth += 1
        self.budget.nodes += 1
        if self.depth > MAX_DEPTH or self.budget.nodes > MAX_NODES or len(attrs) > 128:
            raise ValueError('Document XML structure budget exceeded.')
        if XML + 'base' in attrs:
            raise ValueError('Document XML base references are unsupported.')
        return super().start(tag, attrs)

    def end(self, tag):
        result = super().end(tag)
        self.depth -= 1
        return result


def _xml(data, budget):
    budget.check()
    budget.xml_bytes += len(data)
    if not 0 < len(data) <= MAX_XML_BYTES or budget.xml_bytes > MAX_XML_TOTAL:
        raise ValueError('Document XML size budget exceeded.')
    parser = DefusedXMLParser(target=_Builder(budget), forbid_dtd=True,
                             forbid_entities=True, forbid_external=True)
    try:
        for start in range(0, len(data), 64 * 1024):
            budget.check()
            parser.feed(data[start:start + 64 * 1024])
        return parser.close()
    except (ET.ParseError, DefusedXmlException, UnicodeError):
        raise ValueError('Document XML is invalid or unsupported.') from None


def _member(archive, name, budget):
    budget.check()
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise ValueError('A document resource is missing.') from None
    if info.is_dir() or not 0 < info.file_size <= MAX_XML_BYTES:
        raise ValueError('Document XML size budget exceeded.')
    with archive.open(info) as stream:
        chunks, count = [], 0
        while True:
            budget.check()
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > info.file_size:
                raise ValueError('Document resource size mismatch.')
            chunks.append(chunk)
    if count != info.file_size:
        raise ValueError('Document resource size mismatch.')
    return _xml(b''.join(chunks), budget)


def _one(parent, tag):
    values = parent.findall(tag)
    if len(values) != 1:
        raise ValueError('Document structure is missing or ambiguous.')
    return values[0]


def _leaf_text(element):
    """Simple XML text elements cannot silently discard nested source text."""
    if len(element):
        raise ValueError('Nested content in a document text value requires review.')
    return element.text or ''


def _positive(value, maximum=MAX_REPEAT):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value):
        raise ValueError('Document repeat or coordinate is invalid.')
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError('Document repeat or coordinate budget exceeded.')
    return number


def _target(base, target):
    if (not isinstance(target, str) or not 0 < len(target) <= 4096
            or _BAD_PERCENT.search(target) or '\\' in target
            or any(ord(c) < 32 or ord(c) == 127 for c in target)):
        raise ValueError('Document resource reference is invalid.')
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('Primary document resources must be local.')
    try:
        path = unquote(parsed.path, encoding='utf-8', errors='strict')
    except UnicodeError:
        raise ValueError('Document resource reference is invalid.') from None
    if '\\' in path or ':' in path or any(ord(c) < 32 for c in path):
        raise ValueError('Document resource reference is invalid.')
    parts = [] if path.startswith('/') else posixpath.dirname(base).split('/')
    parts = [part for part in parts if part]
    for part in path.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise ValueError('Document resource escapes the archive.')
            parts.pop()
        else:
            parts.append(part)
    if not parts or path.endswith('/'):
        raise ValueError('Document resource reference is invalid.')
    return '/'.join(parts)


def _relationships(archive, base, budget, *, root=False):
    name = '_rels/.rels' if root else posixpath.join(
        posixpath.dirname(base), '_rels', posixpath.basename(base) + '.rels')
    rels = _member(archive, name, budget)
    if rels.tag != REL + 'Relationships':
        raise ValueError('Document relationships are invalid.')
    result = {}
    for rel in rels:
        budget.check()
        ident, kind, destination = rel.get('Id', ''), rel.get('Type', ''), rel.get('Target', '')
        if rel.tag != REL + 'Relationship' or not ident or len(ident) > 256 or ident in result:
            raise ValueError('Document relationships are invalid or duplicated.')
        if not kind or len(kind) > 4096:
            raise ValueError('Document relationship type is invalid.')
        external = rel.get('TargetMode', 'Internal')
        if external not in ('Internal', 'External'):
            raise ValueError('Document relationship mode is invalid.')
        if external == 'External':
            budget.warnings.add('external_resources_not_loaded')
            result[ident] = (kind, None)
        else:
            result[ident] = (kind, _target(base if not root else '', destination))
    return result


def _opc(archive, primary, budget):
    content = _member(archive, '[Content_Types].xml', budget)
    if content.tag != CT + 'Types':
        raise ValueError('Document content types are invalid.')
    for item in content:
        value = item.get('ContentType', '').lower()
        if 'macroenabled' in value or 'vbaproject' in value or 'activex' in value:
            raise ValueError('Active document content is unsupported.')
    roots = _relationships(archive, '', budget, root=True)
    office = [path for kind, path in roots.values() if kind.endswith('/officeDocument')]
    if primary.startswith(('xl/', 'ppt/')) and office != [primary]:
        raise ValueError('Document primary resource is missing or ambiguous.')
    if primary == 'fb3/body.xml':
        bodies = [path for kind, path in roots.values()
                  if kind == REL[1:-1] + '/fb3-body']
        if bodies != [primary]:
            raise ValueError('FictionBook primary resource is missing or ambiguous.')
    names = archive.namelist()
    if any('vbaproject' in name.lower() or '/activex/' in name.lower() for name in names):
        raise ValueError('Active document content is unsupported.')
    if any('/embeddings/' in name.lower() or '/media/' in name.lower() for name in names):
        budget.warnings.add('embedded_media_not_read')


def _check_odf_visibility(root, budget):
    blocked = {TEXT + name for name in ('tracked-changes', 'change', 'change-start',
        'change-end', 'hidden-text', 'hidden-paragraph', 'conditional-text',
        'script', 'execute-macro')}
    for element in root.iter():
        budget.check()
        if element.tag in blocked or element.tag == OFFICE + 'scripts' and len(element):
            raise ValueError('Conditional, changed or active document text requires review.')
        if (element.get(TEXT + 'display') in ('none', 'false')
                or element.get(FO + 'visibility') in ('hidden', 'collapse')
                or element.get(STYLE + 'display') == 'false'):
            raise ValueError('Hidden document text requires review.')


def _odf_text(element, budget):
    parts = [element.text or '']
    total = len(parts[0])
    allowed = {TEXT + name for name in ('span', 'a', 's', 'tab', 'line-break',
        'bookmark', 'bookmark-start', 'bookmark-end', 'reference-mark',
        'reference-mark-start', 'reference-mark-end', 'soft-page-break')}
    for child in element:
        budget.check()
        before = len(parts)
        if child.tag in (TEXT + 's', TEXT + 'tab', TEXT + 'line-break'):
            if len(child) or child.text:
                raise ValueError('Document text separators contain unsupported content.')
        if child.tag == TEXT + 's':
            parts.append(' ' * _positive(child.get(TEXT + 'c', '1')))
        elif child.tag == TEXT + 'tab':
            parts.append('\t')
        elif child.tag == TEXT + 'line-break':
            parts.append('\n')
        elif child.tag in (TEXT + 'note', OFFICE + 'annotation', DRAW + 'frame'):
            budget.warnings.add('inline_notes_images_not_read')
        elif child.tag in allowed:
            parts.append(_odf_text(child, budget))
        else:
            # Fields, formulae, embedded equations and tracked changes are not
            # silently shortened into an apparently complete paragraph.
            raise ValueError('Unsupported inline document content requires review.')
        parts.append(child.tail or '')
        total += sum(len(part) for part in parts[before:])
        if total > budget.max_chars:
            raise ValueError('Document text budget exceeded.')
    return ''.join(parts)


def _odf_blocks(element, budget, locator, path=()):
    budget.check()
    if element.tag == OFFICE + 'forms':
        # LibreOffice writes this empty container for ordinary binary Word
        # imports. Populated controls and event listeners remain unsupported.
        form = '{urn:oasis:names:tc:opendocument:xmlns:form:1.0}'
        if (len(element) or (element.text or '').strip()
                or set(element.attrib) - {form + 'automatic-focus', form + 'apply-design-mode'}
                or any(value not in ('true', 'false') for value in element.attrib.values())):
            raise ValueError('Active or populated document forms require review.')
        return
    if element.tag == DRAW + 'enhanced-geometry':
        # Geometry equations describe vector shapes; they are not reading
        # text. Accept only this documented inert subtree, never arbitrary
        # descendants that could silently hide an unsupported text object.
        if ((element.text or '').strip()
                or any(child.tag not in (DRAW + 'equation', DRAW + 'handle')
                       or len(child) or (child.text or '').strip()
                       or (child.tail or '').strip() for child in element)):
            raise ValueError('Unsupported vector shape content requires review.')
        budget.warnings.add('vector_geometry_not_read')
        return
    if element.tag in (TEXT + 'p', TEXT + 'h'):
        budget.add(_odf_text(element, budget), {**locator, 'path': list(path)})
        return
    if element.tag in (OFFICE + 'annotation', TEXT + 'note', PRESENTATION + 'notes'):
        budget.warnings.add('notes_not_read')
        return
    if element.tag in (DRAW + 'image', DRAW + 'object', DRAW + 'object-ole', DRAW + 'plugin'):
        budget.warnings.add('embedded_media_not_read')
        return
    containers = ({OFFICE + 'text', OFFICE + 'presentation'}
        | {TEXT + name for name in ('section', 'list', 'list-item', 'list-header')}
        | {TABLE + name for name in ('table', 'table-row', 'table-rows',
            'table-header-rows', 'table-row-group', 'table-cell',
            'covered-table-cell', 'table-column', 'table-columns',
            'table-header-columns', 'table-column-group')}
        | {DRAW + name for name in ('page', 'frame', 'text-box', 'g',
            'custom-shape', 'rect', 'ellipse', 'circle', 'caption', 'line',
            'connector', 'measure', 'a')})
    if element.tag in {TEXT + name for name in ('sequence-decls', 'variable-decls',
            'user-field-decls', 'dde-connection-decls')}:
        budget.warnings.add('field_declarations_not_read')
        return
    if element.tag not in containers:
        raise ValueError('Unsupported document body structure requires review.')
    if element.text and element.text.strip():
        raise ValueError('Unexpected direct document body text requires review.')
    if element.tag in (TABLE + 'table-cell', TABLE + 'table-row'):
        if element.get(TABLE + 'formula') is not None:
            budget.warnings.add('formula_cells_not_read')
            return
        if any(element.get(TABLE + attr, '1') != '1' for attr in
               ('number-columns-repeated', 'number-rows-repeated',
                'number-columns-spanned', 'number-rows-spanned')):
            raise ValueError('Repeated or merged text tables require review.')
    for index, child in enumerate(element, 1):
        _odf_blocks(child, budget, locator, (*path, index))
        if child.tail and child.tail.strip():
            raise ValueError('Unexpected direct document body text requires review.')


def _odf_rows(table, budget):
    for element in table:
        budget.check()
        if element.tag == TABLE + 'table-row':
            yield element
        elif element.tag in {TABLE + x for x in ('table-row-group', 'table-header-rows', 'table-rows')}:
            if element.get(TABLE + 'display') == 'false':
                # Skipping a group without counting every repeated descendant
                # would invent the subsequent logical row coordinates.
                raise ValueError('Hidden spreadsheet row groups require review.')
            yield from _odf_rows(element, budget)


def _ods(body, budget):
    sheets = body.findall(TABLE + 'table')
    if len(sheets) > MAX_SHEETS:
        raise ValueError('Document sheet count budget exceeded.')
    for si, sheet in enumerate(sheets, 1):
        budget.check()
        if sheet.get(TABLE + 'display') == 'false':
            budget.warnings.add('hidden_sheets_not_read')
            continue
        # Column visibility can be inherited through styles. Do not guess it.
        if any(e.get(TABLE + 'visibility', 'visible') != 'visible'
               for e in sheet.iter(TABLE + 'table-column')):
            raise ValueError('Hidden spreadsheet columns require review.')
        if any(e.get(TABLE + 'display') == 'false'
               for e in sheet.iter(TABLE + 'table-column-group')):
            raise ValueError('Hidden spreadsheet column groups require review.')
        if any(e.get(TABLE + key) is not None for e in sheet.iter()
               for key in ('number-matrix-columns-spanned', 'number-matrix-rows-spanned')):
            # Dependent matrix cells can store values without their own formula
            # marker. Never mislabel those cached values as literal source cells.
            raise ValueError('Matrix spreadsheet formula ranges require review.')
        row_index = 1
        for row in _odf_rows(sheet, budget):
            repeated_rows = _positive(row.get(TABLE + 'number-rows-repeated', '1'), 1_048_576)
            cells, column = [], 1
            hidden = row.get(TABLE + 'visibility', 'visible') != 'visible'
            for cell in row:
                budget.check()
                if cell.tag not in (TABLE + 'table-cell', TABLE + 'covered-table-cell'):
                    continue
                repeat = _positive(cell.get(TABLE + 'number-columns-repeated', '1'), 16384)
                if column + repeat - 1 > 16384:
                    raise ValueError('Spreadsheet coordinate budget exceeded.')
                if cell.tag == TABLE + 'covered-table-cell':
                    column += repeat
                    continue
                if cell.get(TABLE + 'formula') is not None:
                    budget.warnings.add('formula_cells_not_read')
                elif not hidden:
                    if cell.get(TABLE + 'number-columns-spanned', '1') != '1' or cell.get(TABLE + 'number-rows-spanned', '1') != '1':
                        budget.warnings.add('merged_cells_anchor_only')
                    paragraphs = [_odf_text(p, budget) for p in cell if p.tag in (TEXT + 'p', TEXT + 'h')]
                    if not paragraphs:
                        for key in ('string-value', 'value', 'date-value', 'time-value', 'boolean-value'):
                            if cell.get(OFFICE + key) is not None:
                                paragraphs = [cell.get(OFFICE + key)]
                                budget.warnings.add('stored_cell_values_not_formatted')
                                break
                    if any(value.strip() for value in paragraphs):
                        if repeat * repeated_rows > MAX_RECORDS:
                            raise ValueError('Document repeated text budget exceeded.')
                        cells.append((column, repeat, paragraphs))
                column += repeat
            if row_index + repeated_rows - 1 > 1_048_576:
                raise ValueError('Spreadsheet coordinate budget exceeded.')
            if hidden:
                budget.warnings.add('hidden_rows_not_read')
            elif cells:
                for r in range(row_index, row_index + repeated_rows):
                    for col, repeat, paragraphs in cells:
                        for c in range(col, col + repeat):
                            for pi, value in enumerate(paragraphs, 1):
                                budget.add(value, {'sheet': si, 'sheet_name': sheet.get(TABLE + 'name', ''),
                                    'row': r, 'column': c, 'paragraph': pi})
            row_index += repeated_rows


def _odf(archive, suffix, budget):
    manifest = _member(archive, 'META-INF/manifest.xml', budget)
    if manifest.tag != MANIFEST + 'manifest':
        raise ValueError('OpenDocument manifest is invalid.')
    if any(e.tag == MANIFEST + 'encryption-data' for e in manifest.iter()):
        raise ValueError('Encrypted documents are unsupported.')
    if any(name.lower().startswith(('basic/', 'scripts/')) for name in archive.namelist()):
        raise ValueError('Active document content is unsupported.')
    root = _member(archive, 'content.xml', budget)
    if root.tag != OFFICE + 'document-content':
        raise ValueError('OpenDocument content namespace is unsupported.')
    _check_odf_visibility(root, budget)
    if 'styles.xml' in archive.namelist():
        styles = _member(archive, 'styles.xml', budget)
        _check_odf_visibility(styles, budget)
        if any(e.get(TABLE + 'display') == 'false' or
               e.get(TABLE + 'visibility', 'visible') != 'visible'
               for e in styles.iter()):
            raise ValueError('Inherited spreadsheet visibility requires review.')
    if any(e.get(TABLE + 'display') == 'false' or
           e.get(TABLE + 'visibility', 'visible') != 'visible'
           for styles in root.findall(OFFICE + 'automatic-styles') for e in styles.iter()):
        raise ValueError('Inherited spreadsheet visibility requires review.')
    body = _one(root, OFFICE + 'body')
    kind = {'.odt': 'text', '.ods': 'spreadsheet', '.odp': 'presentation'}[suffix]
    content = _one(body, OFFICE + kind)
    if suffix == '.ods':
        _ods(content, budget)
    elif suffix == '.odp':
        slides = content.findall(DRAW + 'page')
        if len(slides) > MAX_SHEETS:
            raise ValueError('Document slide count budget exceeded.')
        for si, slide in enumerate(slides, 1):
            if slide.get(PRESENTATION + 'visibility') == 'hidden':
                budget.warnings.add('hidden_slides_not_read')
                continue
            _odf_blocks(slide, budget, {'slide': si, 'slide_name': slide.get(DRAW + 'name', '')})
        budget.warnings.add('slide_shape_order_not_visual_order')
    else:
        _odf_blocks(content, budget, {'part': 'content.xml'})
    budget.warnings.add('headers_footers_styles_not_rendered')


def _drawing_text(paragraph, budget):
    parts = []
    for item in paragraph:
        budget.check()
        if item.tag == A + 'r':
            if any(child.tag not in (A + 'rPr', A + 't') for child in item):
                raise ValueError('Unsupported presentation text requires review.')
            text = _one(item, A + 't')
            parts.append(_leaf_text(text))
        elif item.tag == A + 'br':
            parts.append('\n')
        elif item.tag in (A + 'pPr', A + 'endParaRPr'):
            continue
        else:
            raise ValueError('Unsupported presentation text requires review.')
    return ''.join(parts)


def _pptx(archive, budget):
    primary = 'ppt/presentation.xml'
    _opc(archive, primary, budget)
    root = _member(archive, primary, budget)
    if root.tag != P + 'presentation':
        raise ValueError('Presentation namespace is unsupported.')
    order = _one(root, P + 'sldIdLst')
    if len(order) > MAX_SHEETS:
        raise ValueError('Document slide count budget exceeded.')
    rels = _relationships(archive, primary, budget)
    seen = set()
    for si, item in enumerate(order, 1):
        ident = item.get(R + 'id')
        if item.tag != P + 'sldId' or ident not in rels or ident in seen:
            raise ValueError('Presentation slide order is invalid.')
        seen.add(ident)
        kind, name = rels[ident]
        if kind != R[1:-1] + '/slide' or name is None:
            raise ValueError('Presentation slide relationship is unsupported.')
        slide = _member(archive, name, budget)
        if slide.tag != P + 'sld':
            raise ValueError('Presentation slide namespace is unsupported.')
        if slide.get('show', '1') in ('0', 'false'):
            budget.warnings.add('hidden_slides_not_read')
            continue
        for element in slide.iter():
            budget.check()
            if element.tag == MC + 'AlternateContent' or element.tag == A + 'fld':
                raise ValueError('Conditional or field presentation text requires review.')
            if element.get('hidden') in ('1', 'true'):
                raise ValueError('Hidden presentation shapes require review.')
        body = _one(_one(slide, P + 'cSld'), P + 'spTree')
        for pi, paragraph in enumerate(body.iter(A + 'p'), 1):
            budget.add(_drawing_text(paragraph, budget),
                       {'slide': si, 'part': name, 'paragraph': pi})
    budget.warnings.update(('slide_shape_order_not_visual_order',
                            'speaker_notes_master_slides_not_read', 'embedded_media_not_read'))


def _shared_text(parent, budget):
    parts = []
    for child in parent:
        budget.check()
        if child.tag == X + 't':
            parts.append(_leaf_text(child))
        elif child.tag == X + 'r':
            if any(item.tag not in (X + 'rPr', X + 't') for item in child):
                raise ValueError('Spreadsheet text structure is unsupported.')
            parts.append(_leaf_text(_one(child, X + 't')))
        elif child.tag in (X + 'rPh', X + 'phoneticPr'):
            budget.warnings.add('phonetic_annotations_not_read')
        else:
            raise ValueError('Spreadsheet text structure is unsupported.')
    return ''.join(parts)


def _cell_coordinate(ref):
    match = _CELL.fullmatch(ref)
    if match is None:
        raise ValueError('Spreadsheet coordinate is invalid.')
    col = 0
    for char in match[1]:
        col = col * 26 + ord(char) - 64
    row = int(match[2])
    if col > 16384 or row > 1_048_576:
        raise ValueError('Spreadsheet coordinate budget exceeded.')
    return row, col


def _xlsx(archive, budget):
    primary = 'xl/workbook.xml'
    _opc(archive, primary, budget)
    root = _member(archive, primary, budget)
    if root.tag != X + 'workbook':
        raise ValueError('Spreadsheet namespace is unsupported.')
    rels = _relationships(archive, primary, budget)
    style_parts = [name for kind, name in rels.values() if kind == R[1:-1] + '/styles']
    if len(style_parts) > 1 or style_parts == [None]:
        raise ValueError('Spreadsheet styles are ambiguous.')
    if style_parts:
        styles = _member(archive, style_parts[0], budget)
        if styles.tag != X + 'styleSheet':
            raise ValueError('Spreadsheet styles namespace is unsupported.')
        # A custom number format can hide values (for example ;;;).  We do not
        # implement Excel's locale/conditional formatting language here.
        if styles.find(X + 'numFmts/' + X + 'numFmt') is not None:
            raise ValueError('Custom spreadsheet value formatting requires review.')
    strings = []
    string_parts = [name for kind, name in rels.values() if kind == R[1:-1] + '/sharedStrings']
    if len(string_parts) > 1 or string_parts == [None]:
        raise ValueError('Spreadsheet shared strings are ambiguous.')
    if string_parts:
        sst = _member(archive, string_parts[0], budget)
        if sst.tag != X + 'sst':
            raise ValueError('Spreadsheet shared strings are invalid.')
        total = 0
        for child in sst:
            if child.tag != X + 'si':
                raise ValueError('Spreadsheet shared strings are invalid.')
            value = _shared_text(child, budget)
            total += len(value)
            if total > budget.max_chars or len(strings) >= MAX_NODES:
                raise ValueError('Spreadsheet shared strings budget exceeded.')
            strings.append(value)
    sheets = _one(root, X + 'sheets')
    if len(sheets) > MAX_SHEETS:
        raise ValueError('Document sheet count budget exceeded.')
    seen = set()
    for si, sheet in enumerate(sheets, 1):
        budget.check()
        ident = sheet.get(R + 'id')
        if sheet.tag != X + 'sheet' or ident not in rels or ident in seen:
            raise ValueError('Spreadsheet sheet order is invalid.')
        seen.add(ident)
        state = sheet.get('state', 'visible')
        if state in ('hidden', 'veryHidden'):
            budget.warnings.add('hidden_sheets_not_read')
            continue
        if state != 'visible':
            raise ValueError('Spreadsheet visibility state is unsupported.')
        kind, name = rels[ident]
        if kind != R[1:-1] + '/worksheet' or name is None:
            raise ValueError('Spreadsheet sheet relationship is unsupported.')
        ws = _member(archive, name, budget)
        if ws.tag != X + 'worksheet':
            raise ValueError('Spreadsheet sheet namespace is unsupported.')
        if any(e.tag == MC + 'AlternateContent' for e in ws.iter()):
            raise ValueError('Conditional spreadsheet content requires review.')
        if any(e.get('t', 'normal') != 'normal' or e.get('ref') is not None
               for e in ws.iter(X + 'f')):
            # Array/shared/data-table range results may have no local f child.
            # This adapter omits plain formula cells only; ranged calculation
            # semantics need a separately reviewed full spreadsheet renderer.
            raise ValueError('Ranged spreadsheet formulas require review.')
        if ws.find(X + 'conditionalFormatting') is not None:
            raise ValueError('Conditional spreadsheet formatting requires review.')
        defaults = ws.find(X + 'sheetFormatPr')
        if defaults is not None and defaults.get('zeroHeight') in ('1', 'true'):
            raise ValueError('Default hidden spreadsheet rows require review.')
        hidden_cols = []
        for col in ws.findall(X + 'cols/' + X + 'col'):
            if col.get('hidden') in ('1', 'true'):
                lo, hi = _positive(col.get('min', ''), 16384), _positive(col.get('max', ''), 16384)
                if lo > hi:
                    raise ValueError('Spreadsheet column range is invalid.')
                hidden_cols.append((lo, hi))
        previous_row = 0
        for row in _one(ws, X + 'sheetData'):
            budget.check()
            if row.tag != X + 'row':
                raise ValueError('Spreadsheet row structure is unsupported.')
            ri = _positive(row.get('r', ''), 1_048_576)
            if ri <= previous_row:
                raise ValueError('Spreadsheet row order is invalid.')
            previous_row = ri
            if row.get('hidden') in ('1', 'true'):
                budget.warnings.add('hidden_rows_not_read')
                continue
            previous_col = 0
            for cell in row:
                budget.check()
                if cell.tag != X + 'c':
                    raise ValueError('Spreadsheet cell structure is unsupported.')
                ref = cell.get('r', '')
                cr, cc = _cell_coordinate(ref)
                if cr != ri or cc <= previous_col:
                    raise ValueError('Spreadsheet cell order is invalid.')
                previous_col = cc
                if any(lo <= cc <= hi for lo, hi in hidden_cols):
                    budget.warnings.add('hidden_columns_not_read')
                    continue
                if cell.find(X + 'f') is not None:
                    budget.warnings.add('formula_cells_not_read')
                    continue
                data_type = cell.get('t', 'n')
                if data_type == 'inlineStr':
                    value = _shared_text(_one(cell, X + 'is'), budget)
                else:
                    values = cell.findall(X + 'v')
                    if not values:
                        continue
                    if len(values) != 1:
                        raise ValueError('Spreadsheet cell value is ambiguous.')
                    value = _leaf_text(values[0])
                    if data_type == 's':
                        if not re.fullmatch(r'[0-9]{1,9}', value) or int(value) >= len(strings):
                            raise ValueError('Spreadsheet shared-string reference is invalid.')
                        value = strings[int(value)]
                    elif data_type not in ('n', 'b', 'd', 'e', 'str'):
                        raise ValueError('Spreadsheet cell type is unsupported.')
                budget.add(value, {'sheet': si, 'sheet_name': sheet.get('name', ''),
                                   'cell': ref, 'part': name})
    budget.warnings.update(('stored_cell_values_not_formatted',
                           'charts_images_comments_not_read', 'formula_cells_not_evaluated'))


def _fiction_inline(element, ns, budget):
    allowed = {'strong', 'emphasis', 'em', 'strikethrough', 'sub', 'sup', 'code',
               'underline', 'spacing', 'span', 'smallcaps', 'a', 'style'}
    parts = [element.text or '']
    for child in element:
        budget.check()
        name = child.tag[len(ns):] if child.tag.startswith(ns) else ''
        if name in allowed:
            parts.append(_fiction_inline(child, ns, budget))
        elif name in ('image', 'img', 'note'):
            budget.warnings.add('inline_notes_images_not_read')
        elif name == 'paper-page-break':
            budget.warnings.add('unverified_print_labels_not_used')
        else:
            raise ValueError('Unsupported FictionBook inline content requires review.')
        parts.append(child.tail or '')
    return ''.join(parts)


def _fiction_blocks(element, ns, budget, locator, path=()):
    budget.check()
    name = element.tag[len(ns):] if element.tag.startswith(ns) else ''
    if name in ('p', 'v', 'subtitle', 'text-author', 'date', 'li'):
        budget.add(_fiction_inline(element, ns, budget), {**locator, 'path': list(path)})
        return
    if name in ('image', 'img', 'binary'):
        budget.warnings.add('embedded_media_not_read')
        return
    if name in ('empty-line', 'br', 'paper-page-break'):
        if name == 'paper-page-break':
            budget.warnings.add('unverified_print_labels_not_used')
        return
    if name in ('clipped',) or element.get('clipped') in ('1', 'true'):
        raise ValueError('Incomplete FictionBook fragments require review.')
    containers = {'body', 'fb3-body', 'section', 'title', 'epigraph', 'poem',
                  'stanza', 'cite', 'annotation', 'table', 'tr', 'td', 'th',
                  'subscription', 'blockquote', 'pre', 'div', 'ol', 'ul',
                  'notes', 'notebody'}
    if name not in containers:
        raise ValueError('Unsupported FictionBook body structure requires review.')
    if name in ('td', 'th') and element.text and element.text.strip():
        budget.add(_fiction_inline(element, ns, budget), {**locator, 'path': list(path)})
        return
    if element.text and element.text.strip():
        raise ValueError('Unexpected direct FictionBook body text requires review.')
    for index, child in enumerate(element, 1):
        _fiction_blocks(child, ns, budget, locator, (*path, index))
        if child.tail and child.tail.strip():
            raise ValueError('Unexpected direct FictionBook body text requires review.')


def _fb2(data, budget):
    root = _xml(data, budget)
    if root.tag != FB2 + 'FictionBook':
        raise ValueError('FictionBook namespace is unsupported.')
    if root.text and root.text.strip() or any(child.tail and child.tail.strip() for child in root):
        raise ValueError('Unexpected direct FictionBook body text requires review.')
    bodies = root.findall(FB2 + 'body')
    if not bodies:
        raise ValueError('FictionBook body is missing.')
    for bi, body in enumerate(bodies, 1):
        _fiction_blocks(body, FB2, budget, {'body': bi, 'name': body.get('name', '')})
    budget.warnings.add('metadata_and_images_not_read')


def _fb3(archive, budget):
    _opc(archive, 'fb3/body.xml', budget)
    body = _member(archive, 'fb3/body.xml', budget)
    if body.tag != FB3 + 'fb3-body':
        raise ValueError('FictionBook namespace is unsupported.')
    _fiction_blocks(body, FB3, budget, {'part': 'fb3/body.xml'})
    budget.warnings.add('metadata_and_images_not_read')


def extract_xml_document(data: bytes, suffix: str, *, max_bytes=64 * 1024 * 1024,
                         max_chars=10_000_000, cancelled=None, deadline=None):
    """Extract explicitly unverified logical records from supported subsets.

    ``deadline`` is an absolute monotonic time.  Format/XML/security ceilings
    remain bounded even when a caller sets a larger input budget.  Presentation
    order follows the authored slide list, then the stored shape order.  Sheets
    follow workbook order and literal stored cell values; formula cells and
    explicitly hidden rows/columns/sheets are omitted with fixed warnings.
    """
    if (type(data) is not bytes or type(max_bytes) is not int or max_bytes <= 0
            or not 0 < len(data) <= min(max_bytes, 64 * 1024 * 1024)
            or type(max_chars) is not int or not 0 < max_chars <= 10_000_000
            or not isinstance(suffix, str) or suffix not in SUPPORTED_SUFFIXES
            or cancelled is not None and not callable(cancelled)
            or deadline is not None and (type(deadline) not in (int, float)
                or not math.isfinite(deadline))):
        raise ValueError('Invalid document snapshot or extraction budget.')
    budget = _Budget(max_chars=max_chars, cancelled=cancelled, deadline=deadline)
    budget.check()
    if suffix == '.fb2':
        _fb2(data, budget)
    else:
        validate_office(data, suffix)
        budget.check()
        with ZipFile(BytesIO(data)) as archive:
            if suffix in ('.odt', '.ods', '.odp'):
                _odf(archive, suffix, budget)
            elif suffix == '.pptx':
                _pptx(archive, budget)
            elif suffix == '.xlsx':
                _xlsx(archive, budget)
            else:
                _fb3(archive, budget)
    budget.check()
    if not budget.records:
        raise ValueError('Document contains no supported visible text.')
    return {'status': 'review_required', 'records': budget.records,
            'warnings': sorted(budget.warnings),
            'provenance': {'asset_sha256': sha256(data).hexdigest(),
                           'engine': 'defusedxml-static-structure', 'version': '1',
                           'format': suffix.removeprefix('.')}}
