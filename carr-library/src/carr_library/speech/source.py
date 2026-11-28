"""Read authorized source positions for an explicitly requested narration copy."""
from dataclasses import dataclass
from hashlib import sha256

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
    asset_sha256: str

    def reading_document(self):
        """Build the existing editable document with explicit, non-authorizing lineage."""
        from .reading import ReadingDocument
        return ReadingDocument(self.text, title=self.title, source_anchor={
            'workspace': self.workspace, 'edition': self.edition, 'revision': self.revision,
            'asset_sha256': self.asset_sha256, 'positions': list(self.positions),
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
                          'text_sha256': sha256(page.raw_text.encode('utf-8')).hexdigest(),
                          'text_verified_at_capture': page.verified})
        if not page.verified:
            warnings.append(f'Reading position {ordinal}: extracted text has not been independently approved.')
    guard()
    check_cancelled(cancelled)
    return NarrationSource(''.join(pieces), desk.title, desk.workspace, desk.edition,
                           desk.revision, tuple(positions), tuple(warnings), desk.identity.asset_sha256)


def attest_saved_narration_copy(library, document, workspace, *, topic=None,
                                guard=lambda: None, cancelled=lambda: False):
    """Resolve a portable copy against an explicitly authorized current scope.

    The anchor is inert input, never a grant to open its claimed workspace.
    Capture-time verification is historical; this check does not authorize a
    quotation or change any part of the saved reading copy.
    """
    from ..reader.reading_desk import ReadingDesk

    check_cancelled(cancelled)
    guard()
    anchor = document.source_anchor
    if library is None or anchor is None or workspace != anchor['workspace']:
        raise ValueError('Open the saved narration source in its authorized library workspace.')
    desk = ReadingDesk(library, workspace, anchor['edition'], topic=topic,
                       revision=anchor['revision'], cancelled=cancelled)
    positions = anchor['positions']
    fresh = read_source_range(desk, positions[0]['ordinal'], positions[-1]['ordinal'],
                              guard=guard, cancelled=cancelled).reading_document()
    actual = fresh.source_anchor
    if (fresh.source_text != document.source_text
            or any(actual[key] != anchor[key] for key in anchor if key != 'positions')
            or len(actual['positions']) != len(positions)):
        raise ValueError('The saved narration source changed.')
    for expected, found in zip(positions, actual['positions']):
        if any(found[key] != expected[key] for key in expected
               if key != 'text_verified_at_capture'):
            raise ValueError('The saved narration source position changed.')
    guard()
    check_cancelled(cancelled)
