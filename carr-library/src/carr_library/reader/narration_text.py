"""Explicit, reversible narration edits with immutable source-offset anchors.

This is a reading copy, never quotation evidence. Replacements are literal and
are only applied on request. Changed spans retain the contributing source range
and say that their individual character positions are approximate. Unchanged
spans keep exact offsets, including CR/LF and combining Unicode characters.
"""
from dataclasses import asdict, dataclass
from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import stat

from ..research.export_file import write_export


def text_digest(text):
    if not isinstance(text, str):
        raise ValueError('Text must be a Unicode string.')
    try:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except UnicodeError as exc:
        raise ValueError('Text contains an invalid Unicode surrogate.') from exc


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field.')
        result[key] = value
    return result


def decode_json(text):
    try:
        return json.loads(text, object_pairs_hook=unique_json,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValueError('Non-finite JSON values are not supported.')))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError('The saved document is not valid JSON.') from exc


def read_document(path, *, byte_budget=64 * 1024 * 1024):
    """Read a selected regular local file within the caller's memory budget."""
    if type(byte_budget) is not int or byte_budget <= 0:
        raise ValueError('A positive document loading budget is required.')
    selected = Path(path)
    info = selected.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise ValueError('Choose a regular local file without links.')
    if info.st_size > byte_budget:
        raise ValueError('This document exceeds the configured loading memory budget.')
    # Opening a regular file in this local desktop workflow is not a sandbox
    # against a hostile process that can replace the directory simultaneously.
    with selected.open('rb') as stream:
        payload = stream.read(byte_budget + 1)
    if len(payload) > byte_budget:
        raise ValueError('This document exceeds the configured loading memory budget.')
    return payload


@dataclass(frozen=True)
class NarrationSpan:
    reading_start: int
    reading_end: int
    source_start: int
    source_end: int
    exact: bool


@dataclass(frozen=True)
class _Piece:
    text: str
    start: int
    end: int
    exact: bool

    def part(self, start, end):
        if self.exact:
            return _Piece(self.text[start:end], self.start + start, self.start + end, True)
        return _Piece(self.text[start:end], self.start, self.end, False)


@dataclass(frozen=True)
class NarrationEdit:
    start: int
    end: int
    before: str
    after: str
    reason: str
    substitutions: tuple = ()


class NarrationDraft:
    """A source-pinned reading derivative, with validated replay and undo/redo."""

    def __init__(self, source_text, *, source_id='independent-script'):
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 8192:
            raise ValueError('A nonempty source identity is required.')
        self._source = source_text
        self._source_hash = text_digest(source_text)
        self._source_id = source_id
        self._pieces = (_Piece(source_text, 0, len(source_text), True),) if source_text else ()
        self._edits = []
        self._redo = []
        self._states = [self._pieces]

    @property
    def source_text(self):
        return self._source

    @property
    def source_sha256(self):
        return self._source_hash

    @property
    def source_id(self):
        return self._source_id

    @property
    def text(self):
        return ''.join(piece.text for piece in self._pieces)

    @property
    def edits(self):
        return tuple(self._edits)

    @property
    def can_undo(self):
        return bool(self._edits)

    @property
    def can_redo(self):
        return bool(self._redo)

    def assert_source(self, source_text=None, *, source_sha256=None):
        actual = text_digest(self._source if source_text is None else source_text)
        if actual != self._source_hash or (source_sha256 is not None and source_sha256 != actual):
            raise ValueError('The original source has changed. Open its new revision separately.')

    def spans(self):
        offset = 0
        result = []
        for piece in self._pieces:
            result.append(NarrationSpan(offset, offset + len(piece.text), piece.start, piece.end, piece.exact))
            offset += len(piece.text)
        return tuple(result)

    def source_range(self, start, end):
        """Return contributing source bounds, never pretend a rewrite is exact."""
        self._range(start, end)
        if start == end:
            for item in self.spans():
                if item.reading_start <= start <= item.reading_end:
                    if item.exact:
                        pos = item.source_start + start - item.reading_start
                        return pos, pos, True
                    return item.source_start, item.source_end, False
            return len(self._source), len(self._source), True
        pieces = [item for item in self.spans() if item.reading_end > start and item.reading_start < end]
        mapped = []
        for item in pieces:
            first = item.source_start + max(0, start - item.reading_start) if item.exact else item.source_start
            last = item.source_end - max(0, item.reading_end - end) if item.exact else item.source_end
            mapped.append((first, last))
        contiguous = all(left[1] == right[0] for left, right in zip(mapped, mapped[1:]))
        return min(item[0] for item in mapped), max(item[1] for item in mapped), (
            all(item.exact for item in pieces) and contiguous)

    def _range(self, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(self.text):
            raise ValueError('Choose a valid range in the reading copy.')

    def _slice(self, first, last):
        result, offset = [], 0
        for piece in self._pieces:
            end = offset + len(piece.text)
            if end > first and offset < last:
                result.append(piece.part(max(0, first - offset), min(len(piece.text), last - offset)))
            offset = end
        return result

    def replace_range(self, start, end, replacement, *, reason='Manual reading edit'):
        self.assert_source()
        self._range(start, end)
        text_digest(replacement)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise ValueError('Describe the reading edit in at most 256 characters.')
        current = self.text
        before = current[start:end]
        if before == replacement:
            return False
        source_start, source_end, _ = self.source_range(start, end)
        updated = self._slice(0, start)
        if replacement:
            updated.append(_Piece(replacement, source_start, source_end, False))
        updated.extend(self._slice(end, len(current)))
        self._pieces = tuple(updated)
        self._edits.append(NarrationEdit(start, end, before, replacement, reason))
        self._states.append(self._pieces)
        self._redo.clear()
        return True

    def edit_text(self, text, *, reason='Manual reading edit'):
        """Keep exact common prefix/suffix anchors without an expensive global diff."""
        text_digest(text)
        previous = self.text
        if previous == text:
            return False
        start = 0
        limit = min(len(previous), len(text))
        while start < limit and previous[start] == text[start]:
            start += 1
        tail = 0
        while (tail < limit - start and previous[-tail - 1] == text[-tail - 1]):
            tail += 1
        return self.replace_range(start, len(previous) - tail,
                                  text[start:len(text) - tail if tail else None], reason=reason)

    def replace_literal(self, needle, replacement, *, all_occurrences=True):
        text_digest(needle)
        text_digest(replacement)
        if not needle:
            raise ValueError('Enter the exact word or phrase to replace.')
        if type(all_occurrences) is not bool:
            raise ValueError('Choose whether to replace one occurrence or all occurrences.')
        if needle == replacement:
            return 0
        original = self.text
        positions, cursor = [], 0
        while True:
            at = original.find(needle, cursor)
            if at < 0:
                break
            positions.append(at)
            cursor = at + len(needle)
            if not all_occurrences:
                break
        if len(positions) == 1:
            at = positions[0]
            self.replace_range(at, at + len(needle), replacement, reason='Literal pronunciation replacement')
        elif positions:
            self._replace_many(tuple((at, at + len(needle), needle, replacement) for at in positions),
                               reason='Literal pronunciation replacement')
        return len(positions)

    def _replace_many(self, substitutions, *, reason):
        """Apply one Replace-all command in bounded linear-piece storage."""
        self.assert_source()
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise ValueError('Describe the reading edit in at most 256 characters.')
        current = self.text
        offsets, total = [], 0
        for piece in self._pieces:
            offsets.append(total)
            total += len(piece.text)

        def sliced(first, last):
            if first == last:
                return []
            index = max(0, bisect_right(offsets, first) - 1)
            result = []
            while index < len(self._pieces) and offsets[index] < last:
                piece, at = self._pieces[index], offsets[index]
                result.append(piece.part(max(0, first - at), min(len(piece.text), last - at)))
                index += 1
            return result

        updated, cursor = [], 0
        for item in substitutions:
            if not isinstance(item, (tuple, list)) or len(item) != 4:
                raise ValueError('Invalid literal replacement history.')
            first, last, before, after = item
            if (type(first) is not int or type(last) is not int or not cursor <= first < last <= len(current)
                    or not isinstance(before, str) or current[first:last] != before):
                raise ValueError('Literal replacement does not match its source history.')
            text_digest(after)
            if before == after:
                raise ValueError('A saved narration edit cannot be empty.')
            updated.extend(sliced(cursor, first))
            source = sliced(first, last)
            if after:
                updated.append(_Piece(after, min(piece.start for piece in source),
                                      max(piece.end for piece in source), False))
            cursor = last
        if not substitutions:
            raise ValueError('A replacement batch cannot be empty.')
        updated.extend(sliced(cursor, len(current)))
        first, last = substitutions[0][0], substitutions[-1][1]
        middle, cursor = [], first
        for begin, end, _, after in substitutions:
            middle.extend((current[cursor:begin], after))
            cursor = end
        edit = NarrationEdit(first, last, current[first:last], ''.join(middle), reason,
                             tuple(tuple(item) for item in substitutions))
        self._pieces = tuple(updated)
        self._edits.append(edit)
        self._states.append(self._pieces)
        self._redo.clear()
        return True

    def undo(self):
        if not self._edits:
            return False
        self._redo.append(self._edits.pop())
        self._states.pop()
        self._pieces = self._states[-1]
        return True

    def redo(self):
        if not self._redo:
            return False
        edit = self._redo.pop()
        remaining = list(self._redo)
        if self.text[edit.start:edit.end] != edit.before:
            raise ValueError('The reading history no longer matches this draft.')
        if edit.substitutions:
            self._replace_many(edit.substitutions, reason=edit.reason)
        else:
            self.replace_range(edit.start, edit.end, edit.after, reason=edit.reason)
        self._redo = remaining
        return True

    def difference_text(self):
        if not self._edits:
            return 'The reading copy matches the immutable original text.'
        lines = ['Reading-copy changes only. The original and saved quotations are unchanged.']
        for number, edit in enumerate(self._edits, 1):
            lines.extend(('', f'{number}. {edit.reason}; reading offsets {edit.start} to {edit.end}.',
                          'Before: ' + (edit.before or '(empty)'), 'After: ' + (edit.after or '(empty)')))
        return '\n'.join(lines)

    def to_dict(self):
        self.assert_source()
        return {'kind': 'research-narration-draft', 'version': 1,
                'source_id': self._source_id, 'source_text': self._source,
                'source_sha256': self._source_hash, 'reading_sha256': text_digest(self.text),
                'edits': [asdict(edit) for edit in self._edits]}

    @classmethod
    def from_dict(cls, value, *, source_text=None, source_id=None):
        keys = {'kind', 'version', 'source_id', 'source_text', 'source_sha256', 'reading_sha256', 'edits'}
        if (type(value) is not dict or set(value) != keys or value['kind'] != 'research-narration-draft'
                or type(value['version']) is not int or value['version'] != 1
                or type(value['edits']) is not list):
            raise ValueError('Unsupported narration draft.')
        draft = cls(value['source_text'], source_id=value['source_id'])
        draft.assert_source(source_text, source_sha256=value['source_sha256'])
        if source_id is not None and source_id != draft.source_id:
            raise ValueError('The draft belongs to a different source.')
        edit_keys = {'start', 'end', 'before', 'after', 'reason'}
        for item in value['edits']:
            if type(item) is not dict or set(item) not in (edit_keys, edit_keys | {'substitutions'}):
                raise ValueError('Invalid narration edit.')
            draft._range(item['start'], item['end'])
            if draft.text[item['start']:item['end']] != item['before']:
                raise ValueError('Narration edit does not match its source history.')
            substitutions = item.get('substitutions', ())
            if not isinstance(substitutions, (tuple, list)):
                raise ValueError('Invalid literal replacement history.')
            if substitutions:
                draft._replace_many(substitutions, reason=item['reason'])
                actual = draft.edits[-1]
                if (actual.start, actual.end, actual.before, actual.after) != (
                        item['start'], item['end'], item['before'], item['after']):
                    raise ValueError('Literal replacement summary does not match its history.')
            elif not draft.replace_range(item['start'], item['end'], item['after'], reason=item['reason']):
                raise ValueError('A saved narration edit cannot be empty.')
        if text_digest(draft.text) != value['reading_sha256']:
            raise ValueError('The saved reading copy failed its content check.')
        return draft

    def save(self, path, *, private_root, source_text=None, overwrite=False, cancelled=lambda: False):
        self.assert_source(source_text)
        selected = Path(path)
        if selected.exists() and not overwrite:
            raise FileExistsError('Choose a new draft filename or explicitly allow replacement.')
        write_export(selected, json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + '\n',
                     private_root=private_root, cancelled=cancelled)

    @classmethod
    def load(cls, path, *, source_text, source_id=None, byte_budget=64 * 1024 * 1024):
        payload = read_document(path, byte_budget=byte_budget).decode('utf-8')
        return cls.from_dict(decode_json(payload), source_text=source_text, source_id=source_id)
