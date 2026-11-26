"""Read authorized source positions for an explicitly requested narration copy."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from ._io import check_cancelled


@dataclass(frozen=True)
class NarrationSource:
    text: str
    title: str
    workspace: str
    edition: str
    revision: str
    positions: tuple[dict, ...]
    warnings: tuple[str, ...]

    @property
    def identity(self):
        return source_key(self.workspace, self.edition, self.revision,
                          self.positions[0]['ordinal'], self.positions[-1]['ordinal'])

    @property
    def binding(self):
        return {'kind': 'library-source-range', 'workspace': self.workspace, 'edition': self.edition,
                'revision': self.revision, 'positions': list(self.positions), 'separator': '\n\n'}


def source_key(workspace, edition, revision, first, last):
    raw = json.dumps([workspace, edition, revision, first, last], ensure_ascii=False,
                     separators=(',', ':')).encode('utf-8')
    return 'library-range:' + sha256(raw).hexdigest()


def validate_source_binding(value, source_text):
    if (type(value) is not dict or set(value) != {'kind', 'workspace', 'edition', 'revision', 'positions', 'separator'}
            or value['kind'] != 'library-source-range' or value['separator'] != '\n\n'):
        raise ValueError('Invalid reading-source binding.')
    for name in ('workspace', 'edition', 'revision'):
        if not isinstance(value[name], str) or not value[name].strip() or len(value[name]) > 1024:
            raise ValueError('Invalid reading-source identity.')
    positions = value['positions']
    if type(positions) is not list or not 1 <= len(positions) <= 10000:
        raise ValueError('Invalid reading-source position inventory.')
    end, previous, asset = 0, None, None
    for number, item in enumerate(positions):
        fields = {'ordinal', 'kind', 'index', 'start', 'end', 'text_sha256', 'asset_sha256'}
        if (type(item) is not dict or set(item) != fields
                or any(type(item[name]) is not int for name in ('ordinal', 'index', 'start', 'end'))
                or item['ordinal'] < 1 or item['index'] < 1
                or item['kind'] not in ('physical_page', 'logical_segment')
                or item['start'] != end + (2 if number else 0)
                or not item['start'] <= item['end'] <= len(source_text)
                or (previous is not None and item['ordinal'] != previous + 1)):
            raise ValueError('Invalid reading-source offsets or positions.')
        for name in ('text_sha256', 'asset_sha256'):
            if not isinstance(item[name], str) or not re.fullmatch('[0-9a-f]{64}', item[name]):
                raise ValueError('Invalid reading-source digest.')
        if (source_text[end:item['start']] != ('\n\n' if number else '')
                or sha256(source_text[item['start']:item['end']].encode('utf-8')).hexdigest() != item['text_sha256']
                or (asset is not None and asset != item['asset_sha256'])):
            raise ValueError('The reading source no longer matches its exact positions.')
        end, previous, asset = item['end'], item['ordinal'], item['asset_sha256']
    if end != len(source_text):
        raise ValueError('Reading-source positions do not cover the source text.')
    return source_key(value['workspace'], value['edition'], value['revision'],
                      positions[0]['ordinal'], positions[-1]['ordinal'])


def read_source_range(desk, first, last, *, guard=lambda: None,
                      cancelled=lambda: False, max_characters=2_000_000):
    """Retain real source positions; inserted separators are narration structure.

    ReadingDesk performs the existing fresh authorization and original/revision
    checks for each page. This does not approve extracted text or printed labels.
    """
    if (type(first) is not int or type(last) is not int
            or not 1 <= first <= last <= len(desk.pages)):
        raise ValueError('Choose a valid first and last source reading position.')
    if type(max_characters) is not int or not 1 <= max_characters <= 2_000_000:
        raise ValueError('Choose a positive bounded narration-copy character budget.')
    pieces, positions, warnings, offset = [], [], [], 0
    for ordinal in range(first, last + 1):
        guard()
        check_cancelled(cancelled)
        page = desk.page(ordinal)
        if pieces:
            pieces.append('\n\n')
            offset += 2
        if offset + len(page.raw_text) > max_characters:
            raise ValueError('This source selection exceeds the narration-copy budget; choose fewer chapters or positions.')
        start = offset
        pieces.append(page.raw_text)
        offset += len(page.raw_text)
        positions.append({'ordinal': page.ordinal, 'kind': page.location.kind,
                          'index': page.location.index, 'start': start, 'end': offset,
                          'text_sha256': sha256(page.raw_text.encode('utf-8')).hexdigest(),
                          'asset_sha256': page.location.asset_sha256})
        if not page.verified:
            warnings.append(f'Reading position {ordinal}: extracted text has not been independently approved.')
    guard()
    check_cancelled(cancelled)
    return NarrationSource(''.join(pieces), desk.title, desk.workspace, desk.edition,
                           desk.revision, tuple(positions), tuple(warnings))
