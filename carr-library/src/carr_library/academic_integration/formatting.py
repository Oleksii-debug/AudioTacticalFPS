"""Source-bound document compatibility on the canonical offline QuickJS CSL core.

The document snapshot/typed-locator API is retained. Processor, styles, locales
and resource limits are supplied by carr_library.citations.csl; no alternate
Node worker, downloaded engine, or DOI provider is installed here.
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


from carr_library.citations import csl as _core

_ROOT = _core._ROOT
_CSL = '{http://purl.org/net/xbiblio/csl}'
_MAX_BYTES = 16_000_000
_MAX_REFERENCES = 1000
_MAX_CLUSTERS = 5000
_LOCALES = _core.LOCALES
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
    # This identity intentionally differs from historical Node snapshots even
    # though citeproc's reported engine version happens to be the same.
    processor = {'name': 'citeproc-js', 'version': _core.PROCESSOR_REPORTED_VERSION,
        'package_version': _core.PROCESSOR_PACKAGE_VERSION,
        'sha256': _core._HASHES['_vendor_citeproc/citeproc.js'],
        'runtime': 'quickjs-ng', 'runtime_version': '0.18.0.2',
        'chosen_license': 'CPAL-1.0'}
    return {'processor': processor, 'attribution': _core.ATTRIBUTION}


def available_styles() -> tuple[StyleInfo, ...]:
    return tuple(StyleInfo(info.id, info.title, info.kind,
        _core._HASHES['styles/' + info.filename],
        'https://www.zotero.org/styles/' + info.filename.removesuffix('.csl'),
        'CC-BY-SA-3.0') for info in _core.available_styles())


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


def _assets(style, style_path, locale):
    if locale not in _LOCALES:
        raise CitationError('Choose an installed locale: ' + ', '.join(_LOCALES) + '.')
    if style_path is None:
        # An old command name may select the current style, but the returned
        # identity names the current edition. Historical snapshots are rejected
        # before rendering by regeneration's exact processor/style checks.
        style = 'chicago-note' if style == 'chicago-notes' else style
        info = next((item for item in available_styles() if item.id == style), None)
        if info is None:
            raise CitationError('The selected CSL style is not installed.')
        return info, style
    from carr_library.durable_state import local_path, regular
    path = local_path(style_path, exists=True)
    if regular(path).st_size > _core.MAX_CUSTOM_STYLE_BYTES:
        raise CitationError('The selected local style exceeds its size budget.')
    with path.open('rb') as stream:
        raw = stream.read(_core.MAX_CUSTOM_STYLE_BYTES + 1)
    try:
        custom = _core.load_custom_style(raw)
    except (ValueError, RuntimeError) as exc:
        raise CitationError(str(exc)) from exc
    info = StyleInfo('local-' + custom.sha256[:16], custom.info.title, custom.info.kind,
                     custom.sha256, 'local:' + path.name,
                     'User-installed; consult style rights metadata')
    return info, custom


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


def _run(payload, *, node_path, cancelled, timeout):
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    if node_path is not None:
        raise CitationError('Citation formatting uses the installed QuickJS runtime; '
                            'an external Node.js executable cannot be selected.')
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 60:
        raise CitationError('The processor timeout must be between 0.1 and 60 seconds.')
    budget = _core.RenderBudget(max_records=_MAX_REFERENCES, max_citations=_MAX_CLUSTERS,
        max_input_bytes=_MAX_BYTES, max_output_bytes=_MAX_BYTES,
        memory_bytes=_core.RenderBudget().memory_bytes, cpu_seconds=timeout)
    try:
        result = _core.render(payload['references'], style=payload['style'],
            locale=payload['locale'], output='html', budget=budget,
            citations=[cluster['items'] for cluster in payload['clusters']],
            citation_context=[{'id': cluster['id'], 'note_index': cluster['note_index']}
                              for cluster in payload['clusters']])
    except (ValueError, RuntimeError) as exc:
        raise CitationError(str(exc)) from exc
    # QuickJS has a bounded CPU deadline, not an interruptible Python callback.
    # A stop during that call is observed before any result/publication escapes.
    if cancelled and cancelled():
        raise CitationCancelled('Citation formatting was cancelled.')
    return {'bibliography': result.entries, 'citations': result.citations,
        'parameters': dict(result.bibliography_parameters, entry_ids=result.entry_ids),
        'processor_version': result.processor_reported_version, 'warnings': result.warnings}


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
    info, selected_style = _assets(style, style_path, locale)
    payload = {'references': references, 'style': selected_style, 'locale': locale,
               'clusters': clusters}
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
