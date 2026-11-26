"""Local academic references and reading lists, independent of source evidence.

All services use a caller-owned, authorized SQLite snapshot. Bibliographic page
ranges and metadata never confer quotation, PDF-review or printed-label authority.
Import previews do not write, download documents, follow URLs or read attachments.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import csv
import io
import json
import re
from types import MappingProxyType
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

from ..catalog.membership import _identity


ITEM_TYPES = ('book', 'article', 'thesis', 'report', 'webpage', 'other')
READING_STATUSES = ('unread', 'reading', 'read', 'on_hold')
PRIORITIES = ('low', 'normal', 'high')
SORTS = ('title', 'year', 'priority', 'updated')
MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_IMPORT_RECORDS = 2000
MAX_REFERENCES = 100000
_BIDI = frozenset('\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
_STAMP = '%Y-%m-%dT%H:%M:%S.%fZ'


@dataclass(frozen=True)
class Reference:
    id: str
    workspace: str
    title: str
    authors: tuple[str, ...] = ()
    item_type: str = 'book'
    year: str = ''
    container_title: str = ''
    publisher: str = ''
    volume: str = ''
    issue: str = ''
    pages: str = ''
    doi: str = ''
    isbn: str = ''
    url: str = ''
    abstract: str = ''
    notes: str = ''
    tags: tuple[str, ...] = ()
    reading_status: str = 'unread'
    priority: str = 'normal'
    rating: int = 0
    progress: int = 0
    archived: bool = False
    edition_id: str | None = None
    created_utc: str = ''
    updated_utc: str = ''


@dataclass(frozen=True)
class DuplicateGroup:
    reason: str
    reference_ids: tuple[str, ...]
    exact: bool


@dataclass(frozen=True)
class ImportEntry:
    index: int
    fields: MappingProxyType
    warnings: tuple[str, ...] = ()
    duplicate_ids: tuple[str, ...] = ()
    duplicate_indices: tuple[int, ...] = ()

    @property
    def title(self):
        return self.fields['title']

    @property
    def authors(self):
        return self.fields['authors']


@dataclass(frozen=True)
class ImportPreview:
    workspace: str
    format: str
    source_sha256: str
    entries: tuple[ImportEntry, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    imported_ids: tuple[str, ...]
    skipped_indices: tuple[int, ...]
    duplicate_ids: tuple[str, ...]


_DEFAULTS = dict(title='', authors=(), item_type='book', year='',
    container_title='', publisher='', volume='', issue='', pages='', doi='',
    isbn='', url='', abstract='', notes='', tags=(), reading_status='unread',
    priority='normal', rating=0, progress=0, archived=False, edition_id=None)
_COLUMNS = ('workspace', 'id', *tuple(_DEFAULTS), 'created_utc', 'updated_utc', 'search_text')
_SELECT = ','.join(_COLUMNS)


def _text(value, name, maximum=4000, *, multiline=False, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'Invalid {name}.')
    value.encode('utf-8', errors='strict')
    if any(c in _BIDI or unicodedata.category(c) in ('Cs', 'Cc')
           and not (multiline and c in '\n\r\t') for c in value):
        raise ValueError(f'Unsupported control character in {name}.')
    if not multiline and any(c in '\u2028\u2029' for c in value):
        raise ValueError(f'{name} must be on one line.')
    return value


def _strings(value, name, *, maximum=100, item_maximum=4000):
    if not isinstance(value, (tuple, list)) or len(value) > maximum:
        raise ValueError(f'Invalid {name}.')
    return tuple(_text(item, name, item_maximum, required=True) for item in value)


def normalize_doi(value):
    """Canonical comparison form for an explicitly supplied DOI; no lookup."""
    value = _text(value, 'DOI', 2000).strip()
    if not value:
        return ''
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value, flags=re.I)
    value = re.sub(r'^doi:\s*', '', value, flags=re.I)
    if not re.fullmatch(r'10\.[0-9]{4,9}/\S+', value):
        raise ValueError('Enter a DOI such as 10.1234/example, or leave it empty.')
    return value.casefold()


def normalize_isbn(value):
    """Validate an ISBN-10/13 checksum and remove display separators."""
    value = _text(value, 'ISBN', 64).strip()
    if not value:
        return ''
    value = re.sub(r'^ISBN(?:-1[03])?:?\s*', '', value, flags=re.I)
    value = re.sub(r'[\s-]', '', value).upper()
    valid = False
    if re.fullmatch(r'[0-9]{9}[0-9X]', value):
        valid = sum((10 - i) * (10 if c == 'X' else int(c))
                    for i, c in enumerate(value)) % 11 == 0
    elif re.fullmatch(r'97[89][0-9]{10}', value):
        valid = sum(int(c) * (1 if i % 2 == 0 else 3)
                    for i, c in enumerate(value)) % 10 == 0
    if not valid:
        raise ValueError('The ISBN must have a valid ISBN-10 or ISBN-13 checksum.')
    return value


def _isbn_key(value):
    if len(value) == 10:
        prefix = '978' + value[:9]
        check = (-sum(int(c) * (1 if i % 2 == 0 else 3)
                      for i, c in enumerate(prefix))) % 10
        return prefix + str(check)
    return value


def _fold(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _validate(values):
    if set(values) != set(_DEFAULTS):
        raise ValueError('Unsupported or missing reference fields.')
    result = dict(values)
    result['title'] = _text(result['title'], 'title', required=True)
    result['authors'] = _strings(result['authors'], 'authors')
    result['tags'] = _strings(result['tags'], 'tags', item_maximum=512)
    if len(result['tags']) != len(set(result['tags'])):
        raise ValueError('A reference cannot contain the same tag twice.')
    for name in ('container_title', 'publisher', 'volume', 'issue', 'pages'):
        result[name] = _text(result[name], name)
    result['year'] = _text(result['year'], 'year', 32)
    result['abstract'] = _text(result['abstract'], 'abstract', 100000, multiline=True)
    result['notes'] = _text(result['notes'], 'notes', 100000, multiline=True)
    result['doi'] = normalize_doi(result['doi'])
    result['isbn'] = normalize_isbn(result['isbn'])
    result['url'] = _text(result['url'], 'URL')
    if result['url']:
        url = urlsplit(result['url'])
        if (url.scheme.lower() not in ('http', 'https') or not url.hostname
                or url.username is not None or url.password is not None
                or any(c.isspace() for c in result['url'])):
            raise ValueError('Use a public HTTP or HTTPS URL, without embedded credentials.')
    for name, permitted in (('item_type', ITEM_TYPES), ('reading_status', READING_STATUSES),
                            ('priority', PRIORITIES)):
        if not isinstance(result[name], str) or result[name] not in permitted:
            raise ValueError(f'Invalid {name}.')
    for name, maximum in (('rating', 5), ('progress', 100)):
        if type(result[name]) is not int or not 0 <= result[name] <= maximum:
            raise ValueError(f'Invalid {name}.')
    if type(result['archived']) is not bool:
        raise ValueError('Invalid archive state.')
    if result['edition_id'] is not None:
        result['edition_id'] = _identity(result['edition_id'])
    return result


def _search_text(values):
    content = [values[name] for name in ('title', 'year', 'container_title', 'publisher',
        'volume', 'issue', 'pages', 'doi', 'isbn', 'url', 'abstract', 'notes')]
    content.extend(values['authors'])
    content.extend(values['tags'])
    return '\n'.join(_fold(value) for value in content)


def _stamp(value):
    if not isinstance(value, str) or len(value) != 27:
        raise ValueError('Invalid reference timestamp.')
    result = datetime.strptime(value, _STAMP)
    if result.strftime(_STAMP) != value:
        raise ValueError('Invalid reference timestamp.')
    return result


def _now(previous=None):
    value = datetime.now(timezone.utc).replace(tzinfo=None)
    if previous is not None:
        value = max(value, _stamp(previous) + timedelta(microseconds=1))
    return value.strftime(_STAMP)


def _row_reference(row):
    raw = dict(zip(_COLUMNS, row))
    _identity(raw['workspace'])
    _identity(raw['id'])
    values = {name: raw[name] for name in _DEFAULTS}
    for name in ('authors', 'tags'):
        try:
            decoded = json.loads(values[name])
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f'Invalid stored {name}.') from exc
        if type(decoded) is not list or _json(decoded) != values[name]:
            raise ValueError(f'Invalid stored {name}.')
        values[name] = tuple(decoded)
    if type(values['archived']) is not int or values['archived'] not in (0, 1):
        raise ValueError('Invalid stored archive state.')
    values['archived'] = bool(values['archived'])
    checked = _validate(values)
    if checked != values or raw['search_text'] != _search_text(checked):
        raise ValueError('Academic reference metadata or search index has changed.')
    if _stamp(raw['created_utc']) > _stamp(raw['updated_utc']):
        raise ValueError('Invalid reference timestamp order.')
    return Reference(id=raw['id'], workspace=raw['workspace'], **checked,
        created_utc=raw['created_utc'], updated_utc=raw['updated_utc'])


def validate_academic_database(db):
    """Stream-check every row, including archived workspaces, before backup."""
    for row in db.execute(f'SELECT {_SELECT} FROM academic_reference'):
        _row_reference(row)
    if db.execute('''SELECT 1 FROM academic_reference a LEFT JOIN workspace w
            ON w.id=a.workspace LEFT JOIN catalog_edition e
            ON e.workspace=a.workspace AND e.id=a.edition_id
            WHERE w.id IS NULL OR (a.edition_id IS NOT NULL AND e.id IS NULL)
            LIMIT 1''').fetchone():
        raise ValueError('Academic reference has an unavailable library or edition.')


validate_academic_rows = validate_academic_database


def _metadata(reference):
    return {name: getattr(reference, name) for name in _DEFAULTS}


def _duplicate_keys(values):
    keys = []
    if values['doi']:
        keys.append(('Same DOI', values['doi'], True))
    if values['isbn']:
        keys.append(('Same ISBN', _isbn_key(values['isbn']), True))
    if values['title'] and values['authors'] and values['year']:
        title = ' '.join(re.findall(r'\w+', _fold(values['title'])))
        authors = tuple(_fold(author) for author in values['authors'])
        keys.append(('Same title, authors and year; review editions',
                     (title, authors, _fold(values['year']), values['item_type']), False))
    return tuple(keys)


class AcademicLibrary:
    def __init__(self, db, workspace):
        self.db, self.workspace = db, _identity(workspace)
        self._scope()

    def _scope(self, *, write=False):
        if not self.db.in_transaction or self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise ValueError('A caller snapshot with foreign keys is required.')
        if write and self.db.execute('PRAGMA recursive_triggers').fetchone()[0] != 1:
            raise ValueError('Recursive triggers are required for reference changes.')
        if not self.db.execute('SELECT 1 FROM workspace_meta WHERE workspace=? AND archived=0',
                               (self.workspace,)).fetchone():
            raise ValueError('The selected library is missing or archived.')

    def _edition(self, identity):
        if identity is not None and not self.db.execute(
                'SELECT 1 FROM catalog_edition WHERE workspace=? AND id=?',
                (self.workspace, identity)).fetchone():
            raise ValueError('The linked document must belong to this library.')

    def get(self, identity):
        self._scope()
        row = self.db.execute(f'SELECT {_SELECT} FROM academic_reference WHERE workspace=? AND id=?',
                              (self.workspace, _identity(identity))).fetchone()
        if row is None:
            raise ValueError('The reference is unavailable in this library.')
        return _row_reference(row)

    def create(self, **fields):
        self._scope(write=True)
        if set(fields) - set(_DEFAULTS):
            raise ValueError('Unsupported reference fields.')
        values = _validate({**_DEFAULTS, **fields})
        self._edition(values['edition_id'])
        identity, now = uuid4().hex, _now()
        stored = {**values, 'workspace': self.workspace, 'id': identity,
                  'created_utc': now, 'updated_utc': now, 'search_text': _search_text(values)}
        for name in ('authors', 'tags'):
            stored[name] = _json(stored[name])
        stored['archived'] = int(values['archived'])
        self.db.execute(f'INSERT INTO academic_reference ({_SELECT}) VALUES '
                        f'({",".join("?" for _ in _COLUMNS)})',
                        tuple(stored[name] for name in _COLUMNS))
        return self.get(identity)

    def update(self, identity, *, expected=None, **fields):
        self._scope(write=True)
        current = self.get(identity)
        if expected is not None and (not isinstance(expected, Reference) or expected != current):
            raise ValueError('This reference changed while it was open. Reopen it before saving.')
        if set(fields) - set(_DEFAULTS):
            raise ValueError('Unsupported reference fields.')
        values = _validate({**_metadata(current), **fields})
        self._edition(values['edition_id'])
        if values == _metadata(current):
            return current
        stored = {**values, 'updated_utc': _now(current.updated_utc), 'search_text': _search_text(values)}
        for name in ('authors', 'tags'):
            stored[name] = _json(stored[name])
        stored['archived'] = int(values['archived'])
        names = (*tuple(_DEFAULTS), 'updated_utc', 'search_text')
        self.db.execute('UPDATE academic_reference SET ' + ','.join(f'{name}=?' for name in names)
                        + ' WHERE workspace=? AND id=?',
                        (*tuple(stored[name] for name in names), self.workspace, current.id))
        return self.get(current.id)

    def archive(self, identity, archived=True, *, expected=None):
        return self.update(identity, expected=expected, archived=archived)

    def list(self, query='', status=None, tag=None, sort='title', include_archived=False,
             *, limit=1000, offset=0):
        self._scope()
        query = _text(query, 'query')
        terms = _fold(query).split()
        if len(terms) > 32:
            raise ValueError('Use at most 32 search words.')
        if status is not None and status not in READING_STATUSES:
            raise ValueError('Invalid reading status filter.')
        if type(include_archived) is not bool or sort not in SORTS:
            raise ValueError('Invalid reference view options.')
        if (type(limit) is not int or not 1 <= limit <= 5000
                or type(offset) is not int or not 0 <= offset <= MAX_REFERENCES):
            raise ValueError('Invalid reference page size or offset.')
        where, args = ['workspace=?'], [self.workspace]
        if not include_archived:
            where.append('archived=0')
        if status is not None:
            where.append('reading_status=?')
            args.append(status)
        for term in terms:
            where.append('instr(search_text,?)>0')
            args.append(term)
        if tag is not None:
            token = _json(_text(tag, 'tag', 512, required=True))
            where.append('(tags=? OR instr(tags,?)>0 OR instr(tags,?)>0 OR instr(tags,?)>0)')
            args.extend(('[' + token + ']', '[' + token + ',', ',' + token + ',', ',' + token + ']'))
        order = dict(title='search_text COLLATE BINARY,id',
            year="CASE WHEN length(year) BETWEEN 1 AND 4 AND year NOT GLOB '*[^0-9]*' THEN 0 ELSE 1 END,"
                 "CASE WHEN length(year) BETWEEN 1 AND 4 AND year NOT GLOB '*[^0-9]*' THEN CAST(year AS INTEGER) END DESC,"
                 'year DESC,search_text,id',
            priority="CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,search_text,id",
            updated='updated_utc DESC,id')[sort]
        rows = self.db.execute(f'SELECT {_SELECT} FROM academic_reference WHERE '
            + ' AND '.join(where) + f' ORDER BY {order} LIMIT ? OFFSET ?', (*args, limit, offset))
        return tuple(_row_reference(row) for row in rows)

    def _all(self, *, include_archived=False):
        self._scope()
        rows = self.db.execute(f'SELECT {_SELECT} FROM academic_reference WHERE workspace=?'
            + ('' if include_archived else ' AND archived=0') + ' ORDER BY id LIMIT ?',
            (self.workspace, MAX_REFERENCES + 1))
        result = tuple(_row_reference(row) for row in rows)
        if len(result) > MAX_REFERENCES:
            raise ValueError('This operation supports at most 100,000 references per library.')
        return result

    def duplicates(self):
        groups = defaultdict(list)
        for row in self._all():
            for key in _duplicate_keys(_metadata(row)):
                groups[key].append(row.id)
        return tuple(DuplicateGroup(reason, tuple(identities), exact)
            for (reason, _, exact), identities in groups.items() if len(identities) > 1)

    def statistics(self):
        self._scope()
        counts = dict(total=0, active=0, archived=0, unread=0, reading=0, read=0,
            on_hold=0, high_priority=0, linked=0, unlinked=0, missing_identifier=0,
            missing_author=0, missing_year=0)
        for row in self.db.execute('SELECT archived,reading_status,priority,edition_id,doi,isbn,authors,year '
                                  'FROM academic_reference WHERE workspace=?', (self.workspace,)):
            counts['total'] += 1
            if row[0]:
                counts['archived'] += 1
                continue
            counts['active'] += 1
            counts[row[1]] += 1
            counts['high_priority'] += row[2] == 'high'
            counts['linked' if row[3] is not None else 'unlinked'] += 1
            counts['missing_identifier'] += not row[4] and not row[5]
            counts['missing_author'] += row[6] == '[]'
            counts['missing_year'] += not row[7]
        return counts

    def import_preview(self, data, format='ris'):
        self._scope()
        parsed, general = _parse_import(data, format)
        existing = defaultdict(list)
        for reference in self._all():
            for key in _duplicate_keys(_metadata(reference)):
                existing[key].append(reference.id)
        batch = defaultdict(list)
        for index, (values, _) in enumerate(parsed):
            for key in _duplicate_keys(values):
                batch[key].append(index)
        entries = []
        for index, (values, warnings) in enumerate(parsed):
            keys = _duplicate_keys(values)
            ids = tuple(sorted({identity for key in keys for identity in existing[key]}))
            indices = tuple(sorted({number for key in keys for number in batch[key] if number != index}))
            entries.append(ImportEntry(index, MappingProxyType(values), tuple(warnings), ids, indices))
        return ImportPreview(self.workspace, format, sha256(data).hexdigest(), tuple(entries), tuple(general))

    def import_selected(self, preview, selected_indices, *, skip_duplicates=False):
        self._scope(write=True)
        if (not isinstance(preview, ImportPreview) or preview.workspace != self.workspace
                or type(preview.entries) is not tuple or not 1 <= len(preview.entries) <= MAX_IMPORT_RECORDS
                or type(skip_duplicates) is not bool):
            raise ValueError('Invalid or cross-library import preview.')
        if not isinstance(selected_indices, (tuple, list)) or not selected_indices:
            raise ValueError('Select at least one reference to import.')
        if (any(type(index) is not int or not 0 <= index < len(preview.entries) for index in selected_indices)
                or len(set(selected_indices)) != len(selected_indices)):
            raise ValueError('Invalid or repeated import selection.')
        entries = []
        for index in selected_indices:
            entry = preview.entries[index]
            if not isinstance(entry, ImportEntry) or entry.index != index:
                raise ValueError('The import preview has changed.')
            entries.append((index, _validate(dict(entry.fields))))
        existing = defaultdict(list)
        for row in self._all():
            for key in _duplicate_keys(_metadata(row)):
                existing[key].append(row.id)
        imported, skipped, duplicates = [], [], set()
        self.db.execute('SAVEPOINT academic_import')
        try:
            for index, values in entries:
                matches = {identity for key in _duplicate_keys(values) for identity in existing[key]}
                duplicates.update(matches)
                if matches and skip_duplicates:
                    skipped.append(index)
                    continue
                row = self.create(**values)
                imported.append(row.id)
                for key in _duplicate_keys(values):
                    existing[key].append(row.id)
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK TO academic_import')
                self.db.execute('RELEASE academic_import')
            raise
        self.db.execute('RELEASE academic_import')
        return ImportResult(tuple(imported), tuple(skipped), tuple(sorted(duplicates)))

    def export(self, ids=None, format='text', *, include_notes=False):
        self._scope()
        if type(include_notes) is not bool:
            raise ValueError('Invalid note export option.')
        if ids is None:
            rows = self._all()
        else:
            if (not isinstance(ids, (tuple, list)) or len(ids) > MAX_REFERENCES
                    or len(set(ids)) != len(ids)):
                raise ValueError('Invalid reference export selection.')
            rows = tuple(self.get(identity) for identity in ids)
        return _export(rows, format, include_notes)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _text(key, 'JSON field name', 256, required=True)
        if key in result:
            raise ValueError(f'Duplicate JSON field: {key}.')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f'Invalid JSON numeric constant: {value}.')


def _parse_import(data, format):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMPORT_BYTES:
        raise ValueError('Choose a nonempty reference file no larger than 8 MiB.')
    try:
        text = data.decode('utf-8-sig', errors='strict')
    except UnicodeError as exc:
        raise ValueError('The reference file must be UTF-8 text.') from exc
    _text(text, 'reference file', MAX_IMPORT_BYTES, multiline=True)
    if format == 'ris':
        rows = _parse_ris(text)
    elif format == 'csl-json':
        rows = _parse_csl(text)
    else:
        raise ValueError('Supported import formats are RIS and CSL JSON.')
    if not 1 <= len(rows) <= MAX_IMPORT_RECORDS:
        raise ValueError('Import between 1 and 2,000 references at a time.')
    result = []
    for number, (values, warnings) in enumerate(rows, 1):
        try:
            result.append((_validate({**_DEFAULTS, **values}), tuple(warnings)))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f'Reference {number}: {exc}') from exc
    return result, ('Imported bibliographic metadata is not an independently verified source citation.',)


_RIS_TYPES = {'BOOK': 'book', 'EBOOK': 'book', 'EDBOOK': 'book',
    'JOUR': 'article', 'EJOUR': 'article', 'MGZN': 'article', 'NEWS': 'article',
    'THES': 'thesis', 'RPRT': 'report', 'ELEC': 'webpage', 'GEN': 'other'}
_RIS_TAG = re.compile(r'([A-Z][A-Z0-9])  -(?: (.*))?\Z')
_RIS_HANDLED = frozenset(('TY', 'ER', 'TI', 'T1', 'AU', 'A1', 'PY', 'Y1', 'T2',
    'JF', 'JO', 'PB', 'VL', 'IS', 'SP', 'EP', 'DO', 'SN', 'UR', 'AB', 'N2',
    'N1', 'RN', 'KW', 'ID'))


def _values(record, *names):
    values = []
    for name in names:
        value = record.get(name, [])
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str):
                raise ValueError(f'Invalid reference value in {name}.')
            if item:
                values.append(item)
    return values


def _one(record, *names):
    values = tuple(dict.fromkeys(_values(record, *names)))
    if len(values) > 1:
        raise ValueError(f'Conflicting values for {names[0]}; review the import file.')
    return values[0] if values else ''


def _parse_ris(text):
    """Validate framing before using the existing pinned rispy parser.

    Wrapped abstracts/notes are accepted. Other malformed continuations and
    incomplete records are rejected as a batch instead of silently discarded.
    """
    from ..catalog._rispy_parser import RisParser

    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if len(lines) > 100000 or any(len(line) > 100000 for line in lines):
        raise ValueError('RIS line or line-count limit exceeded.')
    active, prior, tags, tag_sets = False, None, set(), []
    for number, line in enumerate(lines, 1):
        if not line.strip() and not active:
            continue
        if not active and re.fullmatch(r'[0-9]+\.', line.strip()):
            continue  # Numbered headers produced by the pinned upstream writer.
        match = _RIS_TAG.fullmatch(line)
        if match is None:
            if active and prior in ('AB', 'N2', 'N1', 'RN'):
                continue
            raise ValueError(f'Malformed RIS line {number}.')
        tag, value = match.groups()
        value = value or ''
        if tag == 'TY':
            if active or not value.strip():
                raise ValueError(f'Invalid RIS record start on line {number}.')
            active, tags = True, {'TY'}
        elif tag == 'ER':
            if not active or value.strip():
                raise ValueError(f'Invalid RIS record end on line {number}.')
            tag_sets.append(tags)
            if len(tag_sets) > MAX_IMPORT_RECORDS:
                raise ValueError('Import at most 2,000 references at a time.')
            active, tags = False, set()
        elif not active:
            raise ValueError(f'RIS field outside a record on line {number}.')
        else:
            tags.add(tag)
        prior = tag
    if active or not tag_sets:
        raise ValueError('The RIS file contains an incomplete or empty reference batch.')
    class AcademicRisParser(RisParser):
        def parse_line(self, line):
            match = _RIS_TAG.fullmatch(line)
            if match:
                tag, content = match.groups()
                return tag, content or ''
            return None, line[6:] if line.startswith('      ') else line

        def _add_tag(self, record, tag, content, extend_multiline=False):
            if extend_multiline:
                name = self.mapping[tag]
                previous = record[name]
                if isinstance(previous, list):
                    previous[-1] += '\n' + content
                else:
                    record[name] = previous + '\n' + content
                return
            super()._add_tag(record, tag, content)

    mapping = {**RisParser.DEFAULT_MAPPING, 'A1': 'authors'}
    records = AcademicRisParser(mapping=mapping, enforce_list_tags=False,
        delimiter_tags_mapping={}, skip_unknown_tags=False).parse_lines(iter(lines))
    if len(records) != len(tag_sets):
        raise ValueError('RIS parser record count mismatch.')
    result = []
    for number, (record, seen) in enumerate(zip(records, tag_sets), 1):
        try:
            warnings = []
            raw_type = _one(record, 'type_of_reference').strip()
            item_type = _RIS_TYPES.get(raw_type, 'other')
            if raw_type not in _RIS_TYPES:
                warnings.append(f'RIS type {raw_type} is imported as other.')
            unused = seen - _RIS_HANDLED
            if unused:
                warnings.append('RIS fields not imported: ' + ', '.join(sorted(unused)) + '.')
            if seen & {'L1', 'L2', 'L4'}:
                warnings.append('Attachment paths are ignored; no referenced file is opened.')
            dates = _values(record, 'year', 'publication_year')
            years = tuple(dict.fromkeys(date.split('/', 1)[0].strip() for date in dates))
            if len(years) > 1:
                raise ValueError('Conflicting publication years; review the import file.')
            year = years[0] if years else ''
            for date in dict.fromkeys(dates):
                if '/' in date:
                    warnings.append(f'Publication date {date} is imported as its year only.')
            serial = _one(record, 'issn')
            isbn = ''
            if serial:
                if re.fullmatch(r'[0-9]{4}-?[0-9]{3}[0-9Xx]', serial.strip()):
                    warnings.append(f'ISSN {serial} is not stored in the ISBN field.')
                else:
                    isbn = normalize_isbn(serial)
            urls = tuple(dict.fromkeys(_values(record, 'urls')))
            if len(urls) > 1:
                warnings.append('Only the first URL is imported; other URLs: ' + '; '.join(urls[1:]))
            first, last = _one(record, 'start_page'), _one(record, 'end_page')
            pages = first + '-' + last if first and last and first != last else first or last
            result.append((dict(title=_one(record, 'title', 'primary_title'),
                authors=tuple(_values(record, 'authors')), item_type=item_type, year=year,
                container_title=_one(record, 'secondary_title', 'alternate_title3', 'journal_name'),
                publisher=_one(record, 'publisher'), volume=_one(record, 'volume'),
                issue=_one(record, 'number'), pages=pages, doi=_one(record, 'doi'), isbn=isbn,
                url=urls[0] if urls else '', abstract=_one(record, 'abstract', 'notes_abstract'),
                notes='\n\n'.join(_values(record, 'notes', 'research_notes')),
                tags=tuple(dict.fromkeys(_values(record, 'keywords')))), warnings))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f'RIS reference {number}: {exc}') from exc
    return result


_CSL_TYPES = {'book': 'book', 'article-journal': 'article', 'article-magazine': 'article',
    'article-newspaper': 'article', 'article': 'article', 'paper-conference': 'article',
    'thesis': 'thesis', 'report': 'report', 'webpage': 'webpage', 'document': 'other'}
_CSL_HANDLED = frozenset(('id', 'type', 'title', 'author', 'issued', 'container-title',
    'publisher', 'volume', 'issue', 'page', 'DOI', 'ISBN', 'URL', 'abstract', 'note', 'keyword'))


def _csl_authors(value, warnings):
    if type(value) is not list or len(value) > 100:
        raise ValueError('Invalid CSL author list.')
    result = []
    structured = False
    for author in value:
        if type(author) is not dict:
            raise ValueError('Each CSL author must be a name object.')
        if 'literal' in author:
            if any(author.get(key) for key in ('family', 'given', 'suffix',
                    'dropping-particle', 'non-dropping-particle')):
                raise ValueError('A CSL name has both literal and structured name fields.')
            result.append(_text(author['literal'], 'author', required=True))
        else:
            structured = True
            family = _text(author.get('family', ''), 'author family name')
            given = _text(author.get('given', ''), 'author given name')
            particle = _text(author.get('non-dropping-particle', ''), 'author particle')
            dropping = _text(author.get('dropping-particle', ''), 'author particle')
            suffix = _text(author.get('suffix', ''), 'author suffix')
            family = ' '.join(part for part in (particle, family) if part)
            given = ' '.join(part for part in (given, dropping) if part)
            display = ', '.join(part for part in (family, given, suffix) if part)
            result.append(_text(display, 'author', required=True))
        unsupported = set(author) - {'literal', 'family', 'given', 'suffix',
            'dropping-particle', 'non-dropping-particle'}
        if unsupported:
            warnings.append('CSL author attributes not imported: ' + ', '.join(sorted(unsupported)) + '.')
    if structured:
        warnings.append('Structured author names are retained as display names; CSL export uses literal names.')
    return tuple(result)


def _csl_year(value, warnings):
    if value is None:
        return ''
    if type(value) is not dict:
        raise ValueError('Invalid CSL publication date.')
    if 'date-parts' in value:
        parts = value['date-parts']
        if (type(parts) is not list or not 1 <= len(parts) <= 2
                or any(type(part) is not list or not 1 <= len(part) <= 3
                    or any(type(number) is not int for number in part) for part in parts)):
            raise ValueError('Invalid CSL date-parts.')
        if len(parts) > 1 or len(parts[0]) > 1 or set(value) != {'date-parts'}:
            warnings.append('The publication date is imported as the first year only.')
        return str(parts[0][0])
    if 'literal' in value:
        if set(value) != {'literal'}:
            warnings.append('Only the literal publication date is imported.')
        return _text(value['literal'], 'publication date', 32)
    if value:
        raise ValueError('Use CSL issued date-parts or a literal publication date.')
    return ''


def _parse_csl(text):
    try:
        records = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f'Invalid CSL JSON: {exc}') from exc
    if type(records) is dict:
        records = [records]
    if type(records) is not list or not 1 <= len(records) <= MAX_IMPORT_RECORDS:
        raise ValueError('CSL JSON must contain 1 to 2,000 reference objects.')
    result = []
    for number, record in enumerate(records, 1):
        try:
            if type(record) is not dict:
                raise ValueError('The reference must be a JSON object.')
            warnings = []
            kind = _text(record.get('type', 'document'), 'CSL reference type', 128, required=True)
            if kind not in _CSL_TYPES:
                warnings.append(f'CSL type {kind} is imported as other.')
            unused = set(record) - _CSL_HANDLED
            if unused:
                warnings.append('CSL fields not imported: ' + ', '.join(sorted(unused)) + '.')
            if any(key.lower() in ('attachments', 'file', 'files', 'path') for key in unused):
                warnings.append('Attachment paths are ignored; no referenced file is opened.')
            keyword = _text(record.get('keyword', ''), 'keywords', 52000)
            values = dict(title=record.get('title', ''),
                authors=_csl_authors(record.get('author', []), warnings),
                item_type=_CSL_TYPES.get(kind, 'other'), year=_csl_year(record.get('issued'), warnings),
                container_title=record.get('container-title', ''), publisher=record.get('publisher', ''),
                volume=record.get('volume', ''), issue=record.get('issue', ''), pages=record.get('page', ''),
                doi=record.get('DOI', ''), isbn=record.get('ISBN', ''), url=record.get('URL', ''),
                abstract=record.get('abstract', ''), notes=record.get('note', ''),
                tags=tuple(dict.fromkeys(part.strip() for part in keyword.split(';') if part.strip())))
            # CSL permits numbers for volume, issue and page; preserve their
            # displayed representation without accepting boolean pseudo-numbers.
            for field in ('volume', 'issue', 'pages'):
                if type(values[field]) is int:
                    values[field] = str(values[field])
            result.append((values, warnings))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f'CSL reference {number}: {exc}') from exc
    return result


_TO_RIS = dict(book='BOOK', article='JOUR', thesis='THES', report='RPRT', webpage='ELEC', other='GEN')
_TO_CSL = dict(book='book', article='article-journal', thesis='thesis', report='report',
               webpage='webpage', other='document')


def _csl_records(rows, include_notes):
    result = []
    for row in rows:
        record = dict(id=row.id, type=_TO_CSL[row.item_type], title=row.title)
        if row.authors:
            record['author'] = [dict(literal=author) for author in row.authors]
        if row.year:
            record['issued'] = ({'date-parts': [[int(row.year)]]}
                if re.fullmatch(r'[0-9]{1,4}', row.year) else {'literal': row.year})
        for key, field in (('container-title', 'container_title'), ('publisher', 'publisher'),
                ('volume', 'volume'), ('issue', 'issue'), ('page', 'pages'), ('DOI', 'doi'),
                ('ISBN', 'isbn'), ('URL', 'url'), ('abstract', 'abstract')):
            if getattr(row, field):
                record[key] = getattr(row, field)
        if include_notes and row.notes:
            record['note'] = row.notes
        result.append(record)
    return result


def _export_ris(rows, include_notes):
    from ._vendor_rispy.writer import RisWriter

    class PlainWriter(RisWriter):
        def set_header(self, count):
            return None

    result = []
    for row in rows:
        record = dict(type_of_reference=_TO_RIS[row.item_type], id=row.id,
                      title=row.title, authors=list(row.authors))
        for key, field in (('year', 'year'), ('secondary_title', 'container_title'),
                ('publisher', 'publisher'), ('volume', 'volume'), ('number', 'issue'),
                ('start_page', 'pages'), ('doi', 'doi'), ('issn', 'isbn')):
            if getattr(row, field):
                record[key] = getattr(row, field)
        if row.url:
            record['urls'] = [row.url]
        # Metadata-only exports may contain a multiline abstract or an explicitly
        # requested note. Continuation indentation prevents embedded RIS tags
        # from escaping their field and creating a forged reference record.
        def continued(text):
            return text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\n      ')
        if row.abstract:
            record['abstract'] = continued(row.abstract)
        if include_notes and row.notes:
            record['notes'] = [continued(row.notes)]
        result.append(record)
    return PlainWriter().formats(result)


def _export_bibtex(rows, include_notes):
    from .evidence_export import _bibtex_text

    chunks = ['% Bibliographic metadata; not a source quotation or verified page locator.',
              '% BibTeX line breaks and tabs are represented as spaces; source values are unchanged.']
    for row in rows:
        kind = dict(book='book', article='article', thesis='misc', report='techreport',
                    webpage='misc', other='misc')[row.item_type]
        key = sha256(_json([row.workspace, row.id]).encode('utf-8')).hexdigest()[:24]
        values = [('title', row.title)]
        if row.item_type == 'thesis':
            values.append(('howpublished', 'Thesis'))
        if row.authors:
            # Every entered name remains one literal name. A name containing
            # "and" cannot manufacture extra authors or BibTeX syntax.
            authors = ' and '.join('{' + _bibtex_text(author) + '}' for author in row.authors)
        else:
            authors = ''
        for key_name, field in (('year', 'year'), ('journal' if row.item_type == 'article' else 'booktitle',
                'container_title'), ('publisher', 'publisher'), ('volume', 'volume'), ('number', 'issue'),
                ('pages', 'pages'), ('doi', 'doi'), ('isbn', 'isbn'), ('url', 'url'), ('abstract', 'abstract')):
            if getattr(row, field):
                values.append((key_name, getattr(row, field)))
        if include_notes and row.notes:
            values.append(('note', row.notes))
        entry = [f'@{kind}{{reference_{key},']
        if authors:
            entry.append('  author = {' + authors + '},')
        entry.extend('  ' + name + ' = {' + _bibtex_text(value) + '},' for name, value in values)
        entry.append('}')
        chunks.append('\n'.join(entry))
    return '\n\n'.join(chunks) + '\n'


def _export_csv(rows, include_notes):
    from .evidence_export import _encoded

    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    text_fields = ('title', 'authors', 'item_type', 'year', 'container_title', 'publisher',
        'volume', 'issue', 'pages', 'doi', 'isbn', 'url', 'abstract', 'tags', 'reading_status', 'priority')
    writer.writerow(('text_encoding', *(name + '_json' for name in text_fields),
                     'rating', 'progress', 'archived', *(('notes_json',) if include_notes else ())))
    for row in rows:
        values = [_encoded(getattr(row, name)) for name in text_fields]
        if include_notes:
            notes = [_encoded(row.notes)]
        else:
            notes = []
        writer.writerow(('json-value-v1', *values, row.rating, row.progress, int(row.archived), *notes))
    return output.getvalue()


def _export_text(rows, include_notes):
    chunks = ['Academic bibliography and reading list\nMetadata is user-maintained; source quotations are not included.']
    for number, row in enumerate(rows, 1):
        parts = [f'{number}. {row.title}', 'Authors: ' + ('; '.join(row.authors) or '(not entered)'),
                 f'Type: {row.item_type}; Year: {row.year or "(not entered)"}']
        for label, value in (('Publication', row.container_title), ('Publisher', row.publisher),
                ('Volume', row.volume), ('Issue', row.issue), ('Bibliographic pages', row.pages),
                ('DOI', row.doi), ('ISBN', row.isbn), ('URL', row.url)):
            if value:
                parts.append(f'{label}: {value}')
        parts.extend([f'Reading: {row.reading_status}; Priority: {row.priority}; '
                      f'Progress: {row.progress}%; Rating: {row.rating}/5',
                      'Tags: ' + (', '.join(row.tags) or '(none)')])
        if include_notes and row.notes:
            parts.extend(['Personal research notes:', row.notes])
        chunks.append('\n'.join(parts))
    return '\n\n'.join(chunks) + '\n'


def _export(rows, format, include_notes):
    if format == 'csl-json':
        return json.dumps(_csl_records(rows, include_notes), ensure_ascii=False, indent=2) + '\n'
    if format == 'ris':
        return _export_ris(rows, include_notes)
    if format == 'bibtex':
        return _export_bibtex(rows, include_notes)
    if format == 'csv':
        return _export_csv(rows, include_notes)
    if format in ('text', 'plain'):
        return _export_text(rows, include_notes)
    raise ValueError('Supported exports are RIS, BibTeX, CSL JSON, CSV and text.')
