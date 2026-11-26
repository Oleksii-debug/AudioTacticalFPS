"""Bounded EPUB spine text preview; extracted text is never a verified quote.

ZIP members stay in memory. XHTML is parsed as XML without entities, external
resources, scripts or stylesheets. Reading order comes from each spine itemref,
including supplementary and repeated occurrences, rather than archive order.
"""
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
import json
import re
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from ..catalog.office_archive import validate_office


OCF = '{urn:oasis:names:tc:opendocument:xmlns:container}'
OPF = '{http://www.idpf.org/2007/opf}'
DC = '{http://purl.org/dc/elements/1.1/}'
XHTML = '{http://www.w3.org/1999/xhtml}'
XML = '{http://www.w3.org/XML/1998/namespace}'
ENC = '{http://www.w3.org/2001/04/xmlenc#}'
EPUB_TYPE = '{http://www.idpf.org/2007/ops}type'
NCX = '{http://www.daisy.org/z3986/2005/ncx/}'

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_XML = 1024 * 1024
MAX_CHAPTER_XML = 8 * 1024 * 1024
MAX_TOTAL_XML = 32 * 1024 * 1024
MAX_XML_NODES = 100_000
MAX_TOTAL_NODES = 500_000
MAX_XML_DEPTH = 128
MAX_SPINE = 1000
MAX_TEXT = 10_000_000
MAX_LOCATORS = 1024 * 1024
MAX_ALTERNATIVES = 1000
MAX_NAVIGATION = 2000
MAX_NAVIGATION_BYTES = 2 * 1024 * 1024
MAX_ANCHORS = 100_000
MAX_NAV_LABEL = 1024
_SPACE = re.compile(r'[ \t\r\n\f]+')
_BAD_PERCENT = re.compile(r'%(?![0-9A-Fa-f]{2})')
_ENCODED_SEPARATOR = re.compile(r'%(?:2f|5c)', re.IGNORECASE)
_BLOCKS = frozenset(('address article aside blockquote caption dd details div dl dt '
                     'figcaption figure footer h1 h2 h3 h4 h5 h6 header hgroup hr '
                     'li main nav ol p pre section summary table tbody td tfoot th '
                     'thead tr ul').split())
_STATIC = frozenset(('a abbr address article aside b bdi bdo blockquote body br '
                    'caption cite code col colgroup data dd del details dfn div dl '
                    'dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hgroup '
                    'hr i img ins kbd li link main mark meta nav noscript ol p '
                    'picture pre q s samp section small source span strong sub '
                    'summary sup table tbody td tfoot th thead time tr u ul var '
                    'wbr').split())
_OBFUSCATION = frozenset(('http://www.idpf.org/2008/embedding',
                         'http://ns.adobe.com/pdf/enc#RC'))
_FONT_TYPES = frozenset(('font/otf', 'font/ttf', 'font/woff', 'font/woff2',
                        'application/vnd.ms-opentype', 'application/font-sfnt',
                        'application/x-font-ttf', 'application/x-font-opentype'))


class EpubError(ValueError):
    """A fixed, non-source-bearing explanation of an unsupported EPUB."""


@dataclass
class _Budget:
    xml_bytes: int = 0
    nodes: int = 0
    output_chars: int = 0
    locator_bytes: int = 0
    navigation_count: int = 0
    navigation_bytes: int = 0
    anchors: int = 0

    def navigation(self, rows):
        self.navigation_count += len(rows)
        self.navigation_bytes += len(json.dumps(rows, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8'))
        if (self.navigation_count > MAX_NAVIGATION
                or self.navigation_bytes > MAX_NAVIGATION_BYTES):
            raise EpubError('EPUB navigation budget exceeded.')


class _TreeBuilder(ET.TreeBuilder):
    def __init__(self, budget):
        super().__init__()
        self.budget, self.nodes, self.depth = budget, 0, 0

    def start(self, tag, attributes):
        self.nodes += 1
        self.budget.nodes += 1
        self.depth += 1
        if (self.nodes > MAX_XML_NODES or self.budget.nodes > MAX_TOTAL_NODES
                or self.depth > MAX_XML_DEPTH or len(attributes) > 256):
            raise EpubError('EPUB XML structure budget exceeded.')
        if XML + 'base' in attributes:
            raise EpubError('EPUB xml:base references are not supported.')
        if tag == XHTML + 'base':
            raise EpubError('EPUB XHTML base references are not supported.')
        return super().start(tag, attributes)

    def end(self, tag):
        result = super().end(tag)
        self.depth -= 1
        return result


def _xml(archive, name, budget, *, chapter=False):
    from defusedxml.ElementTree import DefusedXMLParser

    try:
        info = archive.getinfo(name)
    except KeyError:
        raise EpubError('An EPUB referenced resource is missing.') from None
    limit = MAX_CHAPTER_XML if chapter else MAX_PACKAGE_XML
    if info.is_dir() or not 0 < info.file_size <= limit:
        raise EpubError('EPUB XML member size limit exceeded.')
    budget.xml_bytes += info.file_size
    if budget.xml_bytes > MAX_TOTAL_XML:
        raise EpubError('EPUB total XML budget exceeded.')
    with archive.open(info) as source:
        data = source.read(limit + 1)
    if len(data) != info.file_size or len(data) > limit:
        raise EpubError('EPUB XML member size mismatch.')
    parser = DefusedXMLParser(target=_TreeBuilder(budget), forbid_dtd=not chapter,
                             forbid_entities=True, forbid_external=True)
    if chapter:
        # XHTML5's ordinary <!DOCTYPE html> is harmless. Do not allow an
        # external/public DTD or even an internal subset with default attrs.
        # The pinned parser's entity/external handlers remain enabled.
        def doctype(name, system, public, internal):
            if name != 'html' or system or public or internal:
                raise EpubError('EPUB XHTML DTD is not supported.')
        parser.parser.StartDoctypeDeclHandler = doctype
    try:
        for offset in range(0, len(data), 64 * 1024):
            parser.feed(data[offset:offset + 64 * 1024])
        return parser.close()
    except EpubError:
        raise
    except (ET.ParseError, ValueError):
        raise EpubError('EPUB XML is invalid or unsupported.') from None


def _resource(base, href):
    """Resolve a local IRI inside the archive, decoding once before traversal."""
    if (not isinstance(href, str) or not 0 < len(href) <= 4096
            or href != href.strip() or '\\' in href
            or any(ord(c) < 32 or ord(c) == 127 for c in href)
            or _BAD_PERCENT.search(href) or _ENCODED_SEPARATOR.search(href)):
        raise EpubError('EPUB resource reference is invalid.')
    uri = urlsplit(href)
    if uri.scheme or uri.netloc or uri.query or uri.fragment or not uri.path:
        raise EpubError('EPUB primary resources must be local files without fragments.')
    try:
        path = unquote(uri.path, encoding='utf-8', errors='strict')
    except UnicodeError:
        raise EpubError('EPUB resource reference encoding is invalid.') from None
    if (path.startswith('/') or '\\' in path or ':' in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise EpubError('EPUB resource reference escapes the archive.')
    parts = base.split('/')[:-1] if base else []
    for part in path.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise EpubError('EPUB resource reference escapes the archive.')
            parts.pop()
        else:
            parts.append(part)
    if not parts or path.endswith('/'):
        raise EpubError('EPUB resource reference is not a file.')
    return '/'.join(parts)


def _identifier(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 512
            or any(c.isspace() or unicodedata.category(c).startswith('C') for c in value)):
        raise EpubError('EPUB manifest identity is invalid.')
    return value


def _display(value, limit=512):
    value = ' '.join(value.split())
    if len(value) > limit or any(unicodedata.category(c).startswith('C') for c in value):
        return ''
    return value


def _one(parent, name, message):
    found = parent.findall(name)
    if len(found) != 1:
        raise EpubError(message)
    return found[0]


def _package(archive, budget, warnings):
    container = _xml(archive, 'META-INF/container.xml', budget)
    if container.tag != OCF + 'container':
        raise EpubError('EPUB container is invalid.')
    rootfiles = _one(container, OCF + 'rootfiles', 'EPUB rootfiles are missing or ambiguous.')
    roots = rootfiles.findall(OCF + 'rootfile')
    if len(roots) != 1 or roots[0].get('media-type') != 'application/oebps-package+xml':
        raise EpubError('Multiple or unsupported EPUB renditions require review.')
    package_name = _resource('', roots[0].get('full-path', ''))
    package = _xml(archive, package_name, budget)
    if package.tag != OPF + 'package' or package.get('version') not in ('2.0', '3.0'):
        raise EpubError('EPUB package version or namespace is unsupported.')
    manifest = _one(package, OPF + 'manifest', 'EPUB manifest is missing or ambiguous.')
    spine = _one(package, OPF + 'spine', 'EPUB spine is missing or ambiguous.')
    items = {}
    for item in manifest:
        if item.tag != OPF + 'item':
            raise EpubError('EPUB manifest contains an unsupported structure.')
        ident = _identifier(item.get('id', ''))
        if ident in items:
            raise EpubError('EPUB manifest identity is duplicated.')
        href = item.get('href', '')
        # Remote non-spine resources are never loaded. Their existence is
        # disclosed; selecting one as primary content is rejected below.
        if len(href) > 4096 or any(ord(c) < 32 for c in href):
            raise EpubError('EPUB resource reference is invalid.')
        uri = urlsplit(href)
        remote = bool(uri.scheme or uri.netloc)
        path = None if remote else _resource(package_name, href)
        if remote:
            warnings.add('remote_resources_not_loaded')
        items[ident] = (path, item.get('media-type', ''), item.get('properties', '').split())
    order = []
    for ref in spine:
        if ref.tag != OPF + 'itemref' or len(order) >= MAX_SPINE:
            raise EpubError('EPUB spine structure or size is unsupported.')
        ident = _identifier(ref.get('idref', ''))
        if ident not in items:
            raise EpubError('EPUB spine references a missing manifest item.')
        path, media, properties = items[ident]
        if path is None or media != 'application/xhtml+xml':
            raise EpubError('EPUB spine requires local XHTML content; its format is unsupported.')
        linear = ref.get('linear', 'yes')
        if linear not in ('yes', 'no'):
            raise EpubError('EPUB spine linear value is invalid.')
        if linear == 'no':
            warnings.add('supplementary_spine_items_included')
        if 'scripted' in properties:
            warnings.add('scripts_not_executed')
        order.append((ident, path, linear == 'yes'))
    if not order:
        raise EpubError('EPUB reading order is empty.')
    metadata = package.findall(OPF + 'metadata')
    if len(metadata) > 1:
        raise EpubError('EPUB metadata is ambiguous.')
    hints = {'verification': 'unverified', 'titles': [], 'creators': [], 'languages': []}
    if metadata:
        for field, tag in (('titles', 'title'), ('creators', 'creator'), ('languages', 'language')):
            for element in metadata[0].findall(DC + tag)[:100]:
                if not len(element) and (value := _display(element.text or '')):
                    hints[field].append(value)
    return package_name, items, order, hints, spine.get('toc')


def _encryption(archive, package_name, items, order, budget, warnings):
    names = set(archive.namelist())
    if 'META-INF/encryption.xml' not in names:
        return
    document = _xml(archive, 'META-INF/encryption.xml', budget)
    if document.tag != OCF + 'encryption' or not len(document):
        raise EpubError('EPUB encryption declaration is unsupported.')
    required = {'META-INF/container.xml', package_name, *(path for _, path, _ in order)}
    types_by_path = {}
    for path, media, _ in items.values():
        types_by_path.setdefault(path, set()).add(media)
    seen = set()
    for encrypted in document:
        if encrypted.tag != ENC + 'EncryptedData':
            raise EpubError('EPUB encryption declaration is unsupported.')
        method = _one(encrypted, ENC + 'EncryptionMethod', 'EPUB encryption method is ambiguous.')
        data = _one(encrypted, ENC + 'CipherData', 'EPUB encryption target is ambiguous.')
        target = _one(data, ENC + 'CipherReference', 'EPUB encryption target is ambiguous.')
        path = _resource('', target.get('URI', ''))
        if path in required:
            raise EpubError('Encrypted EPUB content is not supported.')
        media = types_by_path.get(path, ())
        if (path in seen or method.get('Algorithm') not in _OBFUSCATION
                or not media or any(mime not in _FONT_TYPES for mime in media)
                or path not in names):
            raise EpubError('Only unused obfuscated EPUB fonts are supported.')
        seen.add(path)
    warnings.add('obfuscated_fonts_not_rendered')


class _Text:
    def __init__(self):
        self.parts = []
        self.length = 0
        # ``length`` remains the original conservative safety counter. These
        # positions describe final output, including generated line breaks.
        self.rendered_length = 0
        self.positions = []

    def mark(self):
        position = [self.rendered_length]
        self.positions.append(position)
        return position

    def _removed(self, length):
        self.rendered_length -= length
        # Positions are created in order. Only markers in a removed suffix
        # move; preserving them as references also fixes previously seen IDs.
        for position in reversed(self.positions):
            if position[0] <= self.rendered_length:
                break
            position[0] = self.rendered_length

    def _trim(self):
        while self.parts and self.parts[-1][0] == 'text':
            kind, value = self.parts[-1]
            trimmed = value.rstrip(' ')
            self._removed(len(value) - len(trimmed))
            if trimmed:
                self.parts[-1] = (kind, trimmed)
                return
            self.parts.pop()

    def add(self, value, *, pre=False):
        if not value:
            return
        if not pre:
            value = _SPACE.sub(' ', value)
            if not self.parts or self.parts[-1][1].endswith((' ', '\n')):
                value = value.lstrip(' ')
        if value:
            self.length += len(value)
            if self.length > MAX_TEXT:
                raise EpubError('EPUB text budget exceeded.')
            self.parts.append(('pre' if pre else 'text', value))
            self.rendered_length += len(value)

    def line(self, *, explicit=False):
        self._trim()
        if self.parts and (explicit or not self.parts[-1][1].endswith('\n')):
            self.parts.append(('break' if explicit else 'boundary', '\n'))
            self.rendered_length += 1

    def finish(self):
        self._trim()
        while self.parts and self.parts[-1][0] == 'boundary':
            self._removed(len(self.parts.pop()[1]))
        return ''.join(value for _, value in self.parts)


def _hidden(node):
    # CSS can override prior declarations, inherited visibility and even the
    # browser's default hidden styling. We deliberately do not compute styles;
    # CSS-dependent text is retained with a layout warning, never silently
    # discarded by an incomplete approximation of the cascade.
    return ('hidden' in node.attrib
            and node.get('hidden', '').strip().casefold() != 'until-found')


def _chapter(archive, path, budget, warnings):
    document = _xml(archive, path, budget, chapter=True)
    if document.tag != XHTML + 'html':
        raise EpubError('EPUB spine content is not XHTML.')
    if (document.text and document.text.strip()
            or any(child.tag not in (XHTML + 'head', XHTML + 'body')
                   or child.tail and child.tail.strip() for child in document)):
        raise EpubError('EPUB XHTML document structure is unsupported.')
    body = _one(document, XHTML + 'body', 'EPUB XHTML body is missing or ambiguous.')
    heads = document.findall(XHTML + 'head')
    if len(heads) > 1:
        raise EpubError('EPUB XHTML head is ambiguous.')
    title = ''
    if heads:
        titles = heads[0].findall(XHTML + 'title')
        if len(titles) > 1:
            raise EpubError('EPUB chapter title is ambiguous.')
        if titles:
            title = _display(''.join(titles[0].itertext()))
        for element in heads[0].iter():
            if element.tag == XHTML + 'script':
                warnings.add('script_content_omitted')
            if element.tag in (XHTML + 'style', XHTML + 'link'):
                warnings.add('styles_not_rendered')
    output, alternatives, headings = _Text(), [], []
    heading_locations, anchors = [], {}
    images = 0

    def remember(node, *, heading=False):
        identifiers = {node.get('id'), node.get(XML + 'id')} - {None}
        if not identifiers and not heading:
            return None
        position = output.mark()
        for identifier in identifiers:
            budget.anchors += 1
            if (budget.anchors > MAX_ANCHORS or not identifier or len(identifier) > MAX_NAV_LABEL
                    or any(c.isspace() or unicodedata.category(c).startswith('C') for c in identifier)):
                raise EpubError('EPUB anchor identity or budget is unsupported.')
            # Retain legacy text extraction of duplicate IDs. A navigation
            # request for one must never silently choose either occurrence.
            anchors[identifier] = None if identifier in anchors or len(identifiers) > 1 else position
        return position

    def walk(node, pre=False):
        nonlocal images
        if _hidden(node):
            warnings.add('hidden_content_omitted')
            return
        if not node.tag.startswith(XHTML):
            raise EpubError('EPUB non-XHTML content requires a separate supported adapter.')
        tag = node.tag[len(XHTML):]
        if tag in ('script', 'style', 'template'):
            warnings.add(tag + '_content_omitted')
            if tag == 'style':
                warnings.add('styles_not_rendered')
            return
        if tag not in _STATIC:
            raise EpubError('EPUB XHTML contains an unsupported content structure.')
        if 'style' in node.attrib or 'class' in node.attrib:
            warnings.add('styles_not_rendered')
        if tag in ('table', 'td', 'th'):
            warnings.add('table_layout_not_rendered')
        if tag in ('ins', 'del'):
            warnings.add('revision_markup_not_rendered')
        if tag == 'noscript':
            warnings.add('noscript_fallback_included')
        if tag in ('link', 'meta', 'source', 'col'):
            if node.text and node.text.strip() or len(node):
                raise EpubError('EPUB void content contains unexpected text.')
            if tag == 'link':
                warnings.add('styles_not_rendered')
            remember(node)
            return
        if tag == 'img':
            images += 1
            warnings.add('images_omitted_from_text')
            alt = node.get('alt', '')
            if len(node) or node.text and node.text.strip() or len(alt) > 4096:
                raise EpubError('EPUB image alternative is invalid or oversized.')
            if alt:
                if len(alternatives) >= MAX_ALTERNATIVES:
                    raise EpubError('EPUB image alternative budget exceeded.')
                alternatives.append({'kind': 'image_alt', 'image_index': images, 'text': alt})
            elif 'alt' not in node.attrib:
                warnings.add('images_without_text_alternatives')
            output.line()
            remember(node)
            return
        if tag == 'br':
            if len(node) or node.text and node.text.strip():
                raise EpubError('EPUB line break contains unexpected text.')
            output.line(explicit=True)
            remember(node)
            return
        if tag in _BLOCKS:
            output.line()
        is_heading = tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
        position = remember(node, heading=is_heading)
        pre = pre or tag == 'pre'
        heading_start = len(output.parts)
        output.add(node.text, pre=pre)
        for child in node:
            walk(child, pre)
            output.add(child.tail, pre=pre)
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and not headings:
            heading = _display(''.join(value for _, value in output.parts[heading_start:]))
            if heading:
                headings.append(heading)
        if is_heading:
            if len(heading_locations) >= MAX_NAVIGATION:
                raise EpubError('EPUB navigation budget exceeded.')
            heading_locations.append((position, output.mark(), int(tag[1])))
        if tag in _BLOCKS:
            output.line()

    walk(body)
    raw = output.finish()
    locations = []
    for start, end, level in heading_locations:
        label = _display(raw[start[0]:end[0]], MAX_NAV_LABEL)
        if label:
            locations.append(dict(title=label, offset=start[0], level=level))
    locations.sort(key=lambda row: row['offset'])
    anchors = {key: None if value is None else value[0] for key, value in anchors.items()}
    return raw, title or (headings[0] if headings else ''), alternatives, locations, anchors


def _navigation_link(base, href):
    """Resolve a navigation IRI locally; fragments are never resource paths."""
    if (not isinstance(href, str) or not 0 < len(href) <= 4096 or href != href.strip()
            or '\\' in href or _BAD_PERCENT.search(href)
            or any(ord(c) < 32 or ord(c) == 127 for c in href)):
        raise EpubError('EPUB navigation link is invalid.')
    uri = urlsplit(href)
    if uri.scheme or uri.netloc or '?' in href.split('#', 1)[0]:
        raise EpubError('EPUB navigation must use local resources without queries.')
    path = _resource(base, uri.path) if uri.path else base
    try:
        fragment = unquote(uri.fragment, encoding='utf-8', errors='strict')
    except UnicodeError:
        raise EpubError('EPUB navigation fragment encoding is invalid.') from None
    if (len(path) > MAX_NAV_LABEL or len(fragment) > MAX_NAV_LABEL
            or any(c.isspace() or unicodedata.category(c).startswith('C') for c in fragment)):
        raise EpubError('EPUB navigation target is invalid or oversized.')
    return path, fragment


def _navigation_label(node):
    values = []

    def visit(element):
        if (not element.tag.startswith(XHTML) or _hidden(element)
                or element.tag in (XHTML + 'script', XHTML + 'style', XHTML + 'template')):
            raise EpubError('EPUB navigation label contains unsupported content.')
        if element.tag == XHTML + 'img':
            if len(element) or (element.text or '').strip():
                raise EpubError('EPUB navigation image contains unexpected text.')
            values.append(element.get('alt', ''))
        elif element.tag == XHTML + 'br':
            if len(element) or (element.text or '').strip():
                raise EpubError('EPUB navigation line break contains unexpected text.')
            values.append(' ')
        elif element.text:
            values.append(element.text)
        for child in element:
            visit(child)
            if child.tail:
                values.append(child.tail)

    visit(node)
    label = _display(''.join(values), MAX_NAV_LABEL)
    if not label:
        raise EpubError('EPUB navigation label is empty or unsupported.')
    return label


def _navigation(archive, items, ncx_id, order, parsed, budget, warnings):
    """Build optional contents without choosing between repeated occurrences."""
    occurrences = {}
    for ordinal, (_, path, _) in enumerate(order, 1):
        occurrences.setdefault(path, []).append(ordinal)
    ambiguous = False

    def target(base, href):
        nonlocal ambiguous
        path, fragment = _navigation_link(base, href)
        if path not in occurrences:
            raise EpubError('EPUB navigation target is outside the reading order.')
        anchors = parsed[path][4]
        if fragment and (fragment not in anchors or anchors[fragment] is None):
            raise EpubError('EPUB navigation anchor is missing or ambiguous.')
        if len(occurrences[path]) != 1:
            ambiguous = True
            return None, None
        return occurrences[path][0], anchors.get(fragment, 0)

    def append(output, row):
        budget.navigation([row])
        output.append(row)

    toc, pages = [], []
    nav_items = [item for item in items.values() if 'nav' in item[2]]
    if len(nav_items) > 1:
        raise EpubError('EPUB has ambiguous navigation documents.')
    if nav_items:
        member, media, _ = nav_items[0]
        if member is None or media != 'application/xhtml+xml':
            raise EpubError('EPUB navigation requires local XHTML.')
        if archive.getinfo(member).file_size > MAX_PACKAGE_XML:
            raise EpubError('EPUB navigation XML size limit exceeded.')
        root = _xml(archive, member, budget, chapter=True)
        if (root.tag != XHTML + 'html' or (root.text or '').strip()
                or any(child.tag not in (XHTML + 'head', XHTML + 'body')
                       or (child.tail or '').strip() for child in root)
                or len(root.findall(XHTML + 'head')) > 1):
            raise EpubError('EPUB navigation XHTML structure is unsupported.')
        body = _one(root, XHTML + 'body', 'EPUB navigation body is missing or ambiguous.')
        navs = [node for node in body.iter(XHTML + 'nav')
                if 'toc' in (node.get(EPUB_TYPE) or '').split()]
        if len(navs) != 1:
            raise EpubError('EPUB navigation contents are missing or ambiguous.')

        def read_list(ordered, depth, output, *, grouping=True):
            if depth > 32 or any(child.tag != XHTML + 'li' for child in ordered):
                raise EpubError('EPUB contents list structure or nesting is unsupported.')
            for li in ordered:
                labels = [node for node in li if node.tag in (XHTML + 'a', XHTML + 'span')]
                if (len(labels) != 1 or (li.text or '').strip()
                        or any(node.tag not in (XHTML + 'a', XHTML + 'span', XHTML + 'ol')
                               or (node.tail or '').strip() for node in li)):
                    raise EpubError('EPUB contents item has an ambiguous label.')
                label_node = labels[0]
                label = _navigation_label(label_node)
                if label_node.tag == XHTML + 'a':
                    ordinal, offset = target(member, label_node.get('href', ''))
                    row = dict(title=label, ordinal=ordinal, offset=offset, depth=depth)
                elif grouping:
                    row = dict(title=label, ordinal=None, offset=None, depth=depth, heading_only=True)
                else:
                    raise EpubError('EPUB page hint must identify a chapter location.')
                append(output, row)
                nested = li.findall(XHTML + 'ol')
                if len(nested) > 1:
                    raise EpubError('EPUB contents item has ambiguous child lists.')
                if nested:
                    read_list(nested[0], depth + 1, output, grouping=grouping)

        read_list(_one(navs[0], XHTML + 'ol', 'EPUB contents list is missing or ambiguous.'), 0, toc)
        page_lists = [node for node in body.iter(XHTML + 'nav')
                      if 'page-list' in (node.get(EPUB_TYPE) or '').split()]
        if len(page_lists) > 1:
            raise EpubError('EPUB has ambiguous page-hint lists.')
        if page_lists:
            read_list(_one(page_lists[0], XHTML + 'ol', 'EPUB page-hint list is missing or ambiguous.'),
                      0, pages, grouping=False)
    elif ncx_id:
        ncx = items.get(ncx_id)
        if not ncx or ncx[0] is None or ncx[1] != 'application/x-dtbncx+xml':
            raise EpubError('EPUB NCX navigation is missing or unsupported.')
        member = ncx[0]
        root = _xml(archive, member, budget)
        if root.tag != NCX + 'ncx':
            raise EpubError('EPUB NCX navigation root is unsupported.')

        def ncx_row(node, depth):
            label_node = _one(_one(node, NCX + 'navLabel', 'EPUB NCX label is ambiguous.'),
                              NCX + 'text', 'EPUB NCX label text is ambiguous.')
            label = _display(''.join(label_node.itertext()), MAX_NAV_LABEL)
            if not label:
                raise EpubError('EPUB NCX label is empty or unsupported.')
            ordinal, offset = target(member, _one(node, NCX + 'content',
                'EPUB NCX target is ambiguous.').get('src', ''))
            return dict(title=label, ordinal=ordinal, offset=offset, depth=depth)

        def read_points(parent, depth):
            if depth > 32:
                raise EpubError('EPUB NCX contents nesting exceeds the supported limit.')
            for node in parent.findall(NCX + 'navPoint'):
                append(toc, ncx_row(node, depth))
                read_points(node, depth + 1)

        read_points(_one(root, NCX + 'navMap', 'EPUB NCX contents are missing or ambiguous.'), 0)
        page_lists = root.findall(NCX + 'pageList')
        if len(page_lists) > 1:
            raise EpubError('EPUB NCX page-hint lists are ambiguous.')
        if page_lists:
            for node in page_lists[0].findall(NCX + 'pageTarget'):
                append(pages, ncx_row(node, 0))
    if ambiguous:
        warnings.add('embedded_contents_ambiguous_spine')
        return [], []
    if not any(row.get('ordinal') is not None for row in toc):
        warnings.add('embedded_contents_unavailable')
    if pages:
        warnings.add('page_list_labels_unverified')
    return toc, pages


def extract_epub(data: bytes):
    """Return spine-occurrence records with unknown page numbers and honest warnings.

    ``logical_locator`` is bounded JSON for section selection, not a page map.
    Source-authored image alternatives remain in a separate record field.
    Caller must isolate this parser and retain the original EPUB bytes.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ValueError('Invalid EPUB source snapshot.')
    if version('defusedxml') != '0.7.1':
        raise RuntimeError('Unreviewed defusedxml version.')
    budget = _Budget()
    warnings = {'logical_locator_not_page', 'layout_not_verified', 'xhtml_text_preview',
                'source_quotations_unverified'}
    try:
        validate_office(data, '.epub')
        with ZipFile(BytesIO(data)) as archive:
            package, items, order, hints, ncx_id = _package(archive, budget, warnings)
            _encryption(archive, package, items, order, budget, warnings)
            records, parsed = [], {}
            for index, (ident, path, linear) in enumerate(order, 1):
                if path not in parsed:
                    chapter_warnings = set()
                    parsed[path] = (*_chapter(archive, path, budget, chapter_warnings), sorted(chapter_warnings))
                    warnings.update(chapter_warnings)
                raw, title, alternatives, headings, _, chapter_warnings = parsed[path]
                budget.navigation(headings)
                locator = dict(spine_index=index, manifest_id=ident, href=path,
                               linear=linear, title=title)
                budget.output_chars += len(raw) + sum(len(a['text']) for a in alternatives)
                budget.locator_bytes += len(json.dumps(
                    [locator, alternatives], ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
                if budget.output_chars > MAX_TEXT or budget.locator_bytes > MAX_LOCATORS:
                    raise EpubError('EPUB text or section metadata budget exceeded.')
                records.append(dict(logical_locator=locator, raw_text=raw,
                    text_sha256=sha256(raw.encode('utf-8')).hexdigest(),
                    alternatives=[dict(a) for a in alternatives], physical_index=None,
                    printed_label=None, printed_label_verified=False,
                    headings=[dict(heading) for heading in headings], warnings=list(chapter_warnings)))
            if not any(row['raw_text'].strip() for row in records):
                raise EpubError('EPUB contains no readable XHTML text.')
            toc, page_hints = _navigation(archive, items, ncx_id, order, parsed, budget, warnings)
    except EpubError:
        raise
    except Exception:
        # Do not disclose raw XML, source paths or archive errors to application
        # logs. The error is an explicit rejection, never a partial publication.
        raise ValueError('EPUB archive or XML is invalid or unsupported.') from None
    return dict(status='review_required', records=records, warnings=sorted(warnings),
                toc=toc, page_hints=page_hints,
                metadata_hints=hints, provenance=dict(asset_sha256=sha256(data).hexdigest(),
                engine='epub-spine', version='1', xml_parser='defusedxml',
                xml_parser_version='0.7.1'))
