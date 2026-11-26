"""Explicit, reversible narration edits with immutable source-offset anchors.

This is a reading copy, never quotation evidence. Replacements are literal and
are only applied on request. Changed spans retain the contributing source range
and say that their individual character positions are approximate. Unchanged
spans keep exact offsets, including CR/LF and combining Unicode characters.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import stat

from ..research.export_file import write_export


MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_HISTORY_EDITS = 1000
MAX_REPLACEMENTS = 100000
MAX_MAPPING_PIECES = 250000
MAX_HISTORY_PIECES = 1000000
MAX_REPLAY_BYTES = 256 * 1024 * 1024


def _reading_bytes(text):
    text_digest(text)
    size = len(text.encode('utf-8'))
    if size > MAX_TEXT_BYTES:
        raise ValueError('The reading copy exceeds its 16 MiB text budget. Use separate chapter drafts.')
    return size


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Reading preparation cancelled; no replacement was applied.')


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
        if start == 0 and end == len(self.text):
            return self
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


@dataclass(frozen=True)
class NarrationLiteralEdit:
    needle: str
    replacement: str
    matches: int
    before_sha256: str
    after_sha256: str
    reason: str = 'Literal pronunciation replacement'


class NarrationDraft:
    """A source-pinned reading derivative, with validated replay and undo/redo."""

    def __init__(self, source_text, *, source_id='independent-script'):
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 8192:
            raise ValueError('A nonempty source identity is required.')
        _reading_bytes(source_text)
        self._source = source_text
        self._source_hash = text_digest(source_text)
        self._source_id = source_id
        self._pieces = (_Piece(source_text, 0, len(source_text), True),) if source_text else ()
        self._edits = []
        self._redo = []
        self._states = [self._pieces]
        self._replay_bytes = [0]

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

    def _commit(self, edit, pieces, current):
        # A saved history is replayed on open. Bound both retained mapping
        # references and replay work before changing any accepted draft state.
        work = self._replay_bytes[-1] + _reading_bytes(current)
        if (len(self._edits) >= MAX_HISTORY_EDITS or work > MAX_REPLAY_BYTES
                or len(pieces) > MAX_MAPPING_PIECES
                or sum(len(state) for state in self._states) + len(pieces) > MAX_HISTORY_PIECES):
            raise ValueError('Reading history exceeds its resource budget. Keep this draft and prepare further changes in a separate chapter copy.')
        self._pieces = tuple(pieces)
        self._edits.append(edit)
        self._states.append(self._pieces)
        self._replay_bytes.append(work)
        self._redo.clear()

    def replace_range(self, start, end, replacement, *, reason='Manual reading edit'):
        self.assert_source()
        self._range(start, end)
        _reading_bytes(replacement)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
            raise ValueError('Describe the reading edit in at most 256 characters.')
        current = self.text
        before = current[start:end]
        if before == replacement:
            return False
        if len(current.encode('utf-8')) - len(before.encode('utf-8')) + len(replacement.encode('utf-8')) > MAX_TEXT_BYTES:
            raise ValueError('The replacement would exceed the reading-copy text budget.')
        source_start, source_end, _ = self.source_range(start, end)
        updated = self._slice(0, start)
        if replacement:
            updated.append(_Piece(replacement, source_start, source_end, False))
        updated.extend(self._slice(end, len(current)))
        self._commit(NarrationEdit(start, end, before, replacement, reason), updated, current)
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

    def replace_literal(self, needle, replacement, *, all_occurrences=True, cancelled=lambda: False):
        """Apply one noncascading batch with linear mapping work and one undo.

        Each unchanged piece retains its exact anchor. Changed pieces refer to
        the full contributing source range, including earlier derived pieces.
        Cancellation and resource errors leave the draft and its history intact.
        """
        _check_cancelled(cancelled)
        _reading_bytes(needle)
        replacement_bytes = _reading_bytes(replacement)
        if not needle:
            raise ValueError('Enter the exact word or phrase to replace.')
        if type(all_occurrences) is not bool:
            raise ValueError('Choose whether to replace one occurrence or all occurrences.')
        if needle == replacement:
            return 0
        self.assert_source()
        original = self.text
        positions, cursor = [], 0
        while True:
            if len(positions) % 512 == 0:
                _check_cancelled(cancelled)
            at = original.find(needle, cursor)
            if at < 0:
                break
            positions.append(at)
            if len(positions) > MAX_REPLACEMENTS:
                raise ValueError('This replacement exceeds the match budget. Apply it to separate chapter drafts.')
            cursor = at + len(needle)
            if not all_occurrences:
                break
        if not positions:
            return 0
        output_bytes = len(original.encode('utf-8')) + len(positions) * (replacement_bytes - len(needle.encode('utf-8')))
        if output_bytes > MAX_TEXT_BYTES:
            raise ValueError('The replacement would exceed the reading-copy text budget.')
        if len(positions) == 1:
            _check_cancelled(cancelled)
            self.replace_range(positions[0], positions[0] + len(needle), replacement,
                               reason='Literal pronunciation replacement')
            return 1

        updated = []
        index = offset = cursor = 0

        def consume(stop, *, preserve):
            nonlocal index, offset, cursor
            first = last = None
            while cursor < stop:
                if index % 512 == 0:
                    _check_cancelled(cancelled)
                piece = self._pieces[index]
                take = min(len(piece.text) - offset, stop - cursor)
                part = piece.part(offset, offset + take)
                first = part.start if first is None else min(first, part.start)
                last = part.end if last is None else max(last, part.end)
                if preserve:
                    updated.append(part)
                cursor += take
                offset += take
                if offset == len(piece.text):
                    index += 1
                    offset = 0
            return first, last

        for number, at in enumerate(positions):
            if number % 512 == 0:
                _check_cancelled(cancelled)
            consume(at, preserve=True)
            first, last = consume(at + len(needle), preserve=False)
            if replacement:
                updated.append(_Piece(replacement, first, last, False))
        consume(len(original), preserve=True)
        result = ''.join(piece.text for piece in updated)
        edit = NarrationLiteralEdit(needle, replacement, len(positions),
                                   text_digest(original), text_digest(result))
        _check_cancelled(cancelled)
        self._commit(edit, updated, original)
        return len(positions)

    def undo(self):
        if not self._edits:
            return False
        self._redo.append(self._edits.pop())
        self._states.pop()
        self._replay_bytes.pop()
        self._pieces = self._states[-1]
        return True

    def redo(self):
        if not self._redo:
            return False
        edit = self._redo[-1]
        remaining = self._redo[:-1]
        if isinstance(edit, NarrationLiteralEdit):
            if text_digest(self.text) != edit.before_sha256:
                raise ValueError('The reading history no longer matches this draft.')
            self.replace_literal(edit.needle, edit.replacement)
        else:
            if self.text[edit.start:edit.end] != edit.before:
                raise ValueError('The reading history no longer matches this draft.')
            self.replace_range(edit.start, edit.end, edit.after, reason=edit.reason)
        self._redo = remaining
        return True

    def difference_text(self):
        if not self._edits:
            return 'The reading copy matches the immutable original text.'
        lines = ['Reading-copy changes only. The original and saved quotations are unchanged.']
        for number, edit in enumerate(self._edits, 1):
            if isinstance(edit, NarrationLiteralEdit):
                lines.extend(('', f'{number}. {edit.reason}; {edit.matches} occurrences in one reversible batch.',
                              'Find: ' + edit.needle, 'Replace with: ' + (edit.replacement or '(empty)')))
                continue
            lines.extend(('', f'{number}. {edit.reason}; reading offsets {edit.start} to {edit.end}.',
                          'Before: ' + (edit.before or '(empty)'), 'After: ' + (edit.after or '(empty)')))
        return '\n'.join(lines)

    def to_dict(self):
        self.assert_source()
        return {'kind': 'research-narration-draft', 'version': 2,
                'source_id': self._source_id, 'source_text': self._source,
                'source_sha256': self._source_hash, 'reading_sha256': text_digest(self.text),
                'edits': [({'kind': 'literal-batch', **asdict(edit)} if isinstance(edit, NarrationLiteralEdit)
                           else asdict(edit)) for edit in self._edits]}

    @classmethod
    def from_dict(cls, value, *, source_text=None, source_id=None):
        keys = {'kind', 'version', 'source_id', 'source_text', 'source_sha256', 'reading_sha256', 'edits'}
        if (type(value) is not dict or set(value) != keys or value['kind'] != 'research-narration-draft'
                or type(value['version']) is not int or value['version'] not in (1, 2)
                or type(value['edits']) is not list or len(value['edits']) > MAX_HISTORY_EDITS):
            raise ValueError('Unsupported narration draft.')
        draft = cls(value['source_text'], source_id=value['source_id'])
        draft.assert_source(source_text, source_sha256=value['source_sha256'])
        if source_id is not None and source_id != draft.source_id:
            raise ValueError('The draft belongs to a different source.')
        edit_keys = {'start', 'end', 'before', 'after', 'reason'}
        for item in value['edits']:
            if type(item) is dict and item.get('kind') == 'literal-batch' and value['version'] == 2:
                keys = {'kind', 'needle', 'replacement', 'matches', 'before_sha256', 'after_sha256', 'reason'}
                if (set(item) != keys or type(item['matches']) is not int or not 2 <= item['matches'] <= MAX_REPLACEMENTS
                        or item['reason'] != 'Literal pronunciation replacement'
                        or item['before_sha256'] != text_digest(draft.text)):
                    raise ValueError('Invalid literal replacement history.')
                count = draft.replace_literal(item['needle'], item['replacement'])
                if count != item['matches'] or item['after_sha256'] != text_digest(draft.text):
                    raise ValueError('Literal replacement history does not match its reading copy.')
                continue
            if type(item) is not dict or set(item) != edit_keys:
                raise ValueError('Invalid narration edit.')
            draft._range(item['start'], item['end'])
            if draft.text[item['start']:item['end']] != item['before']:
                raise ValueError('Narration edit does not match its source history.')
            if not draft.replace_range(item['start'], item['end'], item['after'], reason=item['reason']):
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
