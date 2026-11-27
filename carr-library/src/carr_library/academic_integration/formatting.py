"""Citation-document compatibility using the canonical bounded QuickJS formatter.

The existing typed citation requests and OOXML snapshots remain supported.
Rendering delegates to carr_library.citations.csl; no external executable,
second processor bundle, network fallback or independent style registry exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping, Sequence

from lxml import etree

from ..citations import csl

_ROOT = csl._ROOT
_CSL = '{http://purl.org/net/xbiblio/csl}'
_MAX_BYTES = 16_000_000
_MAX_REFERENCES = 1000
_MAX_CLUSTERS = 5000
_LOCALES = csl.LOCALES
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
    """Identity of the one installed formatter, including its pinned resources.

    These hashes detect changed local source/style identities. They are not a
    signature or independent authorization for a quotation or locator.
    """
    processor = {
        'name': 'citeproc-js', 'version': csl.PROCESSOR_REPORTED_VERSION,
        'package_version': csl.PROCESSOR_PACKAGE_VERSION,
        'sha256': csl._HASHES['_vendor_citeproc/citeproc.js'],
        'chosen_license': 'CPAL-1.0', 'runtime': 'quickjs-ng',
        'document_adapter': 2,
        'bridge_sha256': sha256(csl._BRIDGE.encode('utf-8')).hexdigest(),
        'resources': dict(sorted(csl._HASHES.items())),
    }
    styles = []
    for item in csl.available_styles():
        styles.append(dict(id=item.id, title=item.title, kind=item.kind,
            filename=item.filename, sha256=csl._HASHES['styles/' + item.filename],
            source='bundled:citations/styles/' + item.filename, license='CC-BY-SA-3.0'))
    return {'processor': processor, 'styles': styles, 'attribution': csl.ATTRIBUTION}


def processor_attribution() -> str:
    return csl.ATTRIBUTION


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


def _assets(style: str, style_path: Path | str | None, locale: str):
    if locale not in csl.LOCALES:
        raise CitationError('Choose an installed locale: ' + ', '.join(csl.LOCALES) + '.')
    try:
        if style_path is None:
            entry = next((row for row in _manifest()['styles'] if row['id'] == style), None)
            if entry is None:
                raise CitationError('The selected CSL style is not installed. Choose a current named style.')
            raw = csl._resource('styles/' + entry['filename']).encode('utf-8')
            info = StyleInfo(**{key: entry[key] for key in StyleInfo.__dataclass_fields__})
            selected = style
        else:
            from ..durable_state import local_path, regular
            path = local_path(style_path, exists=True)
            before = regular(path)
            if before.st_size > csl.MAX_CUSTOM_STYLE_BYTES:
                raise CitationError('The selected local style exceeds its size budget.')
            with path.open('rb') as stream:
                raw = stream.read(csl.MAX_CUSTOM_STYLE_BYTES + 1)
            after = regular(path)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise CitationError('The selected style changed while reading it.')
            selected = csl.load_custom_style(raw)
            info = StyleInfo(selected.info.id, selected.info.title, selected.info.kind,
                             selected.sha256, 'local:' + path.name,
                             'User-installed; consult style rights metadata')
        # Bibliography paragraph layout comes from the same validated style
        # snapshot. The text and typography still come only from csl.render.
        root = etree.fromstring(raw, etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))
        bibliography = root.find(_CSL + 'bibliography')
        parameters = {}
        if bibliography is not None:
            parameters['hangingindent'] = bibliography.get('hanging-indent') == 'true'
            parameters['second-field-align'] = bibliography.get('second-field-align')
            for attribute, key in (('line-spacing', 'linespacing'), ('entry-spacing', 'entryspacing')):
                value = bibliography.get(attribute)
                if value is not None and re.fullmatch(r'[0-9]{1,3}', value):
                    parameters[key] = int(value)
        return info, selected, parameters
    except CitationError:
        raise
    except (ValueError, RuntimeError, OSError, etree.XMLSyntaxError) as exc:
        raise CitationError(str(exc)) from exc


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


def _run(references, clusters, selected_style, locale, *, cancelled, timeout):
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    try:
        result = csl.render(references, style=selected_style,
            citations=[cluster['items'] for cluster in clusters], locale=locale,
            output='html', budget=csl.RenderBudget(max_records=_MAX_REFERENCES,
                max_citations=_MAX_CLUSTERS, max_input_bytes=8_000_000,
                max_output_bytes=_MAX_BYTES, cpu_seconds=timeout))
    except (ValueError, RuntimeError) as exc:
        if cancelled and cancelled():
            raise CitationCancelled('Citation formatting was cancelled.') from exc
        raise CitationError(str(exc)) from exc
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    return result


def render_citations(csl_items: Sequence[Mapping], *, style: str = 'apa', locale: str = 'en-US',
                     citations: Iterable[CitationCluster | CitationRequest] = (),
                     style_path: Path | str | None = None, node_path: Path | str | None = None,
                     cancelled: Callable[[], bool] | None = None, timeout: float = 30) -> FormattedCitations:
    """Format a source-immutable document through the existing local CSL service.

    A locator remains an explicit caller assertion. Publication page extents
    never become pinpoints. Cancellation is checked before and after the
    bounded QuickJS evaluation and always before export publication.
    """
    if cancelled is not None and not callable(cancelled):
        raise CitationError('Cancellation must be a callable.')
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    if node_path is not None:
        raise CitationError('External Node.js runtimes are no longer used. Use the installed QuickJS citation service.')
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 60:
        raise CitationError('The processor timeout must be between 0.1 and 60 seconds.')
    if not isinstance(csl_items, (list, tuple)) or not 1 <= len(csl_items) <= _MAX_REFERENCES:
        raise CitationError('Supply between 1 and 1000 CSL JSON references.')
    references = [_reference(item) for item in csl_items]
    ids = {item['id'] for item in references}
    if len(ids) != len(references):
        raise CitationError('Reference IDs must be unique.')
    clusters = _clusters(citations, ids)
    info, selected_style, parameters = _assets(style, style_path, locale)
    if info.kind == 'note' and any(cluster['note_index'] not in (None, index + 1)
                                  for index, cluster in enumerate(clusters)):
        raise CitationError('Native endnote documents need consecutive note numbers starting at 1.')
    rendered = _run(references, clusters, selected_style, locale,
                    cancelled=cancelled, timeout=timeout)
    if rendered.style_sha256 != info.sha256:
        raise CitationError('The selected style identity changed while formatting.')
    bibliography, output = [], []
    for identifiers, entry in zip(rendered.entry_ids, rendered.entries, strict=True):
        text, html = safe_markup(entry)
        bibliography.append(BibliographyEntry(identifiers, text, html))
    for cluster, entry in zip(clusters, rendered.citations, strict=True):
        text, html = safe_markup(entry)
        output.append(RenderedCitation(cluster['id'], tuple(item['id'] for item in cluster['items']),
                                       text, html, cluster['note_index']))
    warnings = tuple(rendered.warnings)
    manifest = _manifest()
    interchange = {'schema': 1, 'references': references, 'clusters': clusters, 'locale': locale,
        'style': {key: getattr(info, key) for key in StyleInfo.__dataclass_fields__},
        'processor': manifest['processor'], 'attribution': rendered.attribution,
        'bibliography_parameters': parameters,
        'bibliography': [{'reference_ids': list(row.reference_ids), 'text': row.text, 'html': row.html}
                         for row in bibliography],
        'citations': [{'cluster_id': row.cluster_id, 'reference_ids': list(row.reference_ids),
                       'text': row.text, 'html': row.html, 'note_index': row.note_index}
                      for row in output], 'warnings': list(warnings)}
    encoded = json.dumps(interchange, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode('utf-8')) > _MAX_BYTES:
        raise CitationError('The citation document snapshot exceeds its size budget.')
    return FormattedCitations(info, locale, tuple(bibliography), tuple(output),
                              warnings, rendered.attribution, encoded)
