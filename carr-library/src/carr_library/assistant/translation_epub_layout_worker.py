"""Bounded EPUB XHTML format mapping over the canonical spine extractor.

The worker is offline and uses the existing retained-source publication host.
It preserves the DOM element structure and does not interpret a translation as
markup. A source profile is deliberately narrower than generic EPUB import.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from urllib.parse import urlsplit

from .translation_layout_worker import (
    MAX_ARCHIVE, MAX_CHARACTERS, MAX_REPLY, MAX_REQUEST, LayoutError)
from ..extract import epub_ordered as canonical_epub

PROFILE = 'carr-epub-xhtml-layout/1'
MAX_UNITS = 2048
NOTICE = (
    'Machine translation of eligible XHTML text blocks in the retained EPUB spine. '
    'Block/table structure, links, IDs, styles and eligible media are retained. '
    'Mixed inline runs place translated text in the first textual style; per-word '
    'emphasis and original screen/page layout are not reconstructed. XHTML whitespace '
    'uses the canonical reader normalization. One whole-block link is retained; '
    'mixed links, inline images/breaks, explicit hidden attributes, script/event '
    'handlers, executable URI schemes, refresh metadata, repeated spine resources '
    'and synchronized media overlays are refused. CSS-dependent visibility is '
    'not computed; the canonical reader retains its text and layout warnings. '
    'Head titles, navigation '
    'outside the translated spine, image alternatives and original package metadata '
    'are not translated. The target language is added to package metadata. Other '
    'ZIP member payloads remain byte-identical. No quotation authority is created.'
)

_X = canonical_epub.XHTML
_XML = canonical_epub.XML
_INLINE_REFUSED = frozenset(('br', 'img', 'audio', 'video', 'source', 'picture',
                            'script', 'style', 'template', 'noscript', 'ins', 'del'))
_UNITS = canonical_epub._BLOCKS - frozenset(('hr', 'table', 'tbody', 'thead',
                                           'tfoot', 'tr', 'ul', 'ol', 'dl'))
_BLOCKED = frozenset(('script', 'template', 'noscript', 'ins', 'del', 'audio', 'video'))


def _digest(raw):
    return sha256(raw).hexdigest()


def _locate(root, path):
    node = root
    for index in path:
        if type(index) is not int or not 0 <= index < len(node):
            raise LayoutError('An XHTML block locator changed.')
        node = node[index]
    return node


def _slots(node):
    result = [(node, 'text')]
    for child in node:
        result.extend(_slots(child))
        result.append((child, 'tail'))
    return result


def _inline(node):
    if any(element.tag[len(_X):] in _INLINE_REFUSED
           or canonical_epub._hidden(element) for element in node.iter()):
        raise LayoutError('This XHTML block contains unsupported inline or hidden content.')
    links = [element for element in node.iter() if element.tag == _X + 'a']
    slots = _slots(node)
    if links:
        if len(links) != 1:
            raise LayoutError('Mixed XHTML hyperlinks require separate review.')
        link = links[0]
        inner = {(id(element), field) for element, field in _slots(link)}
        if any((getattr(element, field) or '').strip() and (id(element), field) not in inner
               for element, field in slots):
            raise LayoutError('Mixed XHTML hyperlinks require separate review.')
        mode = 'one-whole-block-hyperlink'
    else:
        mode = 'first-textual-inline-style'
    return slots, mode


_URI_ATTRIBUTES = frozenset(('href', 'src', 'action', 'formaction', 'cite',
                             'longdesc', 'poster', 'data', 'codebase', 'background',
                             'manifest', 'profile'))
_SAFE_URI_SCHEMES = frozenset(('http', 'https', 'mailto', 'tel'))


def _active_attributes(document):
    # XML entities are decoded by the existing bounded parser. Refuse control
    # characters before URL parsing, including tab/newline scheme obfuscation.
    # This only examines package data; it never follows an external address.
    for element in document.iter():
        attributes = [(name.rsplit('}', 1)[-1].casefold(), value)
                      for name, value in element.attrib.items()]
        if element.tag in (_X + 'script', _X + 'template', _X + 'noscript'):
            raise LayoutError('Active XHTML elements are not eligible for translation.')
        if any(name.startswith('on') for name, _ in attributes):
            raise LayoutError('XHTML event handlers are not eligible for translation.')
        if (element.tag == _X + 'meta'
                and any(name == 'http-equiv' and value.strip().casefold() == 'refresh'
                        for name, value in attributes)):
            raise LayoutError('XHTML refresh metadata is not eligible for translation.')
        for name, value in attributes:
            if name not in _URI_ATTRIBUTES:
                continue
            if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise LayoutError('An XHTML URI attribute is invalid or oversized.')
            try:
                uri = urlsplit(value.strip())
            except ValueError as exc:
                raise LayoutError('An XHTML URI attribute is invalid.') from exc
            if uri.scheme and uri.scheme.casefold() not in _SAFE_URI_SCHEMES:
                raise LayoutError('Executable or unreviewed XHTML URI schemes are refused.')


def _chapter_mapping(archive, path, original_text, *, marker_seed):
    document = canonical_epub._xml(archive, path, canonical_epub._Budget(), chapter=True)
    _active_attributes(document)
    body = canonical_epub._one(document, _X + 'body', 'The XHTML body is ambiguous.')
    for element in document.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith(_X):
            raise LayoutError('The EPUB formatting profile requires ordinary XHTML.')
        tag = element.tag[len(_X):]
        if (tag in _BLOCKED or canonical_epub._hidden(element)
                or any(name.lower().startswith('on') for name in element.attrib)):
            raise LayoutError('Active, hidden or revision-marked XHTML is not eligible for translation.')
    candidates = []
    def walk(node, locator, pre=False):
        tag = node.tag[len(_X):]
        pre = pre or tag == 'pre'
        nested = any(element is not node and element.tag[len(_X):] in canonical_epub._BLOCKS
                     for element in node.iter())
        if tag in _UNITS and not nested:
            slots = _slots(node)
            if any((getattr(element, field) or '').strip() for element, field in slots):
                _, mode = _inline(node)
                candidates.append((tuple(locator), pre, mode))
                if len(candidates) > MAX_UNITS:
                    raise LayoutError('The EPUB exceeds 2,048 eligible text blocks.')
            return
        for index, child in enumerate(node):
            walk(child, (*locator, index), pre)
    body_index = list(document).index(body)
    walk(body, (body_index,))
    copied = deepcopy(document)
    existing = {value for node in document.iter()
                for value in (node.get('id'), node.get(_XML + 'id')) if value is not None}
    prefix = 'carr-layout-' + marker_seed + '-'
    if any(value.startswith(prefix) for value in existing):
        raise LayoutError('The source collides with the private layout marker namespace.')
    for number, (locator, _, _) in enumerate(candidates):
        node = _locate(copied, locator)
        node.attrib.pop(_XML + 'id', None)
        node.set('id', prefix + str(number) + '-start')
        ending = ET.Element(_X + 'span', {'id': prefix + str(number) + '-end'})
        node.append(ending)
    stream = BytesIO()
    with ZipFile(stream, 'w') as instrumented:
        instrumented.writestr(path, ET.tostring(copied, encoding='utf-8', xml_declaration=True))
    with ZipFile(BytesIO(stream.getvalue())) as instrumented:
        raw, _, _, _, anchors = canonical_epub._chapter(
            instrumented, path, canonical_epub._Budget(), set())
    if raw != original_text:
        raise LayoutError('The canonical XHTML text changed during layout mapping.')
    units, previous = [], 0
    for number, (locator, pre, mode) in enumerate(candidates):
        first = anchors.get(prefix + str(number) + '-start')
        last = anchors.get(prefix + str(number) + '-end')
        if (type(first) is not int or type(last) is not int
                or not previous <= first < last <= len(raw)
                or raw[previous:first].strip()):
            raise LayoutError('The XHTML text cannot be mapped to complete nonoverlapping blocks.')
        value = raw[first:last]
        if not value.strip():
            raise LayoutError('A selected XHTML block has no canonical text.')
        units.append({'locator': list(locator), 'start': first, 'end': last,
                      'sha256': _digest(value.encode('utf-8')), 'preformatted': pre,
                      'inline_mode': mode})
        previous = last
    if raw[previous:].strip():
        raise LayoutError('The EPUB has text outside supported complete XHTML blocks.')
    return document, units


def _package(data):
    if type(data) is not bytes or not 0 < len(data) <= MAX_ARCHIVE:
        raise LayoutError('Choose an EPUB within the existing 64 MiB input budget.')
    source_sha256 = _digest(data)
    extracted = canonical_epub.extract_epub(data)
    records = extracted['records']
    if sum(len(row['raw_text']) for row in records) > MAX_CHARACTERS:
        raise LayoutError('Choose an EPUB within 2,000,000 canonical source characters.')
    with ZipFile(BytesIO(data)) as archive:
        if any(name.casefold().startswith(('meta-inf/signatures', '_xmlsignatures/'))
               for name in archive.namelist()):
            raise LayoutError('Signed EPUB packages require separate review.')
        package, items, order, _, _ = canonical_epub._package(
            archive, canonical_epub._Budget(), set())
        if len({path for _, path, _ in order}) != len(order):
            raise LayoutError('Repeated spine resources are not supported for formatted translation.')
        package_xml = canonical_epub._xml(archive, package, canonical_epub._Budget())
        if len(package_xml.findall(canonical_epub.OPF + 'metadata')) != 1:
            raise LayoutError('The EPUB package metadata is missing or ambiguous.')
        if any(node.get('media-overlay') is not None or
               node.get('media-type') == 'application/smil+xml'
               for node in package_xml.iter()):
            raise LayoutError('Synchronized EPUB media overlays require a separate translation review.')
        if any('scripted' in properties for _, _, properties in items.values()):
            raise LayoutError('Scripted EPUB content is not eligible for translation.')
        # Navigation and other local XHTML remain byte-identical, but they
        # must not retain the URI/refresh forms refused in translated chapters.
        extra_budget = canonical_epub._Budget()
        spine_paths = {path for _, path, _ in order}
        checked = set()
        for path, media, _ in items.values():
            if path is not None and media == 'application/xhtml+xml' and path not in spine_paths and path not in checked:
                _active_attributes(canonical_epub._xml(archive, path, extra_budget, chapter=True))
                checked.add(path)
        rows, documents, selected = [], {}, 0
        for ordinal, (record, (_, path, _)) in enumerate(zip(records, order), 1):
            document, units = _chapter_mapping(archive, path, record['raw_text'],
                                               marker_seed=source_sha256[:24])
            for unit in units:
                selected += 1
                unit['ordinal'] = selected
            if selected > MAX_UNITS:
                raise LayoutError('The EPUB exceeds 2,048 eligible text blocks.')
            rows.append({'ordinal': ordinal, 'locator': record['logical_locator'],
                'text': record['raw_text'], 'sha256': record['text_sha256'], 'units': units})
            documents[path] = document
    if not selected:
        raise LayoutError('The EPUB has no eligible nonempty XHTML text blocks.')
    mapping = {'schema': PROFILE, 'source_sha256': source_sha256, 'source_size': len(data),
        'package': package, 'records': rows, 'characters': sum(len(row['text']) for row in rows),
        'selected': selected, 'notice': NOTICE, 'quotation_authority': False}
    return mapping, documents, package_xml


def describe(data):
    return _package(data)[0]


def _canonical_text(value, preformatted):
    writer = canonical_epub._Text()
    writer.add(value, pre=preformatted)
    return writer.finish()


def render(data, replacements, target_language):
    from .models import canonical, text
    import re
    if (type(target_language) is not str
            or re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', target_language) is None
            or len(target_language) > 64):
        raise LayoutError('The target EPUB language is invalid.')
    mapping, documents, package_xml = _package(data)
    expected = [(row, unit) for row in mapping['records'] for unit in row['units']]
    if type(replacements) is not list or len(replacements) != len(expected):
        raise LayoutError('Finish every exact XHTML text block before EPUB export.')
    total, accepted, rendered = 0, {}, {}
    for (row, unit), replacement in zip(expected, replacements):
        if (type(replacement) is not dict
                or set(replacement) != {'ordinal', 'source_sha256', 'text'}
                or type(replacement['ordinal']) is not int
                or replacement['ordinal'] != unit['ordinal']
                or replacement['source_sha256'] != unit['sha256']):
            raise LayoutError('A translation differs from its exact XHTML block mapping.')
        value = text(replacement['text'], 'completed XHTML block', maximum=MAX_CHARACTERS)
        total += len(value)
        if total > MAX_CHARACTERS:
            raise LayoutError('The translated EPUB text exceeds its character budget.')
        normalized = _canonical_text(value, unit['preformatted'])
        if not normalized.strip():
            raise LayoutError('A translated XHTML block has no readable text.')
        accepted[unit['ordinal']] = value
        rendered[unit['ordinal']] = normalized
        node = _locate(documents[row['locator']['href']], unit['locator'])
        slots, mode = _inline(node)
        if mode != unit['inline_mode']:
            raise LayoutError('An XHTML inline mapping changed.')
        first = next((element, field) for element, field in slots
                     if (getattr(element, field) or '').strip())
        for element, field in slots:
            setattr(element, field, value if (element is first[0] and field == first[1]) else None)
        node.set('lang', target_language)
        node.set(_XML + 'lang', target_language)
        for descendant in node.iter():
            for attribute in ('lang', _XML + 'lang'):
                if attribute in descendant.attrib:
                    descendant.set(attribute, target_language)
    # Keep original bibliographic metadata, adding the translated language
    # without replacing a source title, author, identifier or existing language.
    metadata = package_xml.find(canonical_epub.OPF + 'metadata')
    languages = metadata.findall(canonical_epub.DC + 'language')
    if not any((node.text or '').strip() == target_language for node in languages):
        language = ET.Element(canonical_epub.DC + 'language')
        language.text = target_language
        metadata.insert(0, language)
    changed = {}
    ET.register_namespace('', _X[1:-1])
    ET.register_namespace('epub', 'http://www.idpf.org/2007/ops')
    for name, document in documents.items():
        raw = ET.tostring(document, encoding='utf-8', xml_declaration=True)
        if len(raw) > canonical_epub.MAX_CHAPTER_XML:
            raise LayoutError('Translated XHTML exceeds the existing chapter XML bound.')
        changed[name] = raw
    ET.register_namespace('', canonical_epub.OPF[1:-1])
    ET.register_namespace('dc', canonical_epub.DC[1:-1])
    package_data = ET.tostring(package_xml, encoding='utf-8', xml_declaration=True)
    if len(package_data) > canonical_epub.MAX_PACKAGE_XML:
        raise LayoutError('Translated EPUB metadata exceeds its package bound.')
    changed[mapping['package']] = package_data
    output, preserved = BytesIO(), {}
    with ZipFile(BytesIO(data)) as original, ZipFile(output, 'w') as target:
        target.comment = original.comment
        for info in original.infolist():
            raw = changed.get(info.filename)
            if raw is None:
                raw = original.read(info)
                preserved[info.filename] = _digest(raw)
            target.writestr(info, raw)
            if output.tell() > MAX_ARCHIVE:
                raise LayoutError('The translated EPUB exceeds its output byte budget.')
    result = output.getvalue()
    if len(result) > MAX_ARCHIVE:
        raise LayoutError('The translated EPUB exceeds its output byte budget.')
    after = canonical_epub.extract_epub(result)
    if len(after['records']) != len(mapping['records']):
        raise LayoutError('The translated EPUB spine inventory changed.')
    for before, current in zip(mapping['records'], after['records']):
        pieces, previous = [], 0
        for unit in before['units']:
            pieces.append(before['text'][previous:unit['start']])
            pieces.append(rendered[unit['ordinal']])
            previous = unit['end']
        pieces.append(before['text'][previous:])
        # Source titles can depend on a translated heading; all structural
        # locator fields remain exact while that display title may change.
        if (any(current['logical_locator'][key] != before['locator'][key]
                for key in ('spine_index', 'manifest_id', 'href', 'linear'))
                or current['raw_text'] != ''.join(pieces)):
            raise LayoutError('The exported EPUB differs from its accepted text-block mapping.')
    with ZipFile(BytesIO(result)) as archive:
        if any(_digest(archive.read(name)) != digest for name, digest in preserved.items()):
            raise LayoutError('An unselected EPUB package member changed.')
    return result, {'schema': 'carr-epub-layout-output/1', 'source_sha256': mapping['source_sha256'],
        'mapping_sha256': _digest(canonical(mapping).encode('utf-8')),
        'output_sha256': _digest(result), 'output_bytes': len(result),
        'changed_members': sorted(changed), 'preserved_members': preserved,
        'paragraphs': len(expected), 'quotation_authority': False, 'notice': NOTICE}


def _main():
    from ..catalog.intake import source_snapshot
    from ..durable_state import atomic_json, read_json_snapshot
    from ..ingest.worker import _resource_limits
    if len(sys.argv) != 5:
        raise LayoutError('Use the fixed formatted-EPUB worker protocol.')
    request_path, request_sha, expected_origin, timeout_text = sys.argv[1:]
    if Path(__file__).resolve() != Path(expected_origin).resolve():
        raise LayoutError('The formatted-EPUB worker origin differs from its host.')
    timeout = float(timeout_text)
    if not 1 <= timeout <= 120:
        raise LayoutError('The formatted-EPUB worker deadline is invalid.')
    _resource_limits(timeout)
    def offline(event, _arguments):
        if event.startswith('socket.'):
            raise PermissionError('The formatted-EPUB worker is offline.')
    sys.addaudithook(offline)
    request, observed = read_json_snapshot(request_path, max_bytes=MAX_REQUEST)
    if (observed != request_sha or type(request) is not dict
            or set(request) != {'schema', 'action', 'source_sha256', 'replacements', 'target_language'}
            or request['schema'] != 'carr-epub-layout-request/1'
            or request['action'] not in ('describe', 'render')):
        raise LayoutError('The formatted-EPUB request changed or is invalid.')
    source = source_snapshot(Path.cwd(), 'source.epub')
    if source.digest != request['source_sha256']:
        raise LayoutError('The private EPUB snapshot changed.')
    if request['action'] == 'describe':
        if request['replacements'] is not None or request['target_language'] is not None:
            raise LayoutError('Description does not accept translated text.')
        response = describe(source.data)
    else:
        data, response = render(source.data, request['replacements'], request['target_language'])
        with Path('output.epub').open('xb') as output:
            output.write(data)
            output.flush()
            import os
            os.fsync(output.fileno())
    raw = (json.dumps(response, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_REPLY:
        raise LayoutError('The formatted-EPUB response exceeds its byte budget.')
    atomic_json(Path('response.json'), response)
    sys.stdout.buffer.write(b'{"schema":"carr-epub-layout-worker/1","status":"complete"}\n')


if __name__ == '__main__':
    try:
        _main()
    except Exception as error:
        message = str(error) if type(error) is LayoutError else (
            'The EPUB is unsupported, damaged or exceeds a formatting budget.')
        sys.stdout.buffer.write((json.dumps({'schema': 'carr-epub-layout-worker/1',
            'status': 'failed', 'message': message}, ensure_ascii=False) + '\n').encode('utf-8'))
        raise SystemExit(1)
