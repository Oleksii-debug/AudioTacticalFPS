"""Export known catalog metadata from one caller-owned, authorized snapshot.

Bibliography records describe catalog editions, not verified quotations. The
caller keeps its read transaction open and publishes the returned UTF-8 text
through the existing export-file boundary. No source files are opened here.
"""
from hashlib import sha256
import json
import unicodedata

from .membership import Catalog, _identity, _metadata
from .read_views import browse_editions
from ..research.evidence_export import _bibtex_text
from ..research._vendor_rispy.writer import RisWriter, dumps


MAX_REFERENCES = 10000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 100


class _PlainRisWriter(RisWriter):
    """Use rispy's extension hook to omit its non-RIS numbered headings."""
    def set_header(self, count):
        return ''


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Bibliography export cancelled.')


def _fields(row):
    fields = {}
    for key in ('title', 'language', 'edition', 'volume'):
        value = _metadata(getattr(row, key))
        if value is not None and value.strip():
            fields[key] = value
    if not isinstance(row.authors, tuple) or len(row.authors) > 100:
        raise ValueError('Invalid catalog author list.')
    authors = tuple(_metadata(value) for value in row.authors)
    if any(value is None or not value.strip() for value in authors):
        raise ValueError('Invalid catalog author name.')
    return fields, authors


def _reference_id(workspace, edition):
    # Stable across formats/collection membership; raw internal identifiers and
    # asset digests never enter the bibliography. Titles are not identities.
    raw = json.dumps(['carr-bibliography-v1', workspace, _identity(edition)],
                     ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return 'carr_' + sha256(raw).hexdigest()


def _ris(identity, fields, authors):
    # Newlines and controls could terminate a tag or inject another reference.
    for value in (*fields.values(), *authors):
        if any(unicodedata.category(c).startswith('C') or c in '\u2028\u2029'
               for c in value):
            raise ValueError('RIS requires single-line metadata without control characters; '
                             'use CSL-JSON to preserve this metadata.')
    reference = dict(type_of_reference='GEN', id=identity, **fields)
    if authors:
        reference['authors'] = list(authors)
    return dumps([reference], implementation=_PlainRisWriter)


def _bibtex(identity, fields, authors):
    lines = [f'@misc{{{identity},']
    for key, value in fields.items():
        lines.append(f'  {key} = {{{_bibtex_text(value)}}},')
    if authors:
        # Each catalog name is one literal author. In particular, "and" inside
        # a corporate name is not a separator and no given/family split is guessed.
        names = ' and '.join('{' + _bibtex_text(name) + '}' for name in authors)
        lines.append(f'  author = {{{names}}},')
    lines.append('}')
    return '\n'.join(lines) + '\n\n'


def export_bibliography(db, workspace, *, collection=None, format='csl-json',
                        cancelled=lambda: False) -> str:
    """Export at most 10,000 catalog editions and 16 MiB of UTF-8 output.

    The caller authorizes ``workspace`` and owns one consistent read transaction
    for the whole call. This function checks its snapshot prerequisites and the
    workspace/collection archive state, but grants no additional authorization.
    It never starts/ends a transaction, writes SQL, reads files or uses a network.

    Formats are ``csl-json``, ``ris`` and ``bibtex``. Only known title, ordered
    literal authors, language, edition and volume are included, plus opaque
    scoped reference IDs and generic document/GEN/misc types. No evidence,
    source text, private notes, asset hashes, paths or guessed publication data
    are exported. Blank unknown metadata is omitted. RIS rejects unsafe control
    characters; BibTeX uses the existing escaping/whitespace policy.
    """
    if format not in ('csl-json', 'ris', 'bibtex'):
        raise ValueError('Unsupported bibliography format; choose CSL-JSON, RIS or BibTeX.')
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    _cancel(cancelled)
    if not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]:
        raise ValueError('A consistent caller-owned read transaction is required.')
    catalog = Catalog(db, workspace)
    if not db.execute('SELECT 1 FROM workspace_meta WHERE workspace=? AND archived=0',
                      (workspace,)).fetchone():
        raise ValueError('The selected library is missing or archived.')

    chunks = []
    output_size = 0

    def append(text):
        nonlocal output_size
        output_size += len(text.encode('utf-8'))
        if output_size > MAX_OUTPUT_BYTES:
            raise ValueError(f'Bibliography exceeds the {MAX_OUTPUT_BYTES:,}-byte output limit. '
                             'Export a smaller collection.')
        chunks.append(text)

    if format == 'csl-json':
        append('[')
    elif format == 'bibtex':
        append('% Catalog bibliography; source quotations and private notes are omitted.\n'
               '% Metadata tabs and line breaks are represented as spaces.\n\n')
    count = 0
    after = None
    while True:
        _cancel(cancelled)
        limit = min(PAGE_SIZE, MAX_REFERENCES - count + 1)
        rows = browse_editions(catalog, collection=collection, after=after, limit=limit)
        _cancel(cancelled)
        for row in rows:
            _cancel(cancelled)
            count += 1
            if count > MAX_REFERENCES:
                raise ValueError(f'Bibliography exceeds {MAX_REFERENCES:,} references. '
                                 'Export a smaller collection.')
            fields, authors = _fields(row)
            identity = _reference_id(catalog.workspace, row.edition_id)
            if format == 'csl-json':
                reference = dict(id=identity, type='document', **fields)
                if authors:
                    reference['author'] = [dict(literal=name) for name in authors]
                append((',' if count > 1 else '') + '\n  ' +
                       json.dumps(reference, ensure_ascii=False, separators=(',', ':')))
            elif format == 'ris':
                append(_ris(identity, fields, authors))
            else:
                append(_bibtex(identity, fields, authors))
            after = row.edition_id
        if len(rows) < limit:
            break
    if format == 'csl-json':
        append(('\n' if count else '') + ']\n')
    _cancel(cancelled)
    return ''.join(chunks)
