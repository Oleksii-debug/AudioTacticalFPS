"""Editable narration copies with immutable source and explicit origin mapping.

Only retained source pieces have source offsets. Replacements and additions have
no quotation authority, even when they happen to contain the same words. This
module never opens or changes the research catalogue, citations or original file.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class OriginRun:
    derived_start: int
    derived_end: int
    source_start: int | None
    source_end: int | None


@dataclass(frozen=True)
class ReadingChange:
    source_start: int
    source_end: int
    derived_start: int
    derived_end: int
    original: str
    replacement: str


@dataclass(frozen=True)
class _Piece:
    start: int | None = None
    end: int | None = None
    inserted: str | None = None

    @property
    def length(self):
        return len(self.inserted) if self.inserted is not None else self.end - self.start

    def slice(self, start: int, end: int) -> '_Piece':
        if self.inserted is not None:
            return _Piece(inserted=self.inserted[start:end])
        return _Piece(self.start + start, self.start + end)


def _text(value: str, limit: int, label: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{label} must be text within the configured {limit}-character document budget.')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise ValueError(f'{label} must contain valid Unicode.') from exc
    return value


def _join(pieces) -> tuple[_Piece, ...]:
    result, pending = [], []
    for piece in pieces:
        if not piece.length:
            continue
        if piece.inserted is not None:
            pending.append(piece.inserted)
            continue
        if pending:
            result.append(_Piece(inserted=''.join(pending)))
            pending = []
        if result and result[-1].inserted is None and result[-1].end == piece.start:
            result[-1] = _Piece(result[-1].start, piece.end)
        else:
            result.append(piece)
    if pending:
        result.append(_Piece(inserted=''.join(pending)))
    return tuple(result)


class ReadingDerivative:
    """Versioned manual reading copy, using sparse immutable-source pieces.

    ``max_text_chars`` and ``max_history`` are configurable in-memory editing
    budgets, not audiobook duration limits. The production queue can use many
    chapter/segment documents. Plain replacements are literal and deterministic;
    automatic language corrections, regex execution and source approval are not
    performed here.
    """

    def __init__(self, source: str, *, max_text_chars: int = 2_000_000,
                 max_history: int = 1000):
        if type(max_text_chars) is not int or max_text_chars < 1:
            raise ValueError('Text budget must be a positive integer.')
        if type(max_history) is not int or max_history < 1:
            raise ValueError('History budget must be a positive integer.')
        self.max_text_chars = max_text_chars
        self.max_history = max_history
        self._source = _text(source, max_text_chars, 'Original source')
        self._source_sha256 = hashlib.sha256(source.encode('utf-8')).hexdigest()
        self._history = [(_Piece(0, len(source)),) if source else ()]
        self._cursor = 0
        self._revision = 0

    @property
    def source(self) -> str:
        return self._source

    @property
    def source_sha256(self) -> str:
        return self._source_sha256

    @property
    def revision(self) -> int:
        """Monotonic action revision, including undo and redo operations."""
        return self._revision

    @property
    def _pieces(self) -> tuple[_Piece, ...]:
        return self._history[self._cursor]

    @property
    def text(self) -> str:
        return ''.join(piece.inserted if piece.inserted is not None else self.source[piece.start:piece.end]
                       for piece in self._pieces)

    @property
    def modified(self) -> bool:
        return self.text != self.source

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor + 1 < len(self._history)

    @property
    def source_offsets(self) -> tuple[OriginRun, ...]:
        result, position = [], 0
        for piece in self._pieces:
            result.append(OriginRun(position, position + piece.length, piece.start, piece.end))
            position += piece.length
        return tuple(result)

    def render(self, mode: str = 'enhanced') -> str:
        if mode == 'original':
            return self.source
        if mode == 'enhanced':
            return self.text
        raise ValueError('Reading mode must be original or enhanced.')

    def source_span(self, start: int, end: int) -> tuple[int, int] | None:
        """Map a contiguous retained source span; inserted/corrected text is None.

        This mapping describes provenance, not permission to quote or approval.
        Existing research APIs must authenticate any actual source quotation.
        """
        self._range(start, end)
        if start == end:
            return None
        first = last = None
        position = 0
        for piece in self._pieces:
            following = position + piece.length
            left, right = max(start, position), min(end, following)
            if left < right:
                if piece.start is None:
                    return None
                origin = piece.start + left - position
                if first is None:
                    first = origin
                elif origin != last:
                    return None
                last = piece.start + right - position
            position = following
            if position >= end:
                break
        return (first, last) if first is not None else None

    def _range(self, start, end):
        length = sum(piece.length for piece in self._pieces)
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= length:
            raise ValueError('Reading-copy edit offsets are outside the current text.')

    def _slice(self, start, end):
        position = 0
        for piece in self._pieces:
            following = position + piece.length
            left, right = max(position, start), min(end, following)
            if left < right:
                yield piece.slice(left - position, right - position)
            position = following
            if position >= end:
                break

    def _commit(self, pieces):
        pieces = _join(pieces)
        if pieces == self._pieces:
            return False
        if sum(piece.length for piece in pieces) > self.max_text_chars:
            raise ValueError('Reading copy would exceed its configured text budget.')
        # Never silently lose the ability to undo earlier edits.
        if self._cursor + 1 >= self.max_history:
            raise ValueError('Reading-copy history budget reached; save a reviewed new derivative before continuing.')
        insertion_size = sum(len(piece.inserted) for snapshot in self._history[:self._cursor + 1]
                             for piece in snapshot if piece.inserted is not None)
        insertion_size += sum(len(piece.inserted) for piece in pieces if piece.inserted is not None)
        if insertion_size > self.max_text_chars * min(self.max_history, 16):
            raise ValueError('Reading-copy history exceeds its configured memory budget.')
        self._history = self._history[:self._cursor + 1] + [pieces]
        self._cursor += 1
        self._revision += 1
        return True

    def replace(self, start: int, end: int, replacement: str) -> bool:
        self._range(start, end)
        replacement = _text(replacement, self.max_text_chars, 'Replacement')
        current = self.text
        if current[start:end] == replacement:
            return False
        return self._commit((*self._slice(0, start), _Piece(inserted=replacement),
                             *self._slice(end, len(current))))

    def substitute(self, old: str, new: str, count: int = -1) -> int:
        """One undoable, case-sensitive literal pronunciation substitution."""
        old = _text(old, self.max_text_chars, 'Search text')
        new = _text(new, self.max_text_chars, 'Pronunciation text')
        if not old:
            raise ValueError('Literal pronunciation search must not be empty.')
        if type(count) is not int or count < -1:
            raise ValueError('Substitution count must be -1 or a nonnegative integer.')
        if not count or old == new:
            return 0
        current, cursor, matches, pieces = self.text, 0, 0, []
        available = current.count(old)
        replacement_count = available if count == -1 else min(count, available)
        if len(current) + replacement_count * (len(new) - len(old)) > self.max_text_chars:
            raise ValueError('Reading copy would exceed its configured text budget.')
        original_pieces, boundaries, position = self._pieces, [], 0
        for piece in original_pieces:
            boundaries.append(position)
            position += piece.length

        def retained(start, end):
            if start == end:
                return
            index = bisect_right(boundaries, start) - 1
            while index < len(original_pieces) and boundaries[index] < end:
                piece = original_pieces[index]
                offset = boundaries[index]
                yield piece.slice(max(0, start - offset), min(piece.length, end - offset))
                index += 1

        while count == -1 or matches < count:
            index = current.find(old, cursor)
            if index < 0:
                break
            pieces.extend(retained(cursor, index))
            if new:
                pieces.append(_Piece(inserted=new))
            cursor = index + len(old)
            matches += 1
        if matches:
            pieces.extend(retained(cursor, len(current)))
            self._commit(pieces)
        return matches

    def undo(self) -> bool:
        if not self.can_undo:
            return False
        self._cursor -= 1
        self._revision += 1
        return True

    def redo(self) -> bool:
        if not self.can_redo:
            return False
        self._cursor += 1
        self._revision += 1
        return True

    def reset(self) -> bool:
        """Discard enhancements as an undoable action; original bytes stay fixed."""
        return self._commit((_Piece(0, len(self.source)),) if self.source else ())

    def diff(self) -> tuple[ReadingChange, ...]:
        changes, inserted = [], []
        source_pos = derived_pos = gap_start = 0
        for piece in self._pieces:
            if piece.inserted is not None:
                inserted.append(piece.inserted)
                derived_pos += piece.length
                continue
            if piece.start != source_pos or inserted:
                changes.append(ReadingChange(source_pos, piece.start, gap_start,
                    derived_pos, self.source[source_pos:piece.start], ''.join(inserted)))
            inserted = []
            source_pos = piece.end
            derived_pos += piece.length
            gap_start = derived_pos
        if source_pos != len(self.source) or inserted:
            changes.append(ReadingChange(source_pos, len(self.source), gap_start,
                derived_pos, self.source[source_pos:], ''.join(inserted)))
        return tuple(changes)

    def to_dict(self) -> dict:
        return {'schema': 1, 'source': self.source, 'source_sha256': self.source_sha256,
                'history': [[{'inserted': piece.inserted} if piece.inserted is not None else
                             {'start': piece.start, 'end': piece.end} for piece in snapshot]
                            for snapshot in self._history],
                'cursor': self._cursor, 'revision': self.revision}

    @classmethod
    def from_dict(cls, value: dict, *, max_text_chars=2_000_000, max_history=1000) -> 'ReadingDerivative':
        keys = {'schema', 'source', 'source_sha256', 'history', 'cursor', 'revision'}
        if not isinstance(value, dict) or set(value) != keys or type(value['schema']) is not int or value['schema'] != 1:
            raise ValueError('Invalid reading-derivative schema.')
        result = cls(value['source'], max_text_chars=max_text_chars, max_history=max_history)
        if value['source_sha256'] != result.source_sha256:
            raise ValueError('Reading-copy original source hash does not match.')
        history = value['history']
        if not isinstance(history, list) or not 1 <= len(history) <= max_history:
            raise ValueError('Invalid reading-copy history.')
        snapshots = []
        total_inserted = 0
        for snapshot in history:
            if not isinstance(snapshot, list) or len(snapshot) > max_text_chars + 1:
                raise ValueError('Invalid reading-copy snapshot.')
            pieces, previous_end, length = [], 0, 0
            for row in snapshot:
                if not isinstance(row, dict):
                    raise ValueError('Invalid reading-copy piece.')
                if set(row) == {'start', 'end'}:
                    start, end = row['start'], row['end']
                    if type(start) is not int or type(end) is not int or not previous_end <= start < end <= len(result.source):
                        raise ValueError('Source mapping must retain ordered, nonoverlapping original spans.')
                    previous_end = end
                    piece = _Piece(start, end)
                elif set(row) == {'inserted'}:
                    inserted = _text(row['inserted'], max_text_chars, 'Inserted reading text')
                    if not inserted:
                        raise ValueError('Empty reading-copy pieces are invalid.')
                    total_inserted += len(inserted)
                    if total_inserted > max_text_chars * min(max_history, 16):
                        raise ValueError('Serialized reading history exceeds its memory budget.')
                    piece = _Piece(inserted=inserted)
                else:
                    raise ValueError('Invalid reading-copy piece fields.')
                length += piece.length
                if length > max_text_chars:
                    raise ValueError('Reading-copy snapshot exceeds the text budget.')
                pieces.append(piece)
            snapshots.append(_join(pieces))
        initial = (_Piece(0, len(result.source)),) if result.source else ()
        if snapshots[0] != initial:
            raise ValueError('Reading-copy history must begin with the exact immutable original.')
        cursor, revision = value['cursor'], value['revision']
        if type(cursor) is not int or not 0 <= cursor < len(snapshots):
            raise ValueError('Invalid reading-copy history position.')
        if type(revision) is not int or revision < len(snapshots) - 1:
            raise ValueError('Invalid reading-copy revision.')
        result._history, result._cursor, result._revision = snapshots, cursor, revision
        return result
