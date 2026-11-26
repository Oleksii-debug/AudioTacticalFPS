"""Bounded exports of current source-catalog metadata, in one read snapshot.

The host authorizes the workspace and owns the SQLite transaction. These
exports never read source text, historical quotations, research notes or files.
Missing bibliographic facts remain absent; each source edition is a separate
record, including editions with identical titles or original asset digests.
"""
from collections.abc import Callable, Iterator
import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata

from .membership import Catalog, _identity, _metadata
from .read_views import EditionView, _snapshot, browse_editions
from ..research.evidence_export import _bibtex_text, _encoded
from ..research._vendor_rispy.writer import RisWriter, dumps as ris_dumps


MAX_RECORDS = 50_000
MAX_EXPORT_BYTES = 32 * 1024 * 1024
PAGE_SIZE = 100
FORMATS = ('csl-json', 'ris', 'bibtex')
INVENTORY_HEADERS = (
    'text_encoding', 'source_id_json', 'workspace_id_json',
    'work_id_json', 'edition_id_json', 'asset_sha256_json',
    'title_json', 'authors_json', 'language_json', 'edition_json', 'volume_json',
)


class _BoundedOutput(io.StringIO):
    def __init__(self):
        super().__init__(newline='')
        self.bytes_written = 0

    def write(self, value):
        size = len(value.encode('utf-8', errors='strict'))
        if self.bytes_written + size > MAX_EXPORT_BYTES:
            raise ValueError('The catalog export is too large. Export a smaller collection.')
        self.bytes_written += size
        return super().write(value)


class _PlainRisWriter(RisWriter):
    """Use the existing MIT writer without its optional numbered preamble."""
    def set_header(self, count):
        return ''


def _check(cancelled):
    if cancelled():
        raise TimeoutError('Catalog export cancelled or timed out.')


def _catalog(db, workspace, collection, cancelled):
    if not isinstance(db, sqlite3.Connection):
        raise ValueError('A SQLite connection is required.')
    if type(workspace) is not str or (collection is not None and type(collection) is not str):
        raise ValueError('Invalid catalog scope.')
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    _identity(workspace)
    if collection is not None:
        _identity(collection)
    _check(cancelled)
    catalog = Catalog(db, workspace)
    _snapshot(catalog)
    if collection is not None:
        catalog._active_collection(collection)
    return catalog


def _validate(row):
    _identity(row.edition_id)
    _identity(row.work_id)
    if not isinstance(row.asset, str) or not re.fullmatch('[0-9a-f]{64}', row.asset):
        raise ValueError('Invalid catalog asset digest.')
    for value in (row.title, row.language, row.edition, row.volume):
        _metadata(value)
    if len(row.authors) > 100:
        raise ValueError('Invalid catalog author count.')
    for name in row.authors:
        _metadata(name)
        if not name or not name.strip():
            raise ValueError('Invalid catalog author.')


def _records(catalog, collection, cancelled) -> Iterator[EditionView]:
    count, after = 0, None
    while True:
        _check(cancelled)
        page = browse_editions(catalog, collection=collection, after=after, limit=PAGE_SIZE)
        _check(cancelled)
        if not page:
            return
        for row in page:
            _check(cancelled)
            count += 1
            if count > MAX_RECORDS:
                raise ValueError('Too many catalog sources. Export a smaller collection.')
            _validate(row)
            yield row
        after = page[-1].edition_id


def _source_id(workspace, edition_id):
    # Metadata, titles and collection membership must never change identity.
    identity = json.dumps([workspace, edition_id], ensure_ascii=True, separators=(',', ':'))
    return 'source_' + hashlib.sha256(identity.encode('ascii')).hexdigest()


def _csl(row, source_id):
    record = {'id': source_id, 'type': 'document'}
    for name in ('title', 'language', 'edition', 'volume'):
        value = getattr(row, name)
        if value is not None:
            record[name] = value
    if row.authors:
        record['author'] = [{'literal': name} for name in row.authors]
    return _encoded(record)


def _ris(row, source_id):
    record = {'type_of_reference': 'GEN', 'id': source_id}
    for name in ('title', 'language', 'edition', 'volume'):
        value = getattr(row, name)
        if value is not None:
            record[name] = value
    if row.authors:
        record['authors'] = list(row.authors)
    # A source line must never become another RIS tag or record. CSL JSON/CSV
    # retain these characters losslessly when RIS cannot represent them safely.
    for value in (row.title, row.language, row.edition, row.volume, *row.authors):
        if value is not None and any(unicodedata.category(c).startswith('C')
                                     or c in '\u2028\u2029' for c in value):
            raise ValueError('Unsafe multiline/control RIS metadata. Use CSL JSON or CSV.')
    return ris_dumps([record], implementation=_PlainRisWriter)


def _bibtex(row, source_id):
    lines = [f'@misc{{{source_id},']
    for name in ('title', 'language', 'edition', 'volume'):
        value = getattr(row, name)
        if value is not None:
            lines.append(f'  {name} = {{{_bibtex_text(value)}}},')
    if row.authors:
        # Every source author is one literal name. Neither "and", semicolons
        # nor commas in a stored name imply additional people or name parts.
        authors = ' and '.join('{' + _bibtex_text(name) + '}' for name in row.authors)
        lines.append(f'  author = {{{authors}}},')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def export_bibliography(db: sqlite3.Connection, workspace: str, *,
                        collection: str | None = None, format: str = 'csl-json',
                        cancelled: Callable[[], bool] = lambda: False) -> str:
    """Export every scoped source edition in deterministic binary ID order.

    Supported formats are exactly ``csl-json``, ``ris`` and ``bibtex``. Generic
    document types are used because the source catalog has no publication-type
    field. Dates, publishers, DOI/ISBN values and page ranges are never guessed.
    BibTeX converts metadata line breaks/tabs to spaces, as its header discloses;
    RIS rejects multiline/control metadata. CSL JSON preserves Unicode exactly.

    Limits are 50,000 editions and 32 MiB of UTF-8 output. Overflow raises
    ValueError instead of returning a partial bibliography. Cancellation raises
    TimeoutError. This function never commits, rolls back or writes to SQLite.
    """
    if type(format) is not str or format not in FORMATS:
        raise ValueError('Unsupported bibliography format.')
    catalog = _catalog(db, workspace, collection, cancelled)
    with _BoundedOutput() as output:
        if format == 'csl-json':
            output.write('[')
        elif format == 'bibtex':
            output.write('% Current source catalog metadata; no quotations or research notes.\n'
                         '% Metadata line breaks and tabs are represented as spaces.\n')
        first = True
        for row in _records(catalog, collection, cancelled):
            source_id = _source_id(workspace, row.edition_id)
            if format == 'csl-json':
                output.write(('\n' if first else ',\n') + _csl(row, source_id))
            elif format == 'ris':
                output.write(('' if first else '\n') + _ris(row, source_id))
            else:
                output.write('\n' + _bibtex(row, source_id))
            first = False
        if format == 'csl-json':
            output.write(('' if first else '\n') + ']\n')
        _check(cancelled)
        return output.getvalue()


def export_inventory(db: sqlite3.Connection, workspace: str, *,
                     collection: str | None = None,
                     cancelled: Callable[[], bool] = lambda: False) -> str:
    """Export source IDs, asset digests and current metadata as safe UTF-8 CSV.

    ``*_json`` cells use the existing ``json-string-v1`` reversible encoding:
    json.loads restores nullable text and the ordered ``authors_json`` array.
    All untrusted text starts inside a JSON string/array, preventing spreadsheet
    formula interpretation without changing the stored data. Limits, snapshot
    requirements and cancellation match export_bibliography.
    """
    catalog = _catalog(db, workspace, collection, cancelled)
    with _BoundedOutput() as output:
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(INVENTORY_HEADERS)
        for row in _records(catalog, collection, cancelled):
            writer.writerow(('json-string-v1', *(_encoded(value) for value in (
                _source_id(workspace, row.edition_id), workspace,
                row.work_id, row.edition_id, row.asset, row.title,
                row.authors, row.language, row.edition, row.volume,
            ))))
        _check(cancelled)
        return output.getvalue()
