"""Offline CSL formatting with the pinned, unmodified citeproc-js processor.

Only CSL JSON and XML cross the fixed worker boundary. Neither metadata nor
style XML is interpreted as JavaScript. Node is an explicit local dependency;
this module never downloads an engine, a style, a locale or a reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Callable, Iterable, Mapping, Sequence

from lxml import etree

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
        return json.loads((_ROOT / 'UPSTREAM.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CitationError('The installed citation bundle is incomplete.') from exc


def citation_attribution() -> str:
    """Read the installed credit without discovering or launching a runtime."""
    return _text(_manifest()['attribution'], 'Citation attribution', 2048)


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
        raw = (_ROOT / 'styles' / entry['filename']).read_bytes()
        if sha256(raw).hexdigest() != entry['sha256']:
            raise CitationError('The installed CSL style failed its integrity check.')
        info = StyleInfo(**{key: entry[key] for key in StyleInfo.__dataclass_fields__})
    else:
        from carr_library.durable_state import local_path, regular
        path = local_path(style_path, exists=True)
        if regular(path).st_size > 1_000_000:
            raise CitationError('The selected local style exceeds its size budget.')
        raw = path.read_bytes()
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
        raw = (_ROOT / 'locales' / filename).read_bytes()
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


def _node_binary(value: Path | str | None) -> str:
    path = Path(value) if value else Path(shutil.which('node') or '')
    if not str(path) or str(path) == '.' or not path.is_file():
        raise CitationError('Local citation formatting requires an installed Node.js 18 or newer runtime.')
    return str(path.resolve())


def _run(payload: dict, *, node_path: Path | str | None, cancelled: Callable[[], bool] | None,
         timeout: float) -> dict:
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= timeout <= 60:
        raise CitationError('The processor timeout must be between 0.1 and 60 seconds.')
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > _MAX_BYTES:
        raise CitationError('The combined CSL input exceeds its processing budget.')
    binary = _node_binary(node_path)
    env = {k: v for k, v in os.environ.items() if k in (
        'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'TMPDIR',
        'LANG', 'LC_ALL', 'HOME', 'USERPROFILE')}
    with tempfile.TemporaryDirectory(prefix='carr-citations-') as directory:
        directory = Path(directory)
        (directory / 'input.json').write_bytes(raw)
        output_path, error_path = directory / 'output.json', directory / 'error.txt'
        with (directory / 'input.json').open('rb') as source, output_path.open('wb') as output, error_path.open('wb') as errors:
            try:
                process = subprocess.Popen([binary, '--max-old-space-size=256', '--disable-proto=throw',
                    str(_ROOT / 'csl_worker.cjs')], stdin=source, stdout=output, stderr=errors,
                    cwd=directory, env=env, shell=False, start_new_session=os.name != 'nt',
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
            except OSError as exc:
                raise CitationError('The configured local Node.js runtime could not be started.') from exc
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if cancelled and cancelled():
                        raise CitationCancelled('Citation formatting was cancelled.')
                    if time.monotonic() > deadline:
                        raise CitationError('The local citation processor exceeded its time budget.')
                    if output_path.stat().st_size > _MAX_BYTES or error_path.stat().st_size > 1_000_000:
                        raise CitationError('The local citation processor exceeded its output budget.')
                    time.sleep(0.025)
            finally:
                if process.poll() is None:
                    if os.name != 'nt':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait(timeout=5)
            if process.returncode:
                raise CitationError('The local CSL processor could not format this input; check the runtime and style.')
        if output_path.stat().st_size > _MAX_BYTES:
            raise CitationError('The local citation processor exceeded its output budget.')
        try:
            return json.loads(output_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise CitationError('The citation processor returned invalid output.') from exc


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
        'processor': manifest['processor'], 'attribution': manifest['attribution'],
        'bibliography_parameters': {k: result['parameters'].get(k) for k in (
            'hangingindent', 'linespacing', 'entryspacing', 'second-field-align')},
        'bibliography': [{'reference_ids': list(x.reference_ids), 'text': x.text, 'html': x.html} for x in bibliography],
        'citations': [{'cluster_id': x.cluster_id, 'reference_ids': list(x.reference_ids), 'text': x.text,
                       'html': x.html, 'note_index': x.note_index} for x in rendered], 'warnings': list(warnings)}
    return FormattedCitations(info, locale, tuple(bibliography), tuple(rendered), warnings,
                              manifest['attribution'], json.dumps(interchange, ensure_ascii=False,
                                                                  sort_keys=True, allow_nan=False))
