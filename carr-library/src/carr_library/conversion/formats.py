"""Bounded document readers. Active content, links and formulas are not run."""
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

from .models import ConversionBudget, ConversionError


ODF = {
    'office': 'urn:oasis:names:tc:opendocument:xmlns:office:1.0',
    'text': 'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
    'table': 'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
    'draw': 'urn:oasis:names:tc:opendocument:xmlns:drawing:1.0',
}
X = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
FB2 = 'http://www.gribuser.ru/xml/fictionbook/2.0'
FB3 = 'http://www.fictionbook.org/FictionBook3/body'


def preflight_archive(data, budget):
    # Reuse the reviewed central-record scanner before ZipFile/Office readers
    # allocate an untrusted inventory. Commented/multi-disk containers remain
    # explicitly unsupported; no custom extraction or archive writer is added.
    from ..audio.backup import _preflight_archive
    try:
        _preflight_archive(BytesIO(data), len(data), file_budget=budget.max_members - 1,
                           cancelled=lambda: False)
    except ValueError as exc:
        raise ConversionError('Document archive exceeds its directory budget or uses an unsupported ZIP variant.') from exc


def q(namespace, name):
    return '{' + namespace + '}' + name


class TextOutput:
    def __init__(self, budget):
        self.budget = budget
        self.parts, self.sections, self.warnings = [], [], []
        self.size = 0

    def add(self, value, label, *, physical_index=None):
        if not isinstance(value, str) or '\x00' in value:
            raise ConversionError('Extracted text contains invalid characters.')
        raw = value.encode('utf-8', errors='strict')
        self.size += len(raw) + (2 if self.parts else 0)
        if self.size > self.budget.max_text_bytes or len(self.sections) >= self.budget.max_sections:
            raise ConversionError('Extracted text exceeds its selected output budget.')
        self.parts.append(value)
        self.sections.append({'label': str(label)[:512], 'physical_index': physical_index,
                              'printed_label': None, 'text_sha256': sha256(raw).hexdigest()})

    def result(self, engine, metadata=None):
        value = '\n\n'.join(self.parts)
        if not value.strip():
            raise ConversionError('No readable text was found; a scanned document may require OCR.')
        return {'text': value, 'engine': engine, 'sections': self.sections,
                'warnings': list(dict.fromkeys(self.warnings)), 'metadata': metadata or {}}


class LimitedTreeBuilder(ET.TreeBuilder):
    def __init__(self, budget):
        super().__init__()
        self.count = self.depth = 0
        self.budget = budget

    def start(self, tag, attributes):
        self.count += 1
        self.depth += 1
        if self.count > self.budget.max_xml_nodes or self.depth > 128 or len(attributes) > 256:
            raise ConversionError('Document XML exceeds the structure budget.')
        return super().start(tag, attributes)

    def end(self, tag):
        result = super().end(tag)
        self.depth -= 1
        return result


def xml(data, budget):
    from defusedxml.ElementTree import DefusedXMLParser
    if len(data) > budget.max_member_bytes:
        raise ConversionError('Document XML exceeds its byte budget.')
    parser = DefusedXMLParser(target=LimitedTreeBuilder(budget), forbid_dtd=True,
                             forbid_entities=True, forbid_external=True)
    try:
        return ET.fromstring(data, parser=parser)
    except Exception as exc:
        raise ConversionError('Document XML is malformed, unsafe or exceeds its budget.') from exc


class Archive:
    def __init__(self, data, budget):
        from ..catalog.office_archive import _name
        self.budget = budget
        preflight_archive(data, budget)
        self.archive = ZipFile(BytesIO(data))
        infos = self.archive.infolist()
        if not infos or len(infos) > budget.max_members:
            raise ConversionError('Document archive member budget exceeded.')
        self.names = {}
        folded = {}
        total = 0
        for info in infos:
            key = _name(info)
            if key in folded or info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise ConversionError('Duplicate, encrypted or unsupported document archive member.')
            folded[key] = info.is_dir()
            total += info.file_size
            if info.file_size > budget.max_member_bytes or total > budget.max_archive_bytes:
                raise ConversionError('Document archive expansion budget exceeded.')
            self.names[info.filename] = info
            lower = info.filename.casefold()
            if ('vbaproject' in lower or lower.startswith(('basic/', 'scripts/'))
                    or lower.endswith(('.js', '.vbs', '.exe', '.dll'))):
                raise ConversionError('Active code in document archives is not supported.')
        for name in folded:
            parts = name.split('/')
            if any(folded.get('/'.join(parts[:i])) is False for i in range(1, len(parts))):
                raise ConversionError('Document archive has conflicting paths.')
        self.consumed = 0

    def read(self, name):
        if name not in self.names or self.names[name].is_dir():
            raise ConversionError('A referenced document part is missing.')
        info = self.names[name]
        with self.archive.open(info) as stream:
            data = stream.read(min(info.file_size, self.budget.max_member_bytes) + 1)
        if len(data) != info.file_size:
            raise ConversionError('Document archive member size mismatch.')
        self.consumed += len(data)
        if self.consumed > self.budget.max_archive_bytes:
            raise ConversionError('Document archive read budget exceeded.')
        return data

    def xml(self, name):
        return xml(self.read(name), self.budget)

    def relationships(self, name, base):
        tree = self.xml(name)
        if tree.tag != q(REL, 'Relationships'):
            raise ConversionError('Invalid document relationships.')
        result = {}
        for rel in tree:
            identity = rel.get('Id')
            if not identity or identity in result:
                raise ConversionError('Duplicate document relationship.')
            if rel.get('TargetMode') == 'External':
                result[identity] = None
                continue
            target = rel.get('Target', '')
            if not target or '\\' in target or ':' in target or '%' in target or '?' in target or '#' in target:
                raise ConversionError('Unsafe document relationship target.')
            if target.startswith('//'):
                raise ConversionError('Network document relationships are not supported.')
            normalized = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base, target))
            if normalized.startswith('/') or normalized == '..' or normalized.startswith('../'):
                raise ConversionError('Document relationship escapes its archive.')
            result[identity] = normalized
        return result


class SafeHTML(HTMLParser):
    blocks = frozenset('p div br hr h1 h2 h3 h4 h5 h6 li ul ol tr table section article blockquote pre'.split())
    ignored = frozenset('script style template iframe object embed svg math head'.split())

    def __init__(self, budget):
        super().__init__(convert_charrefs=True)
        self.budget = budget
        self.parts, self.stack = [], []
        self.size = self.nodes = 0

    def add(self, value):
        self.size += len(value.encode('utf-8'))
        if self.size > self.budget.max_text_bytes:
            raise ConversionError('HTML output exceeds the text budget.')
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > self.budget.max_xml_nodes or len(self.stack) > 128:
            raise ConversionError('HTML structure budget exceeded.')
        if self.stack:
            if tag in self.ignored:
                self.stack.append(tag)
            return
        if tag in self.ignored:
            self.stack.append(tag)
        elif tag in self.blocks:
            self.add('\n')
        elif tag in ('td', 'th'):
            self.add('\t')
        elif tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self.add('[Image description: ' + alt + ']')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.stack:
            if tag == self.stack[-1]:
                self.stack.pop()
        elif tag in self.blocks:
            self.add('\n')

    def handle_data(self, value):
        if not self.stack:
            self.add(value)

    def result(self):
        result = ''.join(self.parts)
        return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)*', '\n\n', result).strip()


def decode(data, encoding=None):
    if encoding is None:
        encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    if encoding.lower().replace('_', '-') not in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be',
                                                 'windows-1251', 'cp1251', 'windows-1252', 'cp1252', 'iso-8859-1', 'ascii'):
        raise ConversionError('Choose a supported explicit text encoding.')
    try:
        return data.decode(encoding, errors='strict')
    except UnicodeError as exc:
        raise ConversionError('Text decoding failed; choose the source encoding instead of replacing characters.') from exc


def html_text(value, budget):
    parser = SafeHTML(budget)
    parser.feed(value)
    parser.close()
    return parser.result()


def _paragraph(element, budget):
    parts = []
    def visit(node):
        if node.tag == q(ODF['text'], 's'):
            repeat = int(node.get(q(ODF['text'], 'c'), '1'))
            if not 1 <= repeat <= 10000:
                raise ConversionError('ODF whitespace expansion budget exceeded.')
            parts.append(' ' * repeat)
        elif node.tag == q(ODF['text'], 'tab'):
            parts.append('\t')
        elif node.tag == q(ODF['text'], 'line-break'):
            parts.append('\n')
        elif node.tag in (q(ODF['text'], 'hidden-text'), q(ODF['text'], 'hidden-paragraph')):
            return
        else:
            parts.append(node.text or '')
            for child in node:
                visit(child)
                parts.append(child.tail or '')
    visit(element)
    value = ''.join(parts)
    if len(value.encode('utf-8')) > budget.max_text_bytes:
        raise ConversionError('Paragraph text budget exceeded.')
    return value


def odf(data, suffix, output):
    archive = Archive(data, output.budget)
    kind = {'.odt': 'text', '.ods': 'spreadsheet', '.odp': 'presentation'}[suffix]
    if archive.read('mimetype') != ('application/vnd.oasis.opendocument.' + kind).encode():
        raise ConversionError('OpenDocument type does not match its file extension.')
    if 'META-INF/manifest.xml' in archive.names:
        manifest = archive.xml('META-INF/manifest.xml')
        if any(node.tag.endswith('}encryption-data') for node in manifest.iter()):
            raise ConversionError('Password-protected documents are not supported.')
    root = archive.xml('content.xml')
    body = root.find(q(ODF['office'], 'body') + '/' + q(ODF['office'], kind))
    if body is None:
        raise ConversionError('OpenDocument body is missing.')
    if any(node.tag == q(ODF['text'], 'tracked-changes') for node in body.iter()):
        raise ConversionError('Tracked changes require independent review before text conversion.')
    if suffix == '.ods':
        for table in body.findall(q(ODF['table'], 'table')):
            name = table.get(q(ODF['table'], 'name'), 'Sheet')
            row_number = 1
            for row in table.iter(q(ODF['table'], 'table-row')):
                repeat = int(row.get(q(ODF['table'], 'number-rows-repeated'), '1'))
                if not 1 <= repeat <= 10000000:
                    raise ConversionError('Invalid repeated spreadsheet row count.')
                cells = []
                for cell in row:
                    if cell.tag not in (q(ODF['table'], 'table-cell'), q(ODF['table'], 'covered-table-cell')):
                        continue
                    text_parts = [_paragraph(p, output.budget) for p in cell.iter(q(ODF['text'], 'p'))]
                    value = '\n'.join(text_parts)
                    if not value:
                        value = cell.get(q(ODF['office'], 'string-value'), cell.get(q(ODF['office'], 'value'), ''))
                    count = int(cell.get(q(ODF['table'], 'number-columns-repeated'), '1'))
                    if not 1 <= count <= 10000000:
                        raise ConversionError('Invalid repeated spreadsheet cell count.')
                    if not value and count > 1000:
                        continue  # Empty padding does not become millions of words.
                    if len(cells) + count > output.budget.max_sections:
                        raise ConversionError('Spreadsheet cell budget exceeded.')
                    cells.extend([value] * count)
                if any(cells):
                    if repeat > output.budget.max_sections:
                        raise ConversionError('Spreadsheet row expansion budget exceeded.')
                    for offset in range(repeat):
                        output.add('\t'.join(cells).rstrip('\t'), f'{name}, row {row_number + offset}')
                row_number += repeat
        output.warnings.append('Spreadsheet formulas were not evaluated; stored values and text may be stale. Formatting and merged-cell layout are not preserved.')
    else:
        if suffix == '.odp':
            containers = list(body.iter(q(ODF['draw'], 'page')))
        else:
            containers = [body]
        for ordinal, container in enumerate(containers, 1):
            for index, node in enumerate(container.iter(), 1):
                if node.tag in (q(ODF['text'], 'p'), q(ODF['text'], 'h')):
                    output.add(_paragraph(node, output.budget), f'{kind} {ordinal}, paragraph {index}')
    output.warnings.append('Logical document order only; printed pages, visual order and embedded objects were not verified.')
    return output.result('safe-opendocument-xml/1')


def xlsx(data, output):
    archive = Archive(data, output.budget)
    workbook = archive.xml('xl/workbook.xml')
    relationships = archive.relationships('xl/_rels/workbook.xml.rels', 'xl')
    shared = []
    if 'xl/sharedStrings.xml' in archive.names:
        shared = [''.join(node.itertext()) for node in archive.xml('xl/sharedStrings.xml') if node.tag == q(X, 'si')]
    count = 0
    for sheet in workbook.findall(q(X, 'sheets') + '/' + q(X, 'sheet')):
        if sheet.get('state', 'visible') != 'visible':
            output.warnings.append('Hidden sheets were excluded.')
            continue
        target = relationships.get(sheet.get(q(R, 'id')))
        if not target or not target.startswith('xl/worksheets/'):
            raise ConversionError('Spreadsheet sheet relationship is missing or external.')
        root = archive.xml(target)
        for row in root.findall(q(X, 'sheetData') + '/' + q(X, 'row')):
            if row.get('hidden') in ('1', 'true'):
                output.warnings.append('Hidden rows were excluded.')
                continue
            cells = []
            for cell in row.findall(q(X, 'c')):
                count += 1
                if count > output.budget.max_sections:
                    raise ConversionError('Spreadsheet cell budget exceeded.')
                kind = cell.get('t', 'n')
                node = cell.find(q(X, 'v'))
                value = node.text or '' if node is not None else ''
                if kind == 's':
                    try:
                        if not value.isdecimal():
                            raise ValueError('invalid shared string index')
                        value = shared[int(value)]
                    except (ValueError, IndexError):
                        raise ConversionError('Spreadsheet shared-string reference is invalid.') from None
                elif kind == 'inlineStr':
                    value = ''.join(cell.find(q(X, 'is')).itertext()) if cell.find(q(X, 'is')) is not None else ''
                if cell.find(q(X, 'f')) is not None and not value:
                    value = '[Formula without a stored result]'
                cells.append(cell.get('r', '?') + ': ' + value)
            if cells:
                output.add('\n'.join(cells), f'{sheet.get("name", "Sheet")}, row {row.get("r", "?")}')
    output.warnings.extend(['Stored spreadsheet values only; formulas, macros, external links and recalculation were not executed.',
                            'Number/date formatting, charts and hidden column layout are not reproduced.'])
    return output.result('safe-spreadsheet-xml/1')


def pptx(data, output):
    archive = Archive(data, output.budget)
    root = archive.xml('ppt/presentation.xml')
    relationships = archive.relationships('ppt/_rels/presentation.xml.rels', 'ppt')
    slides = root.findall(q(P, 'sldIdLst') + '/' + q(P, 'sldId'))
    if len(slides) > output.budget.max_pages:
        raise ConversionError('Presentation slide budget exceeded.')
    for index, reference in enumerate(slides, 1):
        target = relationships.get(reference.get(q(R, 'id')))
        if not target or not target.startswith('ppt/slides/'):
            raise ConversionError('Presentation slide relationship is missing or external.')
        slide = archive.xml(target)
        if slide.get('show') in ('0', 'false'):
            output.warnings.append('Hidden slides were excluded.')
            continue
        paragraphs = []
        for node in slide.iter(q(A, 'p')):
            paragraphs.append(''.join(x.text or '' for x in node.iter(q(A, 't'))))
        output.add('\n'.join(paragraphs), f'Slide {index}')
    output.warnings.append('Slide XML order only; animations, notes, images and visual reading order were not rendered or verified.')
    return output.result('safe-presentation-xml/1')


def fictionbook(data, suffix, output):
    if suffix == '.fb2':
        root = xml(data, output.budget)
        if root.tag != q(FB2, 'FictionBook'):
            raise ConversionError('Invalid FictionBook 2 root.')
        bodies = root.findall(q(FB2, 'body'))
        namespace = FB2
    else:
        archive = Archive(data, output.budget)
        content = archive.xml('[Content_Types].xml')
        paths = [x.get('PartName', '').lstrip('/') for x in content
                 if x.get('ContentType') == 'application/fb3-body+xml']
        if len(paths) != 1:
            raise ConversionError('FB3 requires one declared body part.')
        root = archive.xml(paths[0])
        if root.tag != q(FB3, 'fb3-body'):
            raise ConversionError('Invalid FictionBook 3 body.')
        bodies, namespace = [root], FB3
    for body_index, body in enumerate(bodies, 1):
        for index, node in enumerate(body.iter(), 1):
            if node.tag in (q(namespace, 'p'), q(namespace, 'v'), q(namespace, 'subtitle')):
                output.add(''.join(node.itertext()), f'Body {body_index}, paragraph {index}')
    output.warnings.append('Book body text only; images, page layout and printed page numbers are not reconstructed.')
    return output.result('safe-fictionbook-xml/1')


def extract_builtin(path, budget, *, encoding=None):
    path = Path(path)
    if path.stat().st_size > budget.max_source_bytes:
        raise ConversionError('Source exceeds the selected byte budget.')
    data = path.read_bytes()
    suffix = path.suffix.casefold()
    output = TextOutput(budget)
    if suffix in ('.txt', '.md', '.markdown'):
        output.add(decode(data, encoding), 'Document text')
        if suffix != '.txt':
            output.warnings.append('Markdown markup is preserved. Choose the Pandoc reader to create plain narration text.')
        return output.result('strict-text-decoder/1')
    if suffix in ('.html', '.htm', '.xhtml'):
        output.add(html_text(decode(data, encoding), budget), 'HTML body')
        output.warnings.append('Scripts, styles, frames, active objects and embedded mathematics were excluded; external resources were not fetched. CSS visibility and visual layout are not reproduced.')
        return output.result('stdlib-html-parser/1')
    if suffix == '.eml':
        message = BytesParser(policy=policy.default).parsebytes(data)
        parts = list(message.walk())
        if len(parts) > 1000:
            raise ConversionError('Email part budget exceeded.')
        body = message.get_body(preferencelist=('plain', 'html'))
        if body is None or body.get_content_disposition() == 'attachment':
            raise ConversionError('Email has no supported non-attachment body.')
        raw = body.get_payload(decode=True)
        if not isinstance(raw, bytes):
            raise ConversionError('Email text encoding is invalid.')
        value = decode(raw, body.get_content_charset() or 'ascii')
        if body.get_content_type() == 'text/html':
            value = html_text(value, budget)
        output.add(value, 'Email body')
        output.warnings.append('Attachments and active HTML were not opened; one plain/HTML alternative was selected.')
        return output.result('stdlib-email-parser/1', {'subject': str(message.get('Subject', ''))})
    if suffix == '.docx':
        preflight_archive(data, budget)
        from ..extract.docx_ordered import extract_docx
        result = extract_docx(data)
        for row in result['records']:
            output.add(row['raw_text'], str(row['logical_locator']))
        output.warnings.extend(result['warnings'])
        return output.result('existing-reviewed-docx-adapter', result['provenance'])
    if suffix == '.epub':
        preflight_archive(data, budget)
        from ..extract.epub_ordered import extract_epub
        result = extract_epub(data)
        for index, row in enumerate(result['records'], 1):
            output.add(row['raw_text'], 'Spine section ' + str(index))
        output.warnings.extend(result.get('warnings', []))
        return output.result('existing-reviewed-epub-adapter', result.get('provenance', {}))
    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ConversionError('Password-protected PDFs are not decrypted or bypassed.')
        if not 1 <= len(reader.pages) <= budget.max_pages:
            raise ConversionError('PDF physical-page budget exceeded.')
        for index, page in enumerate(reader.pages, 1):
            value = page.extract_text() or ''
            if not value.strip():
                output.warnings.append('At least one page has no native text and may require OCR in the library import workflow.')
            output.add(value, 'Physical PDF page ' + str(index), physical_index=index)
        output.warnings.append('Native PDF text is unverified. Invisible text, column order, OCR accuracy and printed page labels require source review.')
        return output.result('pypdf-native-text')
    if suffix in ('.odt', '.ods', '.odp'):
        return odf(data, suffix, output)
    if suffix == '.xlsx':
        return xlsx(data, output)
    if suffix == '.pptx':
        return pptx(data, output)
    if suffix in ('.fb2', '.fb3'):
        return fictionbook(data, suffix, output)
    raise ConversionError('This source format has no enabled safe built-in reader.')
