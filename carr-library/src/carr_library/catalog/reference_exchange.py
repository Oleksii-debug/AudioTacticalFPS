"""Bounded, local bibliography hints for explicit review against an existing book.

This is a narrow interchange profile, not a CSL renderer or a source importer.
External identifiers, links and attachment contents are never returned or read.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import math

from .ris_preview import preview_ris

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 5000
MAX_DEPTH = 16
MAX_NODES = 100000
MAX_AUTHORS = 100
MAX_CANDIDATES = 100
MAX_STRING = 16384

# CSL 1.0.2 input-data vocabulary; see REFERENCE_EXCHANGE.md for the source.
_CSL_TYPES = frozenset('''article article-journal article-magazine article-newspaper
bill book broadcast chapter classic collection dataset document entry
entry-dictionary entry-encyclopedia event figure graphic hearing interview
legal_case legislation manuscript map motion_picture musical_score pamphlet
paper-conference patent performance periodical personal_communication post
post-weblog regulation report review review-book software song speech standard
thesis treaty webpage'''.split())
_SUPPORTED_CSL = frozenset(('title', 'author', 'language', 'edition', 'volume'))
_SUPPORTED_RIS = frozenset(('TI', 'T1', 'AU', 'A1', 'LA', 'ET', 'VL', 'TY', 'ER'))
_NAME_PARTS = ('given', 'dropping-particle', 'non-dropping-particle', 'family', 'suffix')
_NAME_FLAGS = ('comma-suffix', 'static-ordering', 'parse-names')


@dataclass(frozen=True)
class ReferenceHint:
    batch_sha256: str
    record_number: int
    source_format: str
    reference_type: str
    titles: tuple[str, ...]
    authors: tuple[str, ...]
    languages: tuple[str, ...]
    editions: tuple[str, ...]
    volumes: tuple[str, ...]
    unsupported_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    metadata_verified: bool = False

    @property
    def origin(self) -> str:
        """A local input locator; never an edition, asset, page or citation ID."""
        return f'{self.source_format.upper()} record {self.record_number}'


def _text(value, *, limit, field):
    if not isinstance(value, str):
        raise ValueError(f'{field} must be text')
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 or c in '\x85\u2028\u2029' or
                                 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f'{field} is too long or contains unsupported characters')
    return value.strip()


def _candidates(values, *, limit, field):
    if len(values) > MAX_CANDIDATES:
        raise ValueError(f'too many {field} candidates')
    return tuple(v for v in (_text(v, limit=limit, field=field) for v in values) if v)


def _reject_constant(value):
    raise ValueError('non-finite JSON number')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key')
        result[key] = value
    return result


def _check_depth(raw):
    """Reject excess nesting before Python's JSON parser allocates nested nodes."""
    depth = 0
    quoted = False
    escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError('CSL-JSON nesting is too deep')
        elif char in ']}':
            depth -= 1


def _check_tree(value):
    """Bound ignored metadata as well as the fields offered for copying."""
    pending = [value]
    count = 0
    while pending:
        item = pending.pop()
        count += 1
        if count > MAX_NODES:
            raise ValueError('too many CSL-JSON values')
        if isinstance(item, dict):
            for key in item:
                _text(key, limit=128, field='CSL-JSON field name')
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            if len(item) > MAX_STRING or any(
                    (ord(c) < 32 and c not in '\t\r\n') or ord(c) == 127 or
                    0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise ValueError('CSL-JSON text is too long or contains unsupported characters')
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError('non-finite JSON number')


def _number_or_text(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f'{field} must be text or a number')
    return _text(str(value), limit=4000, field=field)


def _authors(value):
    if not isinstance(value, list) or len(value) > MAX_AUTHORS:
        raise ValueError('author must be a list of at most 100 names')
    authors = []
    unsupported = set()
    structured = False
    for name in value:
        if not isinstance(name, dict):
            raise ValueError('each author must be a CSL name object')
        unknown = set(name) - set(_NAME_PARTS) - set(_NAME_FLAGS) - {'literal'}
        unsupported.update(f'author.{field}' for field in unknown)
        for flag in _NAME_FLAGS:
            if flag in name:
                if not isinstance(name[flag], (str, int, float, bool)):
                    raise ValueError(f'author.{flag} has an invalid value')
                unsupported.add(f'author.{flag}')
        if 'literal' in name:
            if any(key in name for key in _NAME_PARTS):
                raise ValueError('author combines literal and structured names; choose one representation')
            author = _text(name['literal'], limit=512, field='author.literal')
        else:
            parts = {key: _text(name[key], limit=512, field=f'author.{key}')
                     for key in _NAME_PARTS if key in name}
            if not parts.get('family') and not parts.get('given'):
                raise ValueError('author needs a literal, family or given name')
            author = ' '.join(parts.get(key, '') for key in _NAME_PARTS[:-1]
                              if parts.get(key))
            if parts.get('suffix'):
                author += ', ' + parts['suffix']
            author = _text(author, limit=512, field='author')
            structured = True
        if not author:
            raise ValueError('author name must not be empty')
        authors.append(author)
    return tuple(authors), tuple(sorted(unsupported)), structured


def _preview_csl(data):
    raw = data.decode('utf-8-sig', errors='strict')
    _check_depth(raw)
    try:
        records = json.loads(raw, object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except RecursionError as exc:
        raise ValueError('CSL-JSON nesting is too deep') from exc
    if not isinstance(records, list) or not records or len(records) > MAX_RECORDS:
        raise ValueError('CSL-JSON must be a nonempty list of at most 5000 records')
    _check_tree(records)
    digest = sha256(data).hexdigest()
    hints = []
    for number, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError('each CSL-JSON record must be an object')
        if 'id' not in record or 'type' not in record:
            raise ValueError('each CSL-JSON record needs id and type')
        external_id = record['id']
        if (isinstance(external_id, bool) or not isinstance(external_id, (str, int, float)) or
                not _text(str(external_id), limit=512, field='id')):
            raise ValueError('CSL-JSON id must be nonempty text or a number')
        reference_type = record['type']
        if not isinstance(reference_type, str) or reference_type not in _CSL_TYPES:
            raise ValueError('unknown CSL-JSON reference type')
        unsupported = set(record) - _SUPPORTED_CSL
        warnings = ['Reference type and external identifiers are not copied into the catalog.']
        authors, name_fields, structured = _authors(record.get('author', []))
        unsupported.update(name_fields)
        if structured:
            warnings.append('Structured author names were converted to editable text. Review their order; '
                            'the original name structure will not be retained.')
        fields = {}
        for key, attr, limit in (('title', 'titles', 512), ('language', 'languages', 4000),
                                 ('edition', 'editions', 4000), ('volume', 'volumes', 4000)):
            value = record.get(key)
            if key not in record:
                fields[attr] = ()
                continue
            value = (_number_or_text(value, key) if key in ('edition', 'volume')
                     else _text(value, limit=limit, field=key))
            fields[attr] = (value,) if value else ()
        hints.append(ReferenceHint(digest, number, 'csl-json', reference_type,
                                   authors=authors, **fields,
                                   unsupported_fields=tuple(sorted(unsupported)),
                                   warnings=tuple(warnings)))
    return tuple(hints)


def _preview_ris(data):
    records = preview_ris(data)
    if len(records) > MAX_RECORDS:
        raise ValueError('too many RIS records')
    # Framing and metadata values are parsed only by the existing RIS adapter.
    # This inventory reads tag names only, after that adapter accepted the batch.
    inventories = []
    ignored = None
    for line in data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if not line.strip():
            continue
        tag = line[:2]
        if tag == 'TY':
            ignored = {'TY'}
        elif tag == 'ER':
            inventories.append(tuple(sorted(ignored)))
        elif tag not in _SUPPORTED_RIS:
            ignored.add(tag)
    if len(inventories) != len(records):
        raise ValueError('RIS field inventory does not match parsed records')
    hints = []
    for rec, unsupported in zip(records, inventories, strict=True):
        if len(rec.authors) > MAX_AUTHORS:
            raise ValueError('too many RIS authors')
        hints.append(ReferenceHint(
            rec.batch_sha256, rec.record_number, 'ris',
            _text(rec.reference_type, limit=128, field='reference type'),
            _candidates(rec.titles, limit=512, field='title'),
            _candidates(rec.authors, limit=512, field='author'),
            _candidates(rec.languages, limit=4000, field='language'),
            _candidates(rec.editions, limit=4000, field='edition'),
            _candidates(rec.volumes, limit=4000, field='volume'),
            unsupported,
            ('Only title, authors, language, edition and volume can be copied. '
             'The listed RIS tags are not copied; links and attachments are never opened.',)))
    return tuple(hints)


def preview_references(data: bytes, *, format: str) -> tuple[ReferenceHint, ...]:
    """Return a fully validated local preview, or fail without partial results.

    Supported formats are ``ris`` and ``csl-json``. Hints carry no trusted source
    identity. The caller must review supported fields and apply them through the
    existing metadata service to a selected, authorized edition.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValueError('invalid bibliography byte size or type; maximum 4 MiB')
    if format == 'ris':
        return _preview_ris(data)
    if format == 'csl-json':
        return _preview_csl(data)
    raise ValueError('choose RIS or CSL-JSON bibliography format')
