"""Explicit reading-copy categories and source-bound narration projections.

These are user-reviewed labels, not an automatic structure classifier or
quotation approval. Unknown/unmarked content is always read.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from uuid import uuid4

from ._io import check_cancelled

SCHEMA = 'carr-reading-narration/1'
CATEGORIES = ('footnote', 'table', 'math', 'code', 'heading', 'other')
LABELS = {'footnote': 'Footnote', 'table': 'Table', 'math': 'Formula or mathematics',
          'code': 'Code', 'heading': 'Heading', 'other': 'Other explicitly selected content'}
MAX_REGIONS = 256
MAX_POLICY_BYTES = 256 * 1024
MAX_CHARACTERS = 2_000_000
_HEX = re.compile(r'[0-9a-f]{32}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class NarrationRegion:
    id: str
    category: str
    label: str
    text_sha256: str
    start: int
    end: int
    selected_sha256: str

    def __post_init__(self):
        if type(self.id) is not str or _HEX.fullmatch(self.id) is None:
            raise ValueError('Invalid reading-content region identity.')
        if type(self.category) is not str or self.category not in CATEGORIES:
            raise ValueError('Choose an available explicit reading-content category.')
        if (type(self.label) is not str or not self.label.strip() or len(self.label) > 80
                or any(ord(char) < 32 or 127 <= ord(char) < 160
                       or 0xD800 <= ord(char) <= 0xDFFF for char in self.label)):
            raise ValueError('Use a region name of 1 to 80 printable characters.')
        if any(type(value) is not str or _DIGEST.fullmatch(value) is None
               for value in (self.text_sha256, self.selected_sha256)):
            raise ValueError('A reading-content region needs exact text checksums.')
        if (type(self.start) is not int or type(self.end) is not int
                or not 0 <= self.start < self.end <= MAX_CHARACTERS):
            raise ValueError('Select a nonempty bounded reading-content range.')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def create(cls, text, start, end, category, label, *, cancelled=lambda: False):
        check_cancelled(cancelled)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError('Select the exact content to classify in the reading editor first.')
        result = cls(uuid4().hex, category, label, _digest(text), start, end, _digest(text[start:end]))
        check_cancelled(cancelled)
        return result


def validate_policy(value):
    """Validate only passive bounded data; never open a source or run speech."""
    if (type(value) is not dict or set(value) != {'schema', 'regions', 'skip'}
            or value['schema'] != SCHEMA or type(value['regions']) is not list
            or not 1 <= len(value['regions']) <= MAX_REGIONS
            or type(value['skip']) is not list or len(value['skip']) > len(CATEGORIES)):
        raise ValueError('Invalid or oversized reading-content policy.')
    fields = {'id', 'category', 'label', 'text_sha256', 'start', 'end', 'selected_sha256'}
    regions = []
    for row in value['regions']:
        if type(row) is not dict or set(row) != fields:
            raise ValueError('Invalid reading-content region fields.')
        regions.append(NarrationRegion(**row))
    if len({item.id for item in regions}) != len(regions):
        raise ValueError('Reading-content region identities must be unique.')
    available = {item.category for item in regions}
    if (any(type(item) is not str or item not in available for item in value['skip'])
            or len(set(value['skip'])) != len(value['skip'])
            or value['skip'] != [item for item in CATEGORIES if item in value['skip']]):
        raise ValueError('Read/Skip settings must name only available reviewed categories.')
    previous = {}
    for item in sorted(regions, key=lambda row: (row.text_sha256, row.start, row.end)):
        if item.start < previous.get(item.text_sha256, 0):
            raise ValueError('Reviewed reading-content ranges may not overlap on the same text version.')
        previous[item.text_sha256] = item.end
    copied = {'schema': SCHEMA, 'regions': [item.to_dict() for item in regions], 'skip': list(value['skip'])}
    if len(_canonical(copied).encode('utf-8')) > MAX_POLICY_BYTES:
        raise ValueError('Reading-content policy exceeds its serialized byte allowance.')
    return copied


def policy_digest(value):
    return _digest(_canonical(value)) if value is not None else None


def check_current_regions(value, text):
    """Current-version ranges must be exact; historical ranges stay passive."""
    if value is None:
        return
    digest = _digest(text)
    for row in value['regions']:
        if row['text_sha256'] == digest and (row['end'] > len(text)
                or _digest(text[row['start']:row['end']]) != row['selected_sha256']):
            raise ValueError('A reviewed reading-content range differs from its exact text.')


@dataclass(frozen=True)
class NarrationSlice:
    start: int
    end: int
    reading_start: int | None
    reading_end: int | None
    source_start: int | None
    source_end: int | None
    source_exact: bool
    derived_separator: bool = False


@dataclass(frozen=True)
class NarrationSelection:
    text: str
    text_sha256: str
    source_sha256: str
    source_anchor_json: str
    signature: tuple
    policy_sha256: str | None
    first: int
    last: int
    slices: tuple
    skipped: tuple

    @property
    def empty(self):
        return not self.text.strip()

    @property
    def filtered(self):
        return bool(self.skipped)

    def check(self, document):
        if (document._text_signature() != self.signature
                or document.source_sha256 != self.source_sha256
                or document.narration_policy_sha256 != self.policy_sha256):
            raise ValueError('The reading text or Read/Skip policy changed. Review narration again.')

    def provenance(self):
        return {'schema': 'carr-narration-selection/1', 'source_sha256': self.source_sha256,
                'source_anchor': json.loads(self.source_anchor_json),
                'narration_sha256': self.text_sha256, 'policy_sha256': self.policy_sha256,
                'reading_first': self.first, 'reading_last': self.last,
                'slices': [asdict(item) for item in self.slices], 'skipped_region_ids': list(self.skipped),
                'classification': 'explicit-user-selection', 'quotation_authority': False}


def select_narration(document, first=0, last=None, *, cancelled=lambda: False):
    """Project exact kept slices; inserted gap separators have no source claim."""
    check_cancelled(cancelled)
    text = document.text
    last = len(text) if last is None else last
    document._range(first, last)
    signature, value = document._text_signature(), document.narration_policy
    removed, skipped = [], []
    if value is not None:
        value = validate_policy(value)
        check_current_regions(value, text)
        fingerprint = _digest(text)
        for row in value['regions']:
            check_cancelled(cancelled)
            if row['category'] not in value['skip']:
                continue
            if row['text_sha256'] != fingerprint:
                raise ValueError('Skipped content belongs to changed reading text. Re-mark it or choose Read.')
            if row['end'] > first and row['start'] < last:
                removed.append((max(first, row['start']), min(last, row['end'])))
                skipped.append(row['id'])
    ranges, cursor = [], first
    for start, end in sorted(removed):
        if start > cursor:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < last:
        ranges.append((cursor, last))
    pieces, slices, offset = [], [], 0
    for start, end in ranges:
        check_cancelled(cancelled)
        if pieces:
            pieces.append('\n')
            slices.append(NarrationSlice(offset, offset + 1, None, None, None, None, False, True))
            offset += 1
        selected = text[start:end]
        source = document.source_span(start, end)
        pieces.append(selected)
        slices.append(NarrationSlice(offset, offset + len(selected), start, end,
                                     source.start, source.end, source.exact))
        offset += len(selected)
    content = ''.join(pieces)
    result = NarrationSelection(content, _digest(content), document.source_sha256,
        _canonical(document.source_anchor), signature, document.narration_policy_sha256, first, last, tuple(slices), tuple(skipped))
    result.check(document)
    check_cancelled(cancelled)
    return result
