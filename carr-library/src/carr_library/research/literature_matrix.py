"""Offline comparison of catalog metadata and stored research-link counts.

The caller supplies one authorized CatalogAnalysis snapshot. No source text,
notes, files, parsers or network services are consulted. These exports cannot
establish quotation, source-byte or printed-page authenticity.
"""
import csv
import io
import re

from ..catalog.insights import (CatalogAnalysis, SourceInsight, ISSUE_LABELS,
                                MAX_METADATA_BYTES, MAX_SOURCES, NOTICE, display_value)
from .evidence_export import _encoded


MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_LIST_ITEMS = 10_000
# Visible topic paths can contain up to 256 named ancestors. They are longer
# than an individual catalog field; the shared total metadata budget still caps
# the complete report before rendering starts.
MAX_FIELD_CHARACTERS = 1_000_000
_MARKDOWN_ESCAPES = str.maketrans({char: '\\' + char for char in r'\`*_{}[]()!#<>|&~'})
_COUNTS = ('text_units', 'indexed_units', 'physical_pages', 'logical_segments',
           'recorded_review_pages', 'evidence_count')
_LISTS = ('authors', 'collections', 'topics', 'tags', 'issues')
_FIELDS = (
    ('title_json', 'Source title', 'title'),
    ('authors_json', 'Authors in source order', 'authors'),
    ('language_json', 'Language', 'language'),
    ('edition_json', 'Edition', 'edition'),
    ('volume_json', 'Volume', 'volume'),
    ('collections_json', 'Active collections', 'collections'),
    ('text_units', 'Active text units', 'text_units'),
    ('indexed_units', 'Indexed text units', 'indexed_units'),
    ('physical_pages', 'Physical PDF pages', 'physical_pages'),
    ('logical_segments', 'Unpaginated logical segments', 'logical_segments'),
    ('recorded_review_pages', 'Recorded PDF page reviews', 'recorded_review_pages'),
    ('evidence_count', 'Stored evidence links', 'evidence_count'),
    ('topics_json', 'Visible research topics', 'topics'),
    ('tags_json', 'Evidence tags', 'tags'),
    ('checklist_json', 'Catalog checklist', 'issues'),
)
_TRACE_FIELDS = (
    ('edition_id_json', 'Source edition ID', 'edition_id'),
    ('asset_sha256_json', 'Retained asset SHA256 identifier', 'asset_sha256'),
    ('revision_id_json', 'Active revision ID', 'revision'),
)
def _check_analysis(analysis, check):
    if not isinstance(analysis, CatalogAnalysis):
        raise ValueError('An authorized catalog analysis is required.')
    used = 0

    def text(value, *, optional=False, identity=False):
        nonlocal used
        check()
        if optional and value is None:
            return
        if not isinstance(value, str) or len(value) > (200 if identity else MAX_FIELD_CHARACTERS):
            raise ValueError('Invalid or oversized matrix text field.')
        if identity and (not value.strip() or any(ord(char) < 32 or 127 <= ord(char) <= 159
                                                for char in value)):
            raise ValueError('Invalid matrix source identity.')
        try:
            used += len(value.encode('utf-8'))
        except UnicodeError as exc:
            raise ValueError('Matrix text must contain valid Unicode.') from exc
        if used > MAX_METADATA_BYTES:
            raise ValueError('Matrix metadata exceeds the report limit. Choose a smaller collection.')

    def text_list(values, *, limit=MAX_LIST_ITEMS):
        if not isinstance(values, tuple) or len(values) > limit:
            raise ValueError('Invalid or oversized matrix text list.')
        for value in values:
            text(value)

    text(analysis.workspace, identity=True)
    text(analysis.workspace_name)
    text(analysis.collection_id, optional=True, identity=True)
    text(analysis.collection_name, optional=True)
    if (analysis.collection_id is None) != (analysis.collection_name is None):
        raise ValueError('The matrix collection scope is incomplete.')
    text_list(analysis.warnings)
    if not isinstance(analysis.sources, tuple) or len(analysis.sources) > MAX_SOURCES:
        raise ValueError('Too many or invalid sources for this matrix.')
    seen = set()
    for source in analysis.sources:
        check()
        if not isinstance(source, SourceInsight):
            raise ValueError('Invalid matrix source record.')
        text(source.edition_id, identity=True)
        if source.edition_id in seen:
            raise ValueError('Duplicate source edition in this matrix.')
        seen.add(source.edition_id)
        text(source.revision, optional=True, identity=True)
        text(source.asset_sha256)
        if re.fullmatch(r'[0-9a-f]{64}', source.asset_sha256) is None:
            raise ValueError('Invalid retained asset identifier in this matrix.')
        for name in ('title', 'language', 'edition', 'volume'):
            text(getattr(source, name), optional=True)
        for name in _LISTS:
            text_list(getattr(source, name), limit=100 if name == 'authors' else MAX_LIST_ITEMS)
        for name in _COUNTS:
            value = getattr(source, name)
            if type(value) is not int or not 0 <= value <= 2_147_483_647:
                raise ValueError('Invalid stored count in this matrix.')
        if (source.indexed_units > source.text_units
                or source.physical_pages + source.logical_segments != source.text_units
                or source.recorded_review_pages > source.physical_pages):
            raise ValueError('Inconsistent stored counts in this matrix.')


class _Output(io.StringIO):
    def __init__(self, check):
        super().__init__(newline='')
        self.check, self.size = check, 0

    def write(self, value):
        self.check()
        self.size += len(value.encode('utf-8'))
        if self.size > MAX_OUTPUT_BYTES:
            raise ValueError('Matrix output exceeds the report limit. Choose a smaller collection.')
        return super().write(value)


def _source_value(source, field):
    value = getattr(source, field)
    if field == 'issues':
        return tuple(ISSUE_LABELS.get(issue, issue) for issue in value)
    return value


def _csv(analysis, include_trace, output, check):
    fields = _FIELDS + (_TRACE_FIELDS if include_trace else ())
    heading = ('record_type', 'format', 'text_encoding', 'scope_kind',
               'workspace_name_json', 'collection_name_json', 'notice_json',
               'source_count', 'warnings_json', 'source_number')
    if include_trace:
        heading += ('workspace_id_json', 'collection_id_json')
    writer = csv.DictWriter(output, fieldnames=heading + tuple(item[0] for item in fields),
                            quoting=csv.QUOTE_ALL, lineterminator='\n')
    writer.writeheader()
    scope = dict(format='csv', text_encoding='json-v1',
                 scope_kind='library' if analysis.collection_id is None else 'collection',
                 workspace_name_json=_encoded(analysis.workspace_name),
                 collection_name_json=_encoded(analysis.collection_name))
    if include_trace:
        scope.update(workspace_id_json=_encoded(analysis.workspace),
                     collection_id_json=_encoded(analysis.collection_id))
    # A distinct metadata record keeps scope and warnings even for an empty
    # library. Consumers select record_type=source for comparison rows.
    writer.writerow(dict(scope, record_type='report', notice_json=_encoded(NOTICE),
                         source_count=len(analysis.sources), warnings_json=_encoded(analysis.warnings)))
    for number, source in enumerate(analysis.sources, 1):
        check()
        record = dict(scope, record_type='source', source_number=number)
        for key, _label, field in fields:
            check()
            value = _source_value(source, field)
            record[key] = value if field in _COUNTS else _encoded(value)
        writer.writerow(record)


def _display(value):
    """Use the catalog's readable text convention, never quotation formatting."""
    if isinstance(value, tuple):
        return '; '.join(display_value(item) for item in value) if value else 'None'
    result = display_value(value)
    return result if result.strip() else 'Not recorded'


def _markdown_value(value):
    # GFM backslash escapes cover ASCII punctuation, including table pipes,
    # link/image syntax, HTML delimiters, backticks and existing backslashes.
    # The shared catalog display helper neutralizes line breaks and controls.
    return _display(value).translate(_MARKDOWN_ESCAPES)


def _heading(analysis, format, output, include_trace):
    markdown = format == 'markdown'
    encode = _markdown_value if markdown else _display
    output.write(('# ' if markdown else '') + 'Literature review matrix\n\n')
    output.write('Library: ' + encode(analysis.workspace_name) + '\n\n')
    output.write(('Scope: all sources in this library.' if analysis.collection_id is None
                  else 'Collection: ' + encode(analysis.collection_name)) + '\n\n')
    output.write(f'Source editions: {len(analysis.sources)}.\n\n')
    output.write(NOTICE + '\n\n')
    for warning in analysis.warnings:
        output.write('Report warning: ' + encode(warning) + '\n\n')
    if include_trace:
        output.write('Library ID: ' + encode(analysis.workspace) + '\n\n')
        if analysis.collection_id is not None:
            output.write('Collection ID: ' + encode(analysis.collection_id) + '\n\n')


def _markdown(analysis, include_trace, output, check):
    _heading(analysis, 'markdown', output, include_trace)
    fields = _FIELDS + (_TRACE_FIELDS if include_trace else ())
    output.write('| Source number | ' + ' | '.join(label for _key, label, _field in fields) + ' |\n')
    output.write('| --- | ' + ' | '.join('---' for _ in fields) + ' |\n')
    for number, source in enumerate(analysis.sources, 1):
        check()
        cells = [str(number)]
        for _key, _label, field in fields:
            check()
            value = _source_value(source, field)
            cells.append(str(value) if field in _COUNTS else _markdown_value(value))
        output.write('| ' + ' | '.join(cells) + ' |\n')
    if not analysis.sources:
        output.write('\nNo sources in this scope.\n')


def _text(analysis, include_trace, output, check):
    _heading(analysis, 'text', output, include_trace)
    fields = _FIELDS + (_TRACE_FIELDS if include_trace else ())
    for number, source in enumerate(analysis.sources, 1):
        check()
        output.write(f'Source {number}\n')
        for _key, label, field in fields:
            value = _source_value(source, field)
            output.write(label + ': ' + (str(value) if field in _COUNTS else _display(value)) + '\n')
        output.write('\n')
    if not analysis.sources:
        output.write('No sources in this scope.\n')


def render_literature_matrix(analysis, *, format='csv', include_trace=False,
                             cancelled=lambda: False):
    """Return a bounded report string; file publication belongs to the caller.

    Source order is retained. CSV text cells use the project's reversible JSON
    representation so no untrusted text can become a leading spreadsheet
    formula. Its first record_type=report row carries scope and notices;
    subsequent source rows form the matrix. Markdown and plain text use readable
    catalog labels. Physical pages and logical segments stay distinct. Optional
    trace adds IDs and the retained asset digest identifier, never filesystem
    paths. There is no notes/quotes option.
    """
    if format not in ('csv', 'markdown', 'text'):
        raise ValueError('Choose CSV, Markdown or plain text for the literature matrix.')
    if type(include_trace) is not bool or not callable(cancelled):
        raise ValueError('Invalid matrix export options.')

    def check():
        if cancelled():
            raise InterruptedError('Matrix export cancelled; no report was returned.')

    check()
    _check_analysis(analysis, check)
    output = _Output(check)
    {'csv': _csv, 'markdown': _markdown, 'text': _text}[format](analysis, include_trace, output, check)
    check()
    return output.getvalue()
