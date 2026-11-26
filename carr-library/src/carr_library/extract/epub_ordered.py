"""Bounded EPUB spine text and navigation, never a rendered or verified quote.

Uses the existing archive screening and metadata hints, lxml XML parsing and
stdlib URL handling. No archive member is extracted to disk or fetched online.
Original bytes, EPUB reading order and logical chapter identities are retained.
"""
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import posixpath
import re
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from carr_library.catalog.epub_metadata import OCF, OPF, _read_xml, preview_epub
from carr_library.catalog.intake import AssetSnapshot


XHTML = '{http://www.w3.org/1999/xhtml}'
EPUB_TYPE = '{http://www.idpf.org/2007/ops}type'
XML = '{http://www.w3.org/XML/1998/namespace}'
NCX = '{http://www.daisy.org/z3986/2005/ncx/}'
ENC = '{http://www.w3.org/2001/04/xmlenc#}'
MAX_BYTES = 64 * 1024 * 1024
MAX_CHAPTER_XML = 8 * 1024 * 1024
MAX_CHAPTERS = 1000
MAX_TEXT = 10_000_000
MAX_NAVIGATION = 2000
MAX_ELEMENTS = 100_000
MAX_LABEL = 1024
_BLOCKS = frozenset(('address', 'article', 'aside', 'blockquote', 'caption', 'dd',
    'center', 'details', 'dir', 'div', 'dl', 'dt', 'figcaption', 'figure', 'footer',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'li', 'main', 'menu', 'nav',
    'ol', 'p', 'pre', 'section', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead',
    'summary', 'tr', 'ul'))
_STATIC = frozenset(('a abbr acronym address article aside audio b bdi bdo big blockquote '
    'body br caption center cite code col colgroup data dd del details dfn dir div dl dt '
    'em figcaption figure font footer h1 h2 h3 h4 h5 h6 header hgroup hr i img ins kbd '
    'li link main mark menu meta nav noscript ol p picture pre q s samp script section '
    'small source span strike strong style sub summary sup table tbody td template '
    'tfoot th thead time tr tt u ul var video wbr').split())
_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
_HEAD = frozenset(XHTML + tag for tag in ('title', 'link', 'meta', 'style', 'script', 'noscript', 'template'))
_UNSUPPORTED = frozenset(('iframe', 'object', 'embed', 'canvas', 'ruby'))
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


class EpubError(ValueError):
    """A supported, unambiguous EPUB reading path could not be established."""


class _Budget:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.elements = 0
        self.text = 0
        self.navigation = 0
        self.descriptions = 0

    def check(self):
        if self.cancelled():
            raise InterruptedError('EPUB import cancelled.')

    def xml(self, root):
        self.check()
        for element in root.iter():
            self.elements += 1
            if self.elements > MAX_ELEMENTS:
                raise EpubError('EPUB structure exceeds the supported size limit.')
            if element.get(XML + 'base') is not None:
                raise EpubError('EPUB XML base overrides are not supported.')
            if element.tag == XHTML + 'base':
                raise EpubError('EPUB document base overrides are not supported.')
            if (isinstance(element.tag, str) and element.tag.startswith(XHTML)
                    and element.tag[len(XHTML):] in _VOID
                    and (len(element) or (element.text or '').strip())):
                raise EpubError('EPUB void element contains unsupported text or children.')

    def nav(self):
        self.navigation += 1
        if self.navigation > MAX_NAVIGATION:
            raise EpubError('EPUB navigation exceeds the supported size limit.')


def _label(value):
    # Labels are display hints, independent of the untouched extracted text.
    value = ''.join(c for c in value if c not in _BIDI_CONTROLS)
    value = ' '.join(value.split())
    if len(value) > MAX_LABEL:
        raise EpubError('EPUB navigation label is too long.')
    return value


def _url(base, value, *, allow_fragment=True):
    """Resolve only names in a ZIP namespace, never filesystem/network paths."""
    if (not isinstance(value, str) or len(value) > 4096 or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r'%(?![0-9a-fA-F]{2})', value)):
        raise EpubError('EPUB contains an unsafe internal link.')
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.path.startswith('/'):
        raise EpubError('EPUB contains an external or unsupported resource link.')
    if not allow_fragment and parsed.fragment:
        raise EpubError('EPUB resource identity must not contain a fragment.')
    if re.search(r'%(?:2f|5c)', parsed.path, re.IGNORECASE):
        raise EpubError('EPUB contains an ambiguous encoded path.')
    try:
        path = unquote(parsed.path, encoding='utf-8', errors='strict')
        fragment = unquote(parsed.fragment, encoding='utf-8', errors='strict')
    except UnicodeError:
        raise EpubError('EPUB contains an invalid encoded internal link.') from None
    if (any(ord(c) < 32 or ord(c) == 127 for c in path + fragment)
            or '\\' in path or ':' in path or path.startswith('/')
            or len(fragment) > MAX_LABEL):
        raise EpubError('EPUB contains an unsafe decoded internal link.')
    if not path:
        resolved = base
    else:
        parts = posixpath.dirname(base).split('/') if base else []
        parts = [part for part in parts if part]
        for part in path.split('/'):
            if part in ('', '.'):
                if not part:
                    raise EpubError('EPUB contains an ambiguous empty path segment.')
                continue
            if part == '..':
                if not parts:
                    raise EpubError('EPUB resource link escapes its container.')
                parts.pop()
            else:
                parts.append(part)
        resolved = '/'.join(parts)
    if not resolved or len(resolved) > MAX_LABEL:
        raise EpubError('EPUB resource link has no supported target.')
    return resolved, fragment


def _one(root, tag, description):
    found = root.findall(tag)
    if len(found) != 1:
        raise EpubError('EPUB has missing or ambiguous ' + description + '.')
    return found[0]


def _plain_label(element):
    values = []
    def visit(node):
        if not isinstance(node.tag, str):
            return
        if node.tag in (XHTML + 'script', XHTML + 'style', XHTML + 'template'):
            return
        if node.text:
            values.append(node.text)
        if node.tag == XHTML + 'img' and node.get('alt'):
            values.append(node.get('alt'))
        for child in node:
            visit(child)
            if child.tail:
                values.append(child.tail)
    visit(element)
    return _label(''.join(values))


def _document_parts(root):
    """Reject prose outside the body instead of silently discarding it."""
    if root.tag != XHTML + 'html':
        raise EpubError('EPUB content must be an XHTML document.')
    if ((root.text or '').strip() or any(
            (isinstance(node.tag, str) and node.tag not in (XHTML + 'head', XHTML + 'body'))
            or (node.tail or '').strip() for node in root)):
        raise EpubError('EPUB XHTML document structure is unsupported.')
    body = _one(root, XHTML + 'body', 'chapter body')
    heads = root.findall(XHTML + 'head')
    if len(heads) > 1:
        raise EpubError('EPUB XHTML head is ambiguous.')
    head = heads[0] if heads else None
    if head is not None:
        if ((head.text or '').strip() or any(
                (isinstance(node.tag, str) and node.tag not in _HEAD)
                or (node.tail or '').strip() for node in head)):
            raise EpubError('EPUB XHTML head contains unsupported prose or structure.')
        titles = head.findall(XHTML + 'title')
        if len(titles) > 1 or any(len(title) for title in titles):
            raise EpubError('EPUB chapter title is ambiguous or unsupported.')
    return head, body


def _chapter(root, ordinal, member, linear, budget):
    head, body = _document_parts(root)
    parts, length, headings, anchors, warnings, descriptions = [], 0, [], {}, [], []

    def warn(value):
        if value not in warnings:
            warnings.append(value)

    def append(value):
        nonlocal length
        if not value:
            return
        length += len(value)
        budget.text += len(value)
        if budget.text > MAX_TEXT:
            raise EpubError('EPUB text exceeds the supported size limit.')
        parts.append(value)

    def boundary():
        if parts and not parts[-1].endswith('\n'):
            append('\n')

    def walk(element):
        if not isinstance(element.tag, str):
            return
        if not element.tag.startswith(XHTML):
            raise EpubError('EPUB contains unsupported embedded mathematical or graphical content.')
        tag = element.tag[len(XHTML):]
        if tag in _UNSUPPORTED:
            raise EpubError('EPUB contains unsupported embedded or annotated content.')
        if tag not in _STATIC or (tag == 'body' and element is not body):
            raise EpubError('EPUB XHTML contains an unsupported content structure.')
        if any(attribute in element.attrib for attribute in ('style', 'class', 'hidden')):
            warn('styles_not_rendered')
        if tag in ('table', 'td', 'th'):
            warn('table_layout_not_rendered')
        if tag in ('ins', 'del'):
            warn('revision_markup_not_rendered')
        if tag == 'noscript':
            warn('noscript_fallback_included')
        if tag in _BLOCKS:
            boundary()
        identifiers = {element.get('id'), element.get(XML + 'id')} - {None}
        if len(identifiers) > 1:
            raise EpubError('EPUB contains conflicting chapter anchors.')
        for identifier in identifiers:
            if not identifier or len(identifier) > MAX_LABEL or identifier in anchors:
                raise EpubError('EPUB contains empty, duplicate or oversized chapter anchors.')
            anchors[identifier] = length
        if tag in ('script', 'style', 'template'):
            warn({'script': 'scripts_not_executed', 'style': 'styles_not_rendered',
                  'template': 'template_content_omitted'}[tag])
            return
        if tag in ('link', 'meta', 'source', 'col'):
            if tag == 'link':
                warn('styles_not_rendered')
            return
        if tag == 'img':
            warn('images_not_transcribed')
            if element.get('alt'):
                descriptions.append(_label(element.get('alt')))
                budget.descriptions += 1
                if len(descriptions) > 100 or budget.descriptions > MAX_NAVIGATION:
                    raise EpubError('EPUB image descriptions exceed the supported size limit.')
            return
        if tag in ('audio', 'video'):
            warn('media_not_transcribed')
            return
        if tag == 'br':
            append('\n')
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            title = _plain_label(element)
            if title:
                budget.nav()
                headings.append(dict(title=title, offset=length, level=int(tag[1])))
        append(element.text)
        for child in element:
            walk(child)
            append(child.tail)
        if tag in _BLOCKS:
            boundary()

    if head is not None:
        if head.find(XHTML + 'style') is not None or any(
                'stylesheet' in (node.get('rel') or '').split() for node in head.findall(XHTML + 'link')):
            warn('styles_not_rendered')
        if head.find(XHTML + 'script') is not None:
            warn('scripts_not_executed')
    walk(body)
    raw = ''.join(parts)
    if not raw.strip():
        warn('chapter_has_no_readable_text')
    title_element = head.find(XHTML + 'title') if head is not None else None
    title = headings[0]['title'] if headings else (
        _plain_label(title_element) if title_element is not None else '')
    return dict(raw_text=raw, text_sha256=sha256(raw.encode('utf-8')).hexdigest(),
                logical_locator=[ordinal], physical_index=None, printed_label=None,
                printed_label_verified=False, title=title or f'Chapter {ordinal}',
                member=member, linear=linear, headings=headings, warnings=warnings,
                image_descriptions=descriptions), anchors


def _encrypted_members(archive, budget):
    if 'META-INF/encryption.xml' not in archive.namelist():
        return {}
    root = _read_xml(archive, 'META-INF/encryption.xml')
    budget.xml(root)
    result = {}
    for node in root.findall('.//' + ENC + 'EncryptedData'):
        method = _one(node, ENC + 'EncryptionMethod', 'encryption declaration').get('Algorithm', '')
        references = node.findall(ENC + 'CipherData/' + ENC + 'CipherReference')
        if len(references) != 1 or not method:
            raise EpubError('EPUB has an unsupported encryption declaration.')
        member, fragment = _url('', references[0].get('URI', ''), allow_fragment=False)
        if member in result or member not in archive.namelist():
            raise EpubError('EPUB has an invalid encrypted resource identity.')
        result[member] = method
    if not result:
        raise EpubError('EPUB has an unsupported encryption declaration.')
    return result


def extract_epub(data, *, cancelled=lambda: False):
    """Return deterministic, explicitly unverified text in package spine order.

    Supports unencrypted reflowable EPUB 2/3 XHTML chapters and EPUB3/NCX
    navigation. Font obfuscation does not block textual extraction. This is not
    a general EPUB renderer: CSS, scripts, images and printed pages are not
    inferred. Unsupported foreign/annotated text and DTDs fail explicitly.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES or not callable(cancelled):
        raise EpubError('Invalid or oversized EPUB source snapshot.')
    budget = _Budget(cancelled)
    budget.check()
    digest = sha256(data).hexdigest()
    try:
        hints = preview_epub(AssetSnapshot(digest, 'application/epub+zip', len(data), data))
        with ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            container = _read_xml(archive, 'META-INF/container.xml')
            budget.xml(container)
            roots = container.findall(OCF + 'rootfiles/' + OCF + 'rootfile')
            if container.tag != OCF + 'container' or len(roots) != 1:
                raise EpubError('EPUB has missing or ambiguous package information.')
            package_member, _ = _url('', roots[0].get('full-path', ''), allow_fragment=False)
            package = _read_xml(archive, package_member)
            budget.xml(package)
            if package.tag != OPF + 'package' or not re.fullmatch(r'[23]\.[0-9]+', package.get('version', '')):
                raise EpubError('This importer supports EPUB 2 and EPUB 3 packages.')
            metadata = _one(package, OPF + 'metadata', 'package metadata')
            if any(node.get('property') == 'rendition:layout' and (node.text or '').strip() == 'pre-paginated'
                   for node in metadata.findall(OPF + 'meta')):
                raise EpubError('Fixed-layout EPUB requires a rendered reading route.')
            manifest_node = _one(package, OPF + 'manifest', 'manifest')
            manifest, manifest_members = {}, set()
            for item in manifest_node.findall(OPF + 'item'):
                ident = item.get('id', '')
                if not ident or len(ident) > MAX_LABEL or ident in manifest:
                    raise EpubError('EPUB manifest identifiers are missing or duplicated.')
                if not item.get('href'):
                    raise EpubError('EPUB manifest resource link is missing.')
                member, _ = _url(package_member, item.get('href', ''), allow_fragment=False)
                if member not in names:
                    raise EpubError('EPUB references a missing publication resource.')
                if member in manifest_members:
                    raise EpubError('EPUB manifest repeats a publication resource.')
                manifest_members.add(member)
                manifest[ident] = dict(member=member, media_type=item.get('media-type', ''),
                                      properties=(item.get('properties') or '').split())
            spine = _one(package, OPF + 'spine', 'reading order')
            references = spine.findall(OPF + 'itemref')
            if not 1 <= len(references) <= MAX_CHAPTERS:
                raise EpubError('EPUB reading order exceeds the supported chapter limit.')
            encrypted = _encrypted_members(archive, budget)
            warnings = ['logical_locator_not_page', 'layout_not_verified', 'extracted_text_unverified']
            if encrypted:
                warnings.append('encrypted_or_obfuscated_nontext_resources_not_rendered')
            if package_member in encrypted:
                raise EpubError('Encrypted EPUB package content is unsupported.')
            if any(item['member'] in encrypted and (item['media_type'].startswith('text/')
                    or item['media_type'].endswith('+xml') or item['media_type'] == 'application/xml')
                    for item in manifest.values()):
                raise EpubError('Encrypted EPUB textual resources are unsupported.')
            records, chapters, seen_ids, seen_members = [], {}, set(), set()
            for ordinal, reference in enumerate(references, 1):
                budget.check()
                ident = reference.get('idref', '')
                if ident not in manifest or ident in seen_ids:
                    raise EpubError('EPUB reading order has a missing or duplicate item reference.')
                seen_ids.add(ident)
                item = manifest[ident]
                member = item['member']
                if member in seen_members:
                    raise EpubError('EPUB reading order repeats the same chapter resource.')
                seen_members.add(member)
                if item['media_type'] != 'application/xhtml+xml':
                    raise EpubError('This importer supports XHTML EPUB chapters only.')
                if member in encrypted:
                    raise EpubError('Encrypted EPUB chapter text is unsupported.')
                if ('rendition:layout-pre-paginated' in (reference.get('properties') or '').split()):
                    raise EpubError('Fixed-layout EPUB requires a rendered reading route.')
                linear = reference.get('linear', 'yes')
                if linear not in ('yes', 'no'):
                    raise EpubError('EPUB reading order contains an invalid auxiliary marker.')
                root = _read_xml(archive, member, max_bytes=MAX_CHAPTER_XML)
                budget.xml(root)
                record, anchors = _chapter(root, ordinal, member, linear == 'yes', budget)
                records.append(record)
                chapters[member] = (ordinal, anchors)
            if not any(record['linear'] for record in records):
                raise EpubError('EPUB has no primary reading-order chapter.')
            if not any(record['raw_text'].strip() for record in records):
                raise EpubError('EPUB has no readable chapter text; image-only books require OCR.')

            def target(base, href):
                member, fragment = _url(base, href)
                if member not in chapters:
                    raise EpubError('EPUB contents target is outside its declared reading order.')
                ordinal, anchors = chapters[member]
                if fragment and fragment not in anchors:
                    raise EpubError('EPUB contents target has a missing chapter anchor.')
                return ordinal, anchors.get(fragment, 0)

            toc, page_hints = [], []
            nav_items = [item for item in manifest.values() if 'nav' in item['properties']]
            if len(nav_items) > 1:
                raise EpubError('EPUB has multiple navigation documents.')
            if nav_items:
                nav = nav_items[0]
                if nav['media_type'] != 'application/xhtml+xml' or nav['member'] in encrypted:
                    raise EpubError('EPUB navigation format or encryption is unsupported.')
                root = _read_xml(archive, nav['member'])
                budget.xml(root)
                _document_parts(root)
                navs = [node for node in root.iter(XHTML + 'nav')
                        if 'toc' in (node.get(EPUB_TYPE) or '').split()]
                if len(navs) != 1:
                    raise EpubError('EPUB navigation has missing or ambiguous contents.')

                def read_list(ordered, depth, output, *, grouping=True):
                    if depth > 32:
                        raise EpubError('EPUB contents nesting exceeds the supported limit.')
                    for li in ordered.findall(XHTML + 'li'):
                        labels = [node for node in li if node.tag in (XHTML + 'a', XHTML + 'span')]
                        if len(labels) != 1:
                            raise EpubError('EPUB contents item has an ambiguous label.')
                        node = labels[0]
                        label = _plain_label(node)
                        if not label:
                            raise EpubError('EPUB contents item has no readable label.')
                        budget.nav()
                        if node.tag == XHTML + 'a':
                            ordinal, offset = target(nav['member'], node.get('href', ''))
                            output.append(dict(title=label, ordinal=ordinal, offset=offset, depth=depth))
                        else:
                            if not grouping:
                                raise EpubError('EPUB page hint must point to a chapter location.')
                            output.append(dict(title=label, ordinal=None, offset=None,
                                               depth=depth, heading_only=True))
                        nested = li.findall(XHTML + 'ol')
                        if len(nested) > 1:
                            raise EpubError('EPUB contents item has ambiguous child lists.')
                        if nested:
                            read_list(nested[0], depth + 1, output, grouping=grouping)

                read_list(_one(navs[0], XHTML + 'ol', 'contents list'), 0, toc)
                pages = [node for node in root.iter(XHTML + 'nav')
                         if 'page-list' in (node.get(EPUB_TYPE) or '').split()]
                if len(pages) > 1:
                    raise EpubError('EPUB has multiple page-hint lists.')
                if pages:
                    read_list(_one(pages[0], XHTML + 'ol', 'page-hint list'), 0, page_hints, grouping=False)
                    warnings.append('page_list_labels_unverified')
            elif spine.get('toc'):
                ncx = manifest.get(spine.get('toc'))
                if not ncx or ncx['media_type'] != 'application/x-dtbncx+xml' or ncx['member'] in encrypted:
                    raise EpubError('EPUB NCX navigation is missing or unsupported.')
                root = _read_xml(archive, ncx['member'])
                budget.xml(root)
                if root.tag != NCX + 'ncx':
                    raise EpubError('EPUB NCX navigation has an invalid root.')

                def read_points(parent, depth):
                    if depth > 32:
                        raise EpubError('EPUB contents nesting exceeds the supported limit.')
                    for node in parent.findall(NCX + 'navPoint'):
                        label = _label(''.join(_one(_one(node, NCX + 'navLabel', 'NCX label'),
                            NCX + 'text', 'NCX label text').itertext()))
                        if not label:
                            raise EpubError('EPUB NCX item has no readable label.')
                        ordinal, offset = target(ncx['member'], _one(node, NCX + 'content', 'NCX target').get('src', ''))
                        budget.nav()
                        toc.append(dict(title=label, ordinal=ordinal, offset=offset, depth=depth))
                        read_points(node, depth + 1)

                read_points(_one(root, NCX + 'navMap', 'NCX contents'), 0)
            else:
                warnings.append('embedded_contents_unavailable')
            budget.check()
            import lxml
            return dict(status='review_required', records=records, toc=toc,
                        page_hints=page_hints, metadata=asdict(hints), warnings=warnings,
                        provenance=dict(asset_sha256=digest, adapter='epub-spine-v1',
                                        package_member=package_member, engine='lxml',
                                        version=lxml.__version__))
    except (EpubError, InterruptedError):
        raise
    except ImportError:
        raise EpubError('EPUB support is unavailable in this installation.') from None
    except Exception:
        # Parser diagnostics may contain private paths, URLs or source passages.
        raise EpubError('EPUB extraction rejected invalid or unsupported publication content.') from None
