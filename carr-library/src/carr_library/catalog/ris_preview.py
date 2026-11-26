"""Bounded, local-only RIS metadata preview; never creates source or citation IDs."""
from dataclasses import dataclass
from hashlib import sha256
import re

from ._rispy_parser import RisParser

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 5000
MAX_LINES = 100000
MAX_LINE = 16384
_TAG = re.compile(r"([A-Z][A-Z0-9])  -(?: (.*))?\Z")


@dataclass(frozen=True)
class RisHint:
    batch_sha256: str
    record_number: int
    reference_type: str
    titles: tuple[str, ...]
    authors: tuple[str, ...]
    languages: tuple[str, ...]
    editions: tuple[str, ...]
    volumes: tuple[str, ...]
    identifiers: tuple[str, ...]
    metadata_verified: bool = False


def _values(record, *keys):
    result = []
    for key in keys:
        value = record.get(key, [])
        for item in ([value] if isinstance(value, str) else value):
            if item:
                result.append(item)
    return tuple(result)


def preview_ris(data: bytes) -> tuple[RisHint, ...]:
    """Strict UTF-8(-BOM) RIS subset. All or nothing, no filesystem/network reads.

    Framing checks prevent rispy's permissive silent loss of incomplete records.
    Multiline data is intentionally rejected rather than ambiguously reassigned.
    Only selected bibliographic fields are returned; attachment paths, URLs,
    abstracts, notes and page ranges are not exposed or resolved.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValueError('invalid RIS byte size or type')
    raw = data.decode('utf-8-sig', errors='strict')
    if any(ord(ch) < 32 and ch not in '\r\n\t' for ch in raw):
        raise ValueError('RIS control character')
    lines = raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if len(lines) > MAX_LINES:
        raise ValueError('too many RIS lines')
    count = 0
    active = False
    for line in lines:
        if len(line) > MAX_LINE:
            raise ValueError('RIS line too long')
        if not line.strip():
            if active:
                raise ValueError('blank line inside RIS record')
            continue
        m = _TAG.fullmatch(line)
        if not m:
            raise ValueError('malformed or multiline RIS tag')
        tag, value = m.groups()
        value = (value or '').strip()
        if tag == 'TY':
            if active or not value:
                raise ValueError('invalid RIS record start')
            active = True
        elif tag == 'ER':
            if not active or value:
                raise ValueError('invalid RIS record end')
            active = False
            count += 1
            if count > MAX_RECORDS:
                raise ValueError('too many RIS records')
        elif not active:
            raise ValueError('RIS tag outside record')
    if active or not count:
        raise ValueError('incomplete or empty RIS batch')
    mapping = {**RisParser.DEFAULT_MAPPING, "A1": "authors", "T1": "title"}
    records = RisParser(mapping=mapping, enforce_list_tags=False, skip_unknown_tags=True).parse_lines(iter(lines))
    if len(records) != count:
        raise ValueError('RIS parser record count mismatch')
    digest = sha256(data).hexdigest()
    return tuple(RisHint(digest, n, rec['type_of_reference'],
                         _values(rec, 'title', 'primary_title'),
                         _values(rec, 'authors', 'first_authors'),
                         _values(rec, 'language'), _values(rec, 'edition'),
                         _values(rec, 'volume'), _values(rec, 'doi', 'issn'))
                 for n, rec in enumerate(records, 1))
