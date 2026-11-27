"""Read authorized source positions for an explicitly requested narration copy."""
import copy
from dataclasses import dataclass
from hashlib import sha256

from ._io import check_cancelled
from ..reader.reading_state import ReadingLocation


def validate_source_origin(value, source_text):
    """Validate portable lineage, never quotation authority or current access.

    Identity comes from the already authorized ReadingDesk. On reopening, these
    fields remain historical observations; only the existing library resolver
    can authorize a live source or an exact quotation. No file path is trusted.
    """
    if value is None:
        return None
    fields = {'schema', 'workspace', 'edition', 'revision', 'asset_sha256',
              'positions', 'derived_separators', 'quotation_authority'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'carr.narration-source.v1'
            or value['quotation_authority'] is not False):
        raise ValueError('Invalid narration-source identity fields.')
    positions = value['positions']
    if type(positions) is not list or not 1 <= len(positions) <= 10000:
        raise ValueError('Invalid narration-source position count.')
    keys = {'ordinal', 'kind', 'index', 'start', 'end', 'text_sha256', 'text_verified'}
    copied, separators, previous = [], [], None
    for row in positions:
        if type(row) is not dict or set(row) != keys:
            raise ValueError('Invalid narration-source position fields.')
        ReadingLocation(value['workspace'], value['edition'], value['revision'],
                        row['kind'], row['index'], row['text_sha256'], value['asset_sha256'])
        if (type(row['ordinal']) is not int or not 1 <= row['ordinal'] <= 10000
                or type(row['start']) is not int or type(row['end']) is not int
                or not 0 <= row['start'] <= row['end'] <= len(source_text)
                or type(row['text_verified']) is not bool):
            raise ValueError('Invalid narration-source position or offsets.')
        expected_start = previous['end'] + 2 if previous is not None else 0
        if row['start'] != expected_start:
            raise ValueError('Narration-source positions do not cover the source.')
        if previous is not None:
            if (row['ordinal'] != previous['ordinal'] + 1 or row['kind'] != previous['kind']
                    or row['index'] <= previous['index']
                    or source_text[previous['end']:row['start']] != '\n\n'):
                raise ValueError('Invalid narration-source sequence or derived separator.')
            separators.append({'start': previous['end'], 'end': row['start']})
        if sha256(source_text[row['start']:row['end']].encode('utf-8')).hexdigest() != row['text_sha256']:
            raise ValueError('Narration-source position checksum mismatch.')
        copied.append(dict(row))
        previous = row
    if previous['end'] != len(source_text):
        raise ValueError('Narration-source positions do not cover the source.')
    if (type(value['derived_separators']) is not list
            or len(value['derived_separators']) != len(separators)
            or any(type(row) is not dict or set(row) != {'start', 'end'}
                   or type(row['start']) is not int or type(row['end']) is not int
                   for row in value['derived_separators'])
            or value['derived_separators'] != separators):
        raise ValueError('Invalid narration-source derived separators.')
    return {**value, 'positions': copied, 'derived_separators': separators}


def _source_anchor_from_origin(value, text):
    """Accept the earlier origin-only v2 format without inventing provenance."""
    origin = validate_source_origin(value, text)
    if origin is None:
        return None
    return {key: origin[key] for key in ('workspace', 'edition', 'revision',
            'asset_sha256', 'quotation_authority')} | {
        'positions': [{**{key: item for key, item in row.items() if key != 'text_verified'},
                       'text_verified_at_capture': row['text_verified']}
                      for row in origin['positions']]}


def _source_origin_from_anchor(anchor):
    """Return the stable legacy identity view of an already validated anchor.

    Recovery filenames use this exact representation. Keep its historical
    verification flags and explicit derived separators when normalizing v2.
    """
    if anchor is None:
        return None
    positions = [{**{key: item for key, item in row.items()
                     if key != 'text_verified_at_capture'},
                  'text_verified': row['text_verified_at_capture']}
                 for row in anchor['positions']]
    return {'schema': 'carr.narration-source.v1',
            **{key: anchor[key] for key in ('workspace', 'edition', 'revision',
               'asset_sha256', 'quotation_authority')},
            'positions': positions,
            'derived_separators': [{'start': first['end'], 'end': second['start']}
                                   for first, second in zip(positions, positions[1:])]}


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

    @property
    def locations(self):
        from .session import document_source_locations
        return document_source_locations(self.reading_document())

    @property
    def source_anchor(self):
        from .reading import _validated_source_anchor
        return _validated_source_anchor({
            'workspace': self.workspace, 'edition': self.edition, 'revision': self.revision,
            'asset_sha256': self.asset_sha256, 'positions': list(self.positions),
            'quotation_authority': False}, self.text)

    @property
    def origin(self):
        return _source_origin_from_anchor(self.source_anchor)

    def reading_document(self, recovered=None):
        """Construct the existing editable copy without guessing identity from text.

        A mismatched or legacy unbound draft is kept intact for explicit recovery;
        it must not silently acquire the current edition or lose its user edits.
        """
        from .reading import ReadingDocument
        anchor = self.source_anchor
        if recovered is not None:
            if (not isinstance(recovered, ReadingDocument) or recovered.source_text != self.text
                    or recovered.source_anchor != anchor):
                raise ValueError('Saved narration draft has a different or unbound source identity. '
                                 'Its edits were preserved; it was not attached to this source.')
            return recovered
        return ReadingDocument(self.text, title=self.title, source_anchor=anchor)


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
    desk = copy.copy(desk)
    desk.cancelled = cancelled
    desk.timeout = min(30.0, desk.timeout)
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
                          'text_verified_at_capture': page.verified})
        if not page.verified:
            warnings.append(f'Reading position {ordinal}: extracted text has not been independently approved.')
    guard()
    check_cancelled(cancelled)
    return NarrationSource(''.join(pieces), desk.title, desk.workspace, desk.edition,
                           desk.revision, tuple(positions), tuple(warnings), desk.identity.asset_sha256)
