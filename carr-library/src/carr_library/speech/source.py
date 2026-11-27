"""Read authorized source positions for an explicitly requested narration copy."""
from dataclasses import dataclass
from hashlib import sha256

from ..reader.reading_state import ReadingLocation
from ._io import check_cancelled, text_value


def validate_source_anchor(text, value):
    """Check portable lineage, never current access or quotation authority.

    ReadingLocation supplies the existing source/locator/digest validation.
    The original-page hashes also bind the offsets in the narration source;
    separators inserted by read_source_range are explicitly derived structure.
    A valid imported record is still an untrusted provenance claim until the
    ordinary library services resolve its exact retained source afresh.
    """
    text_value(text, 'narration source', empty=True)
    fields = {'schema', 'workspace', 'edition', 'revision', 'asset_sha256',
              'positions', 'derived_separators', 'quotation_authority'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'carr.narration-source.v1'
            or value['quotation_authority'] is not False):
        raise ValueError('Invalid narration-source anchor.')
    positions = value['positions']
    if type(positions) is not list or not 1 <= len(positions) <= 10000:
        raise ValueError('Invalid narration-source position count.')
    normalized, separators, previous = [], [], None
    for position in positions:
        if (type(position) is not dict or set(position) != {'ordinal', 'kind', 'index',
                'start', 'end', 'text_sha256', 'text_verified_at_read'}):
            raise ValueError('Invalid narration-source position fields.')
        ReadingLocation(value['workspace'], value['edition'], value['revision'],
                        position['kind'], position['index'], position['text_sha256'],
                        value['asset_sha256'])
        if (type(position['ordinal']) is not int or not 1 <= position['ordinal'] <= 10000
                or type(position['start']) is not int or type(position['end']) is not int
                or not 0 <= position['start'] <= position['end'] <= len(text)
                or type(position['text_verified_at_read']) is not bool):
            raise ValueError('Invalid narration-source offsets or verification state.')
        if previous is None:
            if position['start'] != 0:
                raise ValueError('Narration-source positions must cover the complete source.')
        else:
            if (position['ordinal'] != previous['ordinal'] + 1
                    or position['kind'] != previous['kind']
                    or position['index'] <= previous['index']
                    or position['start'] != previous['end'] + 2
                    or text[previous['end']:position['start']] != '\n\n'):
                raise ValueError('Narration-source positions or separators are inconsistent.')
            separators.append({'start': previous['end'], 'end': position['start']})
        if sha256(text[position['start']:position['end']].encode('utf-8')).hexdigest() != position['text_sha256']:
            raise ValueError('Narration-source page checksum mismatch.')
        normalized.append(dict(position))
        previous = position
    if previous['end'] != len(text):
        raise ValueError('Narration-source positions must cover the complete source.')
    supplied = value['derived_separators']
    if (type(supplied) is not list or len(supplied) != len(separators)
            or any(type(row) is not dict or set(row) != {'start', 'end'}
                   or type(row['start']) is not int or type(row['end']) is not int
                   for row in supplied) or supplied != separators):
        raise ValueError('Invalid derived narration separators.')
    return {**value, 'positions': normalized, 'derived_separators': separators}


@dataclass(frozen=True)
class NarrationSource:
    text: str
    title: str
    workspace: str
    edition: str
    revision: str
    positions: tuple[dict, ...]
    warnings: tuple[str, ...]
    asset_sha256: str

    def anchor(self):
        """Return a detached, checked lineage snapshot for a reading project."""
        return validate_source_anchor(self.text, {
            'schema': 'carr.narration-source.v1', 'workspace': self.workspace,
            'edition': self.edition, 'revision': self.revision,
            'asset_sha256': self.asset_sha256, 'positions': [dict(p) for p in self.positions],
            'derived_separators': [{'start': a['end'], 'end': b['start']}
                                   for a, b in zip(self.positions, self.positions[1:])],
            'quotation_authority': False})


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
                          'text_sha256': page.location.text_sha256,
                          'text_verified_at_read': page.verified})
        if not page.verified:
            warnings.append(f'Reading position {ordinal}: extracted text has not been independently approved.')
    guard()
    check_cancelled(cancelled)
    return NarrationSource(''.join(pieces), desk.title, desk.workspace, desk.edition,
                           desk.revision, tuple(positions), tuple(warnings),
                           desk.identity.asset_sha256)
