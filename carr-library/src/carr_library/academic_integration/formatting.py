"""Offline CSL formatting with the pinned, unmodified citeproc-js processor.

Only CSL JSON and XML cross the fixed worker boundary. Neither metadata nor
style XML is interpreted as JavaScript. The installed QuickJS runtime is the
default; an explicitly selected Node executable is compatibility mode only.
This module never downloads an engine, a style, a locale or a reference.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Callable, Iterable, Mapping, Sequence

from lxml import etree

from ..audio.cleanup import retained_context
from ..audio.process import owned_process

_ROOT = Path(__file__).resolve().parent
_CSL = '{http://purl.org/net/xbiblio/csl}'
_MAX_BYTES = 16_000_000
_MAX_REFERENCES = 1000
_MAX_CLUSTERS = 5000
_LOCALES = ('en-US', 'uk-UA', 'sk-SK')
_LABELS = frozenset(('page', 'chapter', 'section', 'paragraph', 'line', 'volume',
                     'issue', 'book', 'part', 'verse', 'column', 'folio',
                     'figure', 'note', 'sub-verbo', 'timestamp'))
_NAME_FIELDS = frozenset(('author', 'editor', 'translator', 'director', 'composer',
    'illustrator', 'interviewer', 'recipient', 'reviewed-author', 'collection-editor',
    'container-author', 'original-author', 'editorial-director', 'editor-translator',
    'executive-producer', 'guest', 'host', 'narrator', 'organizer', 'performer',
    'producer', 'script-writer', 'series-creator'))
_DATE_FIELDS = frozenset(('accessed', 'available-date', 'event-date', 'issued',
                         'original-date', 'submitted'))
_TEXT_FIELDS = frozenset(('title', 'title-short', 'container-title', 'container-title-short',
    'collection-title', 'original-title', 'reviewed-title', 'publisher', 'publisher-place',
    'original-publisher', 'original-publisher-place', 'event', 'event-title', 'event-place',
    'archive', 'archive-place', 'archive_location', 'archive-collection', 'call-number',
    'citation-key', 'collection-number', 'dimensions', 'division', 'DOI', 'edition',
    'genre', 'ISBN', 'ISSN', 'issue', 'jurisdiction', 'keyword', 'language', 'medium',
    'number', 'number-of-pages', 'number-of-volumes', 'page', 'page-first', 'PMCID',
    'PMID', 'references', 'reviewed-genre', 'scale', 'section', 'source', 'status',
    'supplement-number', 'URL', 'version', 'volume', 'volume-title', 'volume-title-short',
    'chapter-number', 'part-number', 'part-title', 'printing', 'authority'))
_TYPES = frozenset(('article', 'article-journal', 'article-magazine', 'article-newspaper',
    'bill', 'book', 'broadcast', 'chapter', 'classic', 'collection', 'dataset',
    'document', 'entry', 'entry-dictionary', 'entry-encyclopedia', 'event', 'figure',
    'graphic', 'hearing', 'interview', 'legal_case', 'legislation', 'manuscript',
    'map', 'motion_picture', 'musical_score', 'pamphlet', 'paper-conference',
    'patent', 'performance', 'periodical', 'personal_communication', 'post',
    'post-weblog', 'regulation', 'report', 'review', 'review-book', 'software',
    'song', 'speech', 'standard', 'thesis', 'treaty', 'webpage'))


class CitationError(ValueError):
    """Invalid citation data, unavailable capability or bounded processor error."""


class CitationCancelled(CitationError):
    """The caller cancelled the local operation before publication."""


@dataclass(frozen=True)
class StyleInfo:
    id: str
    title: str
    kind: str
    sha256: str
    source: str
    license: str


@dataclass(frozen=True)
class CitationRequest:
    reference_id: str
    locator: str | None = None
    label: str = 'page'
    locator_verified: bool = False
    prefix: str = ''
    suffix: str = ''
    suppress_author: bool = False


@dataclass(frozen=True)
class CitationCluster:
    items: tuple[CitationRequest, ...]
    id: str = ''
    note_index: int | None = None


@dataclass(frozen=True)
class BibliographyEntry:
    reference_ids: tuple[str, ...]
    text: str
    html: str

    @property
    def reference_id(self) -> str:
        return self.reference_ids[0]


@dataclass(frozen=True)
class RenderedCitation:
    cluster_id: str
    reference_ids: tuple[str, ...]
    text: str
    html: str
    note_index: int | None


@dataclass(frozen=True)
class FormattedCitations:
    style: StyleInfo
    locale: str
    bibliography: tuple[BibliographyEntry, ...]
    citations: tuple[RenderedCitation, ...]
    warnings: tuple[str, ...]
    attribution: str
    _interchange_json: str

    @property
    def interchange(self) -> dict:
        """An independent snapshot suitable for regeneration and DOCX embedding."""
        return json.loads(self._interchange_json)

    @property
    def processor(self) -> dict:
        return self.interchange['processor']

    @property
    def references(self) -> tuple[dict, ...]:
        return tuple(self.interchange['references'])


def _manifest() -> dict:
    try:
        return json.loads(_bounded_bytes(_ROOT / 'UPSTREAM.json', 128000, 'The installed citation manifest').decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise CitationError('The installed citation bundle is incomplete.') from exc


def available_styles() -> tuple[StyleInfo, ...]:
    return tuple(StyleInfo(**{key: entry[key] for key in StyleInfo.__dataclass_fields__})
                 for entry in _manifest()['styles'])


def _text(value: object, field: str, maximum: int = 200_000) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise CitationError(f'{field} must be bounded text.')
    if any(ord(c) < 32 and c not in '\n\t\r' for c in value):
        raise CitationError(f'{field} contains unsupported control characters.')
    return value


def _identifier(value: object, field: str) -> str:
    value = _text(value, field, 200)
    if not value.strip() or any(c.isspace() for c in value):
        raise CitationError(f'{field} must be a stable, nonblank identifier without spaces.')
    if value in ('__proto__', 'constructor', 'prototype'):
        raise CitationError(f'{field} uses a reserved runtime identifier.')
    return value


def _reference(value: Mapping) -> dict:
    if not isinstance(value, Mapping):
        raise CitationError('Each reference must be a CSL JSON object.')
    item = {'id': _identifier(value.get('id'), 'Reference ID')}
    kind = value.get('type')
    if kind not in _TYPES:
        raise CitationError('Each reference needs a recognized CSL type.')
    item['type'] = kind
    for field in _TEXT_FIELDS:
        if field in value and value[field] is not None:
            content = value[field]
            if isinstance(content, int) and not isinstance(content, bool):
                if abs(content) > 1_000_000_000:
                    raise CitationError(f'{field} exceeds the metadata budget.')
                content = str(content)
            item[field] = _text(content, field)
    for field in _NAME_FIELDS:
        if field not in value:
            continue
        names = value[field]
        if not isinstance(names, (list, tuple)) or len(names) > 1000:
            raise CitationError('Contributor lists must be bounded CSL name arrays.')
        result = []
        for name in names:
            if not isinstance(name, Mapping):
                raise CitationError('Each contributor must be a CSL name object.')
            cleaned = {}
            for key in ('family', 'given', 'literal', 'dropping-particle',
                        'non-dropping-particle', 'suffix'):
                if key in name:
                    cleaned[key] = _text(name[key], 'Contributor name', 2000)
            for key in ('comma-suffix', 'static-ordering', 'parse-names'):
                if key in name:
                    if type(name[key]) is not bool:
                        raise CitationError('Contributor flags must be boolean.')
                    cleaned[key] = name[key]
            if not any(cleaned.get(k) for k in ('family', 'given', 'literal')):
                raise CitationError('A contributor needs a name.')
            result.append(cleaned)
        item[field] = result
    for field in _DATE_FIELDS:
        if field not in value:
            continue
        date = value[field]
        if not isinstance(date, Mapping):
            raise CitationError('Dates must use CSL date objects.')
        result = {}
        if 'date-parts' in date:
            parts = date['date-parts']
            if not isinstance(parts, (list, tuple)) or not 1 <= len(parts) <= 2:
                raise CitationError('A date needs one date or a two-date interval.')
            for part in parts:
                if (not isinstance(part, (list, tuple)) or not 1 <= len(part) <= 3
                        or any(type(x) is not int for x in part)
                        or not -9999 <= part[0] <= 9999
                        or (len(part) > 1 and not 1 <= part[1] <= 12)
                        or (len(part) > 2 and not 1 <= part[2] <= 31)):
                    raise CitationError('A CSL date has invalid date parts.')
            result['date-parts'] = [list(x) for x in parts]
        for key in ('literal', 'raw'):
            if key in date:
                result[key] = _text(date[key], 'Date', 2000)
        if 'circa' in date:
            if type(date['circa']) not in (bool, int) or date['circa'] not in (0, 1):
                raise CitationError('The circa flag must be boolean.')
            result['circa'] = bool(date['circa'])
        if 'season' in date:
            if type(date['season']) is not int or not 1 <= date['season'] <= 4:
                raise CitationError('The CSL season must be between 1 and 4.')
            result['season'] = date['season']
        if result:
            item[field] = result
    return item


def _clusters(values: Iterable[CitationCluster | CitationRequest], ids: set[str]) -> list[dict]:
    result, used = [], set()
    for index, value in enumerate(values):
        if index >= _MAX_CLUSTERS:
            raise CitationError('The citation cluster budget was exceeded.')
        if isinstance(value, CitationRequest):
            value = CitationCluster((value,))
        if not isinstance(value, CitationCluster) or not 1 <= len(value.items) <= 100:
            raise CitationError('A cluster needs between 1 and 100 citation requests.')
        cluster_id = _identifier(value.id or f'citation-{index + 1}', 'Citation cluster ID')
        if cluster_id in used:
            raise CitationError('Citation cluster IDs must be unique.')
        used.add(cluster_id)
        if value.note_index is not None and (type(value.note_index) is not int
                                            or not 1 <= value.note_index <= 1_000_000):
            raise CitationError('A note number must be a positive integer.')
        requests, evidence = [], []
        for request in value.items:
            if not isinstance(request, CitationRequest) or request.reference_id not in ids:
                raise CitationError('A citation refers to an unknown stable reference ID.')
            if type(request.locator_verified) is not bool or type(request.suppress_author) is not bool:
                raise CitationError('Citation verification and author flags must be boolean.')
            if request.label not in _LABELS:
                raise CitationError('The citation locator label is not supported.')
            item = {'id': request.reference_id}
            if request.locator is not None:
                locator = _text(request.locator, 'Citation locator', 200)
                if not locator.strip() or not request.locator_verified:
                    raise CitationError('Verify a pinpoint locator against the source before formatting it.')
                item.update(locator=locator, label=request.label)
            for key in ('prefix', 'suffix'):
                content = _text(getattr(request, key), key, 1000)
                if content:
                    item[key] = content
            if request.suppress_author:
                item['suppress-author'] = True
            requests.append(item)
            evidence.append({'reference_id': request.reference_id,
                             'locator_verified': request.locator_verified if request.locator is not None else None})
        result.append({'id': cluster_id, 'items': requests, 'note_index': value.note_index,
                       'locator_evidence': evidence})
    return result


def _xml(raw: bytes, *, style: bool) -> str:
    if len(raw) > 1_000_000 or re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw, re.IGNORECASE):
        raise CitationError('CSL XML must be bounded and must not contain a DTD or entities.')
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=False, remove_comments=False)
    try:
        root = etree.fromstring(raw, parser)
    except (ValueError, etree.XMLSyntaxError) as exc:
        raise CitationError('The CSL XML is invalid.') from exc
    expected = _CSL + ('style' if style else 'locale')
    if root.tag != expected or sum(1 for _ in root.iter()) > 20_000:
        raise CitationError('The CSL XML has an invalid root or exceeds its element budget.')
    if style:
        if root.find(_CSL + 'citation') is None or root.find(_CSL + 'bibliography') is None:
            raise CitationError('Choose an independent CSL style containing citations and a bibliography.')
        macros = {}
        for node in root.findall(_CSL + 'macro'):
            name = node.get('name')
            if not name or name in macros:
                raise CitationError('CSL macros need distinct names.')
            macros[name] = {x.get('macro') for x in node.iter() if x.get('macro')}
        for node in root.iter():
            if node.get('macro') and node.get('macro') not in macros:
                raise CitationError('The CSL style refers to an unknown macro.')
        checked = set()
        def visit(name, active):
            if name in active or len(active) > 100:
                raise CitationError('Recursive or excessively nested CSL macros are not supported.')
            if name in checked:
                return
            for child in macros[name]:
                visit(child, active | {name})
            checked.add(name)
        for name in macros:
            visit(name, set())
    try:
        return raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise CitationError('CSL files must use UTF-8.') from exc


def _assets(style: str, style_path: Path | str | None, locale: str) -> tuple[StyleInfo, str, dict]:
    manifest = _manifest()
    if locale not in _LOCALES:
        raise CitationError('Choose an installed locale: en-US, uk-UA or sk-SK.')
    if style_path is None:
        entry = next((x for x in manifest['styles'] if x['id'] == style), None)
        if entry is None:
            raise CitationError('The selected CSL style is not installed.')
        raw = _bounded_bytes(_ROOT / 'styles' / entry['filename'], 1_000_000, 'The installed CSL style')
        if sha256(raw).hexdigest() != entry['sha256']:
            raise CitationError('The installed CSL style failed its integrity check.')
        info = StyleInfo(**{key: entry[key] for key in StyleInfo.__dataclass_fields__})
    else:
        from carr_library.durable_state import local_path, regular
        path = local_path(style_path, exists=True)
        if regular(path).st_size > 1_000_000:
            raise CitationError('The selected local style exceeds its size budget.')
        raw = _bounded_bytes(path, 1_000_000, 'The selected local CSL style')
        _xml(raw, style=True)
        node = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
        title = node.findtext(_CSL + 'info/' + _CSL + 'title') or path.stem
        kind = node.get('class', '')
        if kind not in ('note', 'in-text'):
            raise CitationError('The selected style needs a valid CSL citation class.')
        info = StyleInfo('local-' + sha256(raw).hexdigest()[:16], title, kind,
                         sha256(raw).hexdigest(), 'local:' + path.name,
                         'User-installed; consult style rights metadata')
    style_xml = _xml(raw, style=True)
    locales = {}
    for language in _LOCALES:
        filename = f'locales-{language}.xml'
        raw = _bounded_bytes(_ROOT / 'locales' / filename, 1_000_000, 'The installed CSL locale')
        if sha256(raw).hexdigest() != manifest['locales'][filename]:
            raise CitationError('An installed CSL locale failed its integrity check.')
        locales[language] = _xml(raw, style=False)
    return info, style_xml, locales


class _SafeHTML(HTMLParser):
    _allowed = frozenset(('i', 'em', 'b', 'strong', 'u', 'sup', 'sub'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.plain, self.stack = [], [], []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe', 'object'):
            self.blocked += 1
        if self.blocked:
            return
        if tag in self._allowed:
            self.parts.append('<' + tag + '>')
            self.stack.append(tag)
        elif tag == 'span' and any(k == 'style' and re.fullmatch(r'\s*font-variant:\s*small-caps;?\s*', v or '') for k, v in attrs):
            self.parts.append('<span style="font-variant:small-caps">')
            self.stack.append('span')
        elif tag == 'br':
            self.parts.append('<br>')
            self.plain.append('\n')
        elif tag == 'div' and any(k == 'class' and v == 'csl-right-inline' for k, v in attrs):
            # Numeric CSL bibliographies put their label and body into adjacent
            # divs. Preserve that separation in plain text and native Word runs.
            if self.plain and not self.plain[-1][-1:].isspace():
                self.parts.append(' ')
                self.plain.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'iframe', 'object') and self.blocked:
            self.blocked -= 1
            return
        if self.blocked:
            return
        if self.stack and tag == self.stack[-1]:
            self.stack.pop()
            self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(escape(data, quote=False))
            self.plain.append(data)


def safe_markup(value: str) -> tuple[str, str]:
    parser = _SafeHTML()
    parser.feed(_text(value, 'Formatted citation', 2_000_000))
    parser.close()
    while parser.stack:
        parser.parts.append('</' + parser.stack.pop() + '>')
    return ''.join(parser.plain).strip(), ''.join(parser.parts).strip()



_QUICKJS_BRIDGE = r'''
(function (data) {
    'use strict';
    var references = Object.create(null), warnings = [];
    data.references.forEach(function (item) { references[item.id] = item; });
    CSL.debug = function (message) {
        if (warnings.length < 100) warnings.push(String(message).slice(0, 2000));
    };
    var system = {
        retrieveLocale: function (language) {
            if (Object.prototype.hasOwnProperty.call(data.locales, language)) return data.locales[language];
            var prefix = String(language).split('-')[0];
            var match = Object.keys(data.locales).filter(function (name) {
                return name.split('-')[0] === prefix;
            })[0];
            return data.locales[match || 'en-US'];
        },
        retrieveItem: function (id) {
            if (!Object.prototype.hasOwnProperty.call(references, id)) throw new Error('Unknown citation reference.');
            return references[id];
        }
    };
    var engine = new CSL.Engine(system, data.style_xml, data.locale, true);
    engine.setOutputFormat('html');
    var ordered = [];
    data.clusters.forEach(function (cluster) {
        cluster.items.forEach(function (item) {
            if (ordered.indexOf(item.id) < 0) ordered.push(item.id);
        });
    });
    data.references.forEach(function (item) {
        if (ordered.indexOf(item.id) < 0) ordered.push(item.id);
    });
    engine.updateItems(ordered);
    var citations = [];
    data.clusters.forEach(function (cluster, index) {
        var citation = {citationID: cluster.id, citationItems: cluster.items,
            properties: {noteIndex: cluster.note_index || index + 1}};
        var updates = engine.appendCitationCluster(citation);
        updates.forEach(function (entry) { citations[entry[0]] = entry[1]; });
    });
    var bibliography = engine.makeBibliography();
    if (!bibliography) throw new Error('The style has no bibliography.');
    return JSON.stringify({bibliography: bibliography[1], parameters: bibliography[0],
        citations: citations, warnings: warnings, processor_version: CSL.PROCESSOR_VERSION});
})
'''

_QUICKJS_WORKER = r'''
from hashlib import sha256
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path
import sys
import types

def checked(name, expected, maximum):
    with Path(name).open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or sha256(raw).hexdigest() != expected:
        raise ValueError('Worker snapshot integrity failed.')
    return raw

try:
    import quickjs
    binding_version = version('quickjs-ng')
except (ImportError, PackageNotFoundError):
    sys.stderr.write('CARR_CSL_RUNTIME_UNAVAILABLE')
    sys.exit(3)

try:
    runtime_source = checked('context.py', sys.argv[1], 256000)
    processor = checked('processor.js', sys.argv[2], 2097152).decode('utf-8')
    bridge = checked('bridge.js', sys.argv[3], 65536).decode('utf-8')
    raw = sys.stdin.buffer.read(16000001)
    if len(raw) > 16000000:
        raise ValueError('Input budget exceeded.')
    encoded = raw.decode('utf-8')
    module = types.ModuleType('carr_csl_context_snapshot')
    module.__file__ = str(Path('context.py').absolute())
    sys.modules[module.__name__] = module
    # Execute the captured source bytes, never a cached or re-read application module.
    exec(compile(runtime_source, '<carr-csl-context>', 'exec'), module.__dict__)
    budget = module.RenderBudget(max_input_bytes=16000000, max_output_bytes=16000000,
        memory_bytes=512 * 1024 * 1024, cpu_seconds=float(sys.argv[4]))
    output = module._evaluate_processor(processor, bridge, encoded, budget)
    result = json.loads(output)
    result['_binding_version'] = binding_version
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(serialized) > 16000000:
        raise ValueError('Output budget exceeded.')
    sys.stdout.buffer.write(serialized)
except BaseException:
    sys.stderr.write('CARR_CSL_WORKER_FAILED')
    sys.exit(2)
'''


def _bounded_bytes(path: Path, maximum: int, label: str) -> bytes:
    try:
        with path.open('rb') as stream:
            raw = stream.read(maximum + 1)
    except OSError as exc:
        raise CitationError(label + ' is unavailable; repair or select the local resource.') from exc
    if len(raw) > maximum:
        raise CitationError(label + ' exceeds its byte budget.')
    return raw


def _processor_bytes() -> bytes:
    manifest = _manifest()
    expected = '8d80b2d4ad5d249178ddfa4c535bb72bbd1883521a88e9362686558e2f82029d'
    raw = _bounded_bytes(_ROOT / 'vendor' / 'citeproc.js', 2 * 1024 * 1024, 'The installed CSL processor')
    if manifest.get('processor', {}).get('sha256') != expected or sha256(raw).hexdigest() != expected:
        raise CitationError('The installed citation processor failed its integrity check.')
    return raw


def _node_binary(value: Path | str | None) -> str:
    if value is None or not str(value).strip() or not Path(value).is_absolute():
        raise CitationError('Choose an explicit absolute local Node.js executable for compatibility mode.')
    from carr_library.durable_state import local_path, regular
    try:
        path = local_path(value, exists=True)
        regular(path)
        if not path.is_absolute():
            raise ValueError('The executable must be an absolute local path.')
    except (OSError, ValueError) as exc:
        raise CitationError('The selected local Node.js executable is unavailable or unsafe.') from exc
    return str(path)


def _executable_digest(binary: str) -> str:
    maximum = 256 * 1024 * 1024
    total, digest = 0, sha256()
    try:
        with Path(binary).open('rb') as stream:
            while block := stream.read(min(1024 * 1024, maximum + 1 - total)):
                total += len(block)
                if total > maximum:
                    raise CitationError('The selected Node.js executable exceeds its byte budget.')
                digest.update(block)
    except OSError as exc:
        raise CitationError('The selected Node.js executable could not be inspected.') from exc
    return digest.hexdigest()


def _decode_result(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate result key.')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('Nonfinite result number.')
    try:
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError) as exc:
        raise CitationError('The citation processor returned invalid output.') from exc
    if type(result) is not dict:
        raise CitationError('The citation processor returned an invalid result shape.')
    return result


def _run(payload: dict, *, node_path: Path | str | None, cancelled: Callable[[], bool] | None,
         timeout: float) -> dict:
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 60:
        raise CitationError('The processor timeout must be between 0.1 and 60 seconds.')
    processor = _processor_bytes()
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > _MAX_BYTES:
        raise CitationError('The combined CSL input exceeds its processing budget.')
    env = {k: v for k, v in os.environ.items() if k in (
        'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
        'LANG', 'LC_ALL', 'HOME', 'USERPROFILE')}
    with retained_context(tempfile.TemporaryDirectory(prefix='carr-citations-')) as directory:
        directory = Path(directory)
        env.update(TEMP=str(directory), TMP=str(directory), TMPDIR=str(directory))
        if node_path is None:
            from carr_library.citations import csl as shared_runtime
            # Use the same bounded Context implementation as the existing CSL API.
            source = _bounded_bytes(Path(shared_runtime.__file__), 256000, 'The installed QuickJS context')
            bridge = _QUICKJS_BRIDGE.encode('utf-8')
            source_hash, bridge_hash = sha256(source).hexdigest(), sha256(bridge).hexdigest()
            (directory / 'context.py').write_bytes(source)
            (directory / 'processor.js').write_bytes(processor)
            (directory / 'bridge.js').write_bytes(bridge)
            data = {**payload, 'style_xml': shared_runtime._xml_json(payload['style_xml']),
                    'locales': {name: shared_runtime._xml_json(value) for name, value in payload['locales'].items()}}
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if len(raw) > _MAX_BYTES:
                raise CitationError('The combined CSL input exceeds its processing budget.')
            binary = Path(sys.executable) if sys.executable else None
            if binary is None or not binary.is_absolute() or not binary.is_file():
                raise CitationError('The installed Python/QuickJS runtime is unavailable.')
            # Keep the active venv interpreter spelling; resolving its symlink
            # would select a different environment on POSIX.
            command = [str(binary), '-I', '-B', '-c', _QUICKJS_WORKER,
                       source_hash, sha256(processor).hexdigest(), bridge_hash, str(float(timeout))]
            runtime = {'name': 'QuickJS', 'binding': 'quickjs-ng', 'source_sha256': source_hash,
                       'bridge_sha256': bridge_hash, 'processor_sha256': sha256(processor).hexdigest(),
                       'memory_bytes': 512 * 1024 * 1024, 'stack_bytes': 4 * 1024 * 1024}
        else:
            binary = _node_binary(node_path)
            worker = _bounded_bytes(_ROOT / 'csl_worker.cjs', 65536, 'The installed Node compatibility worker')
            (directory / 'csl_worker.cjs').write_bytes(worker)
            (directory / 'vendor').mkdir()
            (directory / 'vendor' / 'citeproc.js').write_bytes(processor)
            (directory / 'UPSTREAM.json').write_text(json.dumps(_manifest(), allow_nan=False), encoding='utf-8')
            command = [binary, '--max-old-space-size=256', '--disable-proto=throw',
                       str(directory / 'csl_worker.cjs')]
            runtime = {'name': 'Node.js', 'mode': 'explicit compatibility',
                       'source_sha256': sha256(worker).hexdigest(),
                       'processor_sha256': sha256(processor).hexdigest(),
                       'executable_sha256': _executable_digest(binary)}
        if cancelled and cancelled():
            raise CitationCancelled('Citation formatting was cancelled.')
        (directory / 'input.json').write_bytes(raw)
        output_path, error_path = directory / 'output.json', directory / 'error.txt'
        with retained_context(ExitStack()) as streams:
            source = streams.enter_context((directory / 'input.json').open('rb'))
            output = streams.enter_context(output_path.open('wb'))
            errors = streams.enter_context(error_path.open('wb'))
            # Keep this adapter's fixed file protocol and limits. The existing
            # process owner also drains a venv redirector's native descendants
            # before these streams or their private staging can be released.
            with ExitStack() as worker:
                try:
                    process = worker.enter_context(owned_process(command, stdin=source,
                        stdout=output, stderr=errors, cwd=directory, env=env, shell=False))
                except OSError as exc:
                    raise CitationError('The selected local citation runtime could not be started.') from exc
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if cancelled and cancelled():
                        raise CitationCancelled('Citation formatting was cancelled.')
                    if time.monotonic() > deadline:
                        raise CitationError('The local citation processor exceeded its time budget.')
                    if output_path.stat().st_size > _MAX_BYTES or error_path.stat().st_size > 1_000_000:
                        raise CitationError('The local citation processor exceeded its output budget.')
                    time.sleep(0.025)
            if cancelled and cancelled():
                raise CitationCancelled('Citation formatting was cancelled.')
            if output_path.stat().st_size > _MAX_BYTES or error_path.stat().st_size > 1_000_000:
                raise CitationError('The local citation processor exceeded its output budget.')
            if process.returncode:
                if node_path is None and process.returncode == 3:
                    raise CitationError('The installed QuickJS citation runtime is unavailable; repair the academic runtime.')
                raise CitationError('The local CSL processor could not finish within its memory/time budget or the style is incompatible.')
        result = _decode_result(_bounded_bytes(output_path, _MAX_BYTES, 'The citation output'))
        if node_path is None:
            binding_version = result.pop('_binding_version', None)
            if (not isinstance(binding_version, str) or not 1 <= len(binding_version) <= 100
                    or any(ord(char) < 33 for char in binding_version)):
                raise CitationError('The QuickJS runtime identity is invalid.')
            runtime['binding_version'] = binding_version
        result['runtime'] = runtime
        return result


def render_citations(csl_items: Sequence[Mapping], *, style: str = 'apa', locale: str = 'en-US',
                     citations: Iterable[CitationCluster | CitationRequest] = (),
                     style_path: Path | str | None = None, node_path: Path | str | None = None,
                     cancelled: Callable[[], bool] | None = None, timeout: float = 30) -> FormattedCitations:
    """Format references locally, with verified pinpoints supplied separately.

    Publication ``page`` metadata describes the publication extent and never
    becomes a citation locator. Omitted source fields stay omitted. Private
    notes, abstracts and non-CSL fields are excluded from the processing copy.
    Caller-owned dictionaries and source records are never mutated.
    """
    if not isinstance(csl_items, (list, tuple)) or not 1 <= len(csl_items) <= _MAX_REFERENCES:
        raise CitationError('Supply between 1 and 1000 CSL JSON references.')
    references = [_reference(item) for item in csl_items]
    ids = {x['id'] for x in references}
    if len(ids) != len(references):
        raise CitationError('Reference IDs must be unique.')
    clusters = _clusters(citations, ids)
    info, style_xml, locales = _assets(style, style_path, locale)
    payload = {'references': references, 'style_xml': style_xml, 'locale': locale,
               'locales': locales, 'clusters': clusters}
    result = _run(payload, node_path=node_path, cancelled=cancelled, timeout=timeout)
    manifest = _manifest()
    if result.get('processor_version') != manifest['processor']['version']:
        raise CitationError('The local processor version does not match the pinned bundle.')
    bibliography, rendered = [], []
    try:
        entries = result['bibliography']
        entry_ids = result['parameters']['entry_ids']
        if len(entries) != len(entry_ids) or len(result['citations']) != len(clusters):
            raise CitationError('The processor returned inconsistent citation counts.')
        for identifiers, markup in zip(entry_ids, entries):
            if not identifiers or any(x not in ids for x in identifiers):
                raise CitationError('The processor returned unknown bibliography IDs.')
            text, html = safe_markup(markup)
            bibliography.append(BibliographyEntry(tuple(identifiers), text, html))
        for cluster, markup in zip(clusters, result['citations']):
            text, html = safe_markup(markup)
            rendered.append(RenderedCitation(cluster['id'], tuple(x['id'] for x in cluster['items']),
                                            text, html, cluster['note_index']))
        warnings = tuple(_text(x, 'Processor warning', 2000) for x in result.get('warnings', ()))
        if result['parameters'].get('bibliography_errors'):
            warnings += ('The CSL processor reported an incomplete bibliography entry; review source metadata.',)
    except (KeyError, TypeError, AttributeError) as exc:
        raise CitationError('The citation processor returned an invalid result shape.') from exc
    interchange = {'schema': 1, 'references': references, 'clusters': clusters, 'locale': locale,
        'style': {k: getattr(info, k) for k in StyleInfo.__dataclass_fields__},
        'processor': manifest['processor'], 'runtime': result.get('runtime', {}), 'attribution': manifest['attribution'],
        'bibliography_parameters': {k: result['parameters'].get(k) for k in (
            'hangingindent', 'linespacing', 'entryspacing', 'second-field-align')},
        'bibliography': [{'reference_ids': list(x.reference_ids), 'text': x.text, 'html': x.html} for x in bibliography],
        'citations': [{'cluster_id': x.cluster_id, 'reference_ids': list(x.reference_ids), 'text': x.text,
                       'html': x.html, 'note_index': x.note_index} for x in rendered], 'warnings': list(warnings)}
    return FormattedCitations(info, locale, tuple(bibliography), tuple(rendered), warnings,
                              manifest['attribution'], json.dumps(interchange, ensure_ascii=False,
                                                                  sort_keys=True, allow_nan=False))
